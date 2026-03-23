"""HLS Output Manager.

Singleton manager that coordinates all HLS output sessions.
Handles session creation/destruction, multi-worker ownership via Redis,
stream switching, lifecycle management, and periodic cleanup/heartbeat
thread matching the TS proxy's cleanup_task pattern.
"""

import os
import time
import uuid
import shutil
import threading
import logging
from typing import Optional, Dict

from .config import hls_config
from .session import HLSSession
from .client_manager import hls_client_manager

logger = logging.getLogger(__name__)


def _get_redis_connection_params():
    """Get Redis connection parameters from Django settings / environment."""
    from django.conf import settings
    host = os.environ.get("REDIS_HOST", getattr(settings, "REDIS_HOST", "localhost"))
    port = int(os.environ.get("REDIS_PORT", getattr(settings, "REDIS_PORT", 6379)))
    db = int(os.environ.get("REDIS_DB", getattr(settings, "REDIS_DB", 0)))
    return host, port, db

# Redis key patterns for ownership & heartbeat
OWNER_KEY_PREFIX = "hls_output:owner:{uuid}"
WORKER_HEARTBEAT_KEY_PREFIX = "hls_output:worker:{worker_id}:heartbeat"
OWNER_TTL = 30  # Seconds before ownership expires
HEARTBEAT_TTL = 30  # Worker heartbeat TTL
CLEANUP_INTERVAL = 10  # Seconds between cleanup cycles


def get_channel_or_stream(identifier: str):
    """Look up a Channel by UUID or a Stream by stream_hash.

    Args:
        identifier: Channel UUID or stream hash.

    Returns:
        Tuple of (channel, stream) - one may be None.
    """
    from apps.channels.models import Channel, Stream

    # Try channel UUID first
    try:
        channel = Channel.objects.get(uuid=identifier)
        return channel, None
    except Channel.DoesNotExist:
        pass

    # Try stream hash
    try:
        stream = Stream.objects.get(stream_hash=identifier)
        return None, stream
    except Stream.DoesNotExist:
        pass

    return None, None


def get_direct_stream_url(channel):
    """Get the direct stream URL, user agent, stream profile, and stream info for a channel.

    Follows the channel's stream selection to find the actual URL
    and resolves the channel's stream profile for HLS command building.

    Args:
        channel: A Channel model instance.

    Returns:
        Tuple of (url, user_agent_string, stream_profile, stream_info_dict)
        or (None, None, None, None).
        stream_info_dict contains 'stream_id' and 'm3u_profile_id'.
    """
    try:
        # channel.streams is a ManyToManyField through ChannelStream,
        # .first() returns a Stream instance directly
        stream = channel.streams.first()
        if not stream:
            return None, None, None, None

        # Get the stream URL
        url = stream.url
        if not url:
            return None, None, None, None

        # Get the channel's stream profile (handles fallback to default)
        profile = channel.get_stream_profile()

        # Get user agent from the profile, or use a sensible default
        user_agent = "VLC/3.0.20 LibVLC/3.0.20"
        if profile and profile.user_agent:
            user_agent = profile.user_agent.user_agent

        # Collect stream info for stats integration
        stream_info = {
            "stream_id": stream.id,
            "m3u_profile_id": None,
        }
        # Try to get M3U profile ID from the stream's M3U account
        if hasattr(stream, "m3u_account") and stream.m3u_account:
            try:
                from apps.m3u.models import M3UAccountProfile
                m3u_profile = M3UAccountProfile.objects.filter(
                    m3u_account=stream.m3u_account
                ).first()
                if m3u_profile:
                    stream_info["m3u_profile_id"] = m3u_profile.id
            except Exception:
                pass

        return url, user_agent, profile, stream_info
    except Exception as e:
        logger.error("Failed to get stream URL for channel %s: %s", channel.uuid, e)
        return None, None, None, None


def get_direct_stream_url_for_stream(stream):
    """Get the direct URL, user agent, stream profile, and stream info for a specific stream.

    Args:
        stream: A Stream model instance.

    Returns:
        Tuple of (url, user_agent_string, stream_profile, stream_info_dict)
        or (None, None, None, None).
    """
    try:
        url = stream.url
        if not url:
            return None, None, None, None

        # Get the default HLS profile for standalone streams
        profile = _get_default_hls_profile()

        user_agent = "VLC/3.0.20 LibVLC/3.0.20"
        if profile and profile.user_agent:
            user_agent = profile.user_agent.user_agent

        stream_info = {
            "stream_id": stream.id,
            "m3u_profile_id": None,
        }

        return url, user_agent, profile, stream_info
    except Exception as e:
        logger.error("Failed to get stream URL: %s", e)
        return None, None, None, None


def _get_default_hls_profile():
    """Get a built-in HLS stream profile.

    Tries 'HLS FFmpeg' first, then falls back to 'HLS Proxy'.

    Returns:
        A StreamProfile instance, or None if not found.
    """
    try:
        from core.models import StreamProfile
        profile = StreamProfile.objects.filter(name="HLS FFmpeg", locked=True).first()
        if not profile:
            profile = StreamProfile.objects.filter(name="HLS Proxy", locked=True).first()
        return profile
    except Exception as e:
        logger.error("Failed to load default HLS profile: %s", e)
        return None


def _create_storage(channel_uuid: str):
    """Create the appropriate storage backend for a channel.

    Returns:
        A SegmentStore instance based on the configured backend.
    """
    backend = hls_config.storage_backend

    if backend == "redis":
        import redis as redis_lib
        from .storage.redis_store import RedisSegmentStore

        host, port, db = _get_redis_connection_params()
        redis_client = redis_lib.Redis(host=host, port=port, db=db)
        return RedisSegmentStore(redis_client, ttl=hls_config.redis_segment_ttl)
    else:
        from .storage.filesystem_store import FilesystemSegmentStore

        return FilesystemSegmentStore(hls_config.output_path)


class HLSOutputManager:
    """Singleton manager for all HLS output sessions.

    Handles:
    - Session creation and destruction
    - Multi-worker ownership coordination via Redis
    - Stream switching
    - Automatic cleanup on shutdown
    - Periodic cleanup/heartbeat thread (ownership refresh, orphan detection)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._sessions: Dict[str, HLSSession] = {}
        self._sessions_lock = threading.Lock()
        self._redis = None
        self._worker_id = str(uuid.uuid4())[:8]
        self._cleanup_thread = None
        self._cleanup_stop_event = threading.Event()
        # Track which channels had stream connection allocated
        self._allocated_streams: Dict[str, dict] = {}  # channel_uuid -> {stream_id, ...}

    @property
    def redis_client(self):
        """Lazy Redis client initialization."""
        if self._redis is None:
            try:
                import redis as redis_lib
                host, port, db = _get_redis_connection_params()
                self._redis = redis_lib.Redis(host=host, port=port, db=db)
            except Exception as e:
                logger.error("Failed to connect to Redis: %s", e)
        return self._redis

    def _get_owner_key(self, channel_uuid: str) -> str:
        return OWNER_KEY_PREFIX.format(uuid=channel_uuid)

    def _get_worker_heartbeat_key(self) -> str:
        return WORKER_HEARTBEAT_KEY_PREFIX.format(worker_id=self._worker_id)

    def _try_acquire_ownership(self, channel_uuid: str) -> bool:
        """Try to acquire ownership of a channel session via Redis SETNX."""
        try:
            rc = self.redis_client
            if rc is None:
                return True  # If Redis unavailable, allow

            key = self._get_owner_key(channel_uuid)
            acquired = rc.set(key, self._worker_id, nx=True, ex=OWNER_TTL)
            return bool(acquired)
        except Exception as e:
            logger.error("Failed to acquire ownership for %s: %s", channel_uuid, e)
            return True

    def _release_ownership(self, channel_uuid: str):
        """Release ownership of a channel session."""
        try:
            rc = self.redis_client
            if rc is None:
                return

            key = self._get_owner_key(channel_uuid)
            # Only release if we own it
            current_owner = rc.get(key)
            if current_owner and current_owner.decode("utf-8") == self._worker_id:
                rc.delete(key)
        except Exception as e:
            logger.debug("Failed to release ownership for %s: %s", channel_uuid, e)

    def _refresh_ownership(self, channel_uuid: str) -> bool:
        """Refresh the TTL on our ownership lock.

        Returns True if we still own the channel, False if ownership
        was lost (e.g. another worker acquired it).
        """
        try:
            rc = self.redis_client
            if rc is None:
                return True

            key = self._get_owner_key(channel_uuid)
            current_owner = rc.get(key)
            if current_owner is None:
                # Key expired — try to re-acquire
                acquired = rc.set(key, self._worker_id, nx=True, ex=OWNER_TTL)
                if acquired:
                    logger.warning(
                        "Re-acquired expired ownership for HLS channel %s",
                        channel_uuid,
                    )
                    return True
                else:
                    logger.warning(
                        "Lost ownership of HLS channel %s — another worker took it",
                        channel_uuid,
                    )
                    return False
            elif current_owner.decode("utf-8") == self._worker_id:
                rc.expire(key, OWNER_TTL)
                return True
            else:
                # Another worker owns it now
                return False
        except Exception as e:
            logger.debug("Failed to refresh ownership for %s: %s", channel_uuid, e)
            return True  # Assume still owner on error

    def _is_session_running_elsewhere(self, channel_uuid: str) -> bool:
        """Check if another worker is running a session for this channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return False

            key = self._get_owner_key(channel_uuid)
            owner = rc.get(key)
            if owner is None:
                return False
            owner_id = owner.decode("utf-8") if isinstance(owner, bytes) else owner
            if owner_id == self._worker_id:
                return False

            # Verify the owner worker is still alive via heartbeat
            heartbeat_key = WORKER_HEARTBEAT_KEY_PREFIX.format(worker_id=owner_id)
            if rc.exists(heartbeat_key):
                return True

            # Owner's heartbeat is missing — the owner may be dead.
            # Don't report as running elsewhere; allow re-acquisition.
            logger.info(
                "HLS channel %s owner %s has no heartbeat — treating as unowned",
                channel_uuid, owner_id,
            )
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Cleanup / Heartbeat Thread (Task 1.1, 3.2, 3.3)
    # ------------------------------------------------------------------ #

    def _start_cleanup_thread(self):
        """Start the periodic cleanup/heartbeat thread.

        Mirrors the TS proxy's _start_cleanup_thread pattern:
        - Refreshes worker heartbeat
        - Refreshes ownership TTL for owned sessions
        - Refreshes metadata TTL for owned sessions
        - Detects ownership loss and stops FFmpeg
        - Scans for orphaned HLS output directories
        """
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return

        self._cleanup_stop_event.clear()

        def cleanup_task():
            while not self._cleanup_stop_event.is_set():
                try:
                    self._cleanup_cycle()
                except Exception as e:
                    logger.error("HLS cleanup cycle error: %s", e)
                self._cleanup_stop_event.wait(CLEANUP_INTERVAL)

        self._cleanup_thread = threading.Thread(
            target=cleanup_task,
            name=f"hls-cleanup-{self._worker_id}",
            daemon=True,
        )
        self._cleanup_thread.start()
        logger.info("HLS cleanup/heartbeat thread started (worker %s)", self._worker_id)

    def _stop_cleanup_thread(self):
        """Stop the cleanup thread."""
        self._cleanup_stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None

    def _cleanup_cycle(self):
        """Single cleanup cycle — called every CLEANUP_INTERVAL seconds."""
        rc = self.redis_client
        if rc is None:
            return

        # 1. Send worker heartbeat
        try:
            heartbeat_key = self._get_worker_heartbeat_key()
            rc.setex(heartbeat_key, HEARTBEAT_TTL, str(time.time()))
        except Exception as e:
            logger.debug("Failed to set worker heartbeat: %s", e)

        # 2. Refresh ownership and metadata for locally-owned sessions
        with self._sessions_lock:
            sessions_snapshot = dict(self._sessions)

        ownership_lost = []
        for channel_uuid, session in sessions_snapshot.items():
            # Skip proxy sessions — they don't own anything
            if session.is_proxy:
                continue

            if session.is_running:
                still_owner = self._refresh_ownership(channel_uuid)
                if still_owner:
                    # Refresh metadata TTL
                    session.refresh_metadata_ttl()
                else:
                    # Task 3.3: Ownership lost — stop local FFmpeg
                    logger.warning(
                        "Ownership lost for HLS channel %s — stopping local session",
                        channel_uuid,
                    )
                    ownership_lost.append(channel_uuid)
            else:
                # Session is not running (maybe crashed) — log and consider removal
                logger.debug(
                    "HLS session for %s is not running in cleanup cycle",
                    channel_uuid,
                )

        # Stop sessions that lost ownership (outside the lock)
        for channel_uuid in ownership_lost:
            try:
                self._stop_session_internal(channel_uuid, release_ownership=False)
            except Exception as e:
                logger.error(
                    "Error stopping ownership-lost session %s: %s",
                    channel_uuid, e,
                )

        # 3. Task 3.2: Scan for orphaned HLS output directories
        self._cleanup_orphaned_directories()

    def _cleanup_orphaned_directories(self):
        """Scan HLS_PATH for channel UUID directories with no active session or owner."""
        hls_path = hls_config.output_path
        if not hls_path or not os.path.isdir(hls_path):
            return

        try:
            for entry in os.listdir(hls_path):
                dir_path = os.path.join(hls_path, entry)
                if not os.path.isdir(dir_path):
                    continue

                channel_uuid = entry
                # Skip if we have a local session for it
                if channel_uuid in self._sessions:
                    continue

                # Check if there is a valid ownership key
                try:
                    rc = self.redis_client
                    if rc is not None:
                        owner_key = self._get_owner_key(channel_uuid)
                        owner = rc.get(owner_key)
                        if owner is not None:
                            # Check if owner has heartbeat
                            owner_id = owner.decode("utf-8")
                            hb_key = WORKER_HEARTBEAT_KEY_PREFIX.format(worker_id=owner_id)
                            if rc.exists(hb_key):
                                continue  # Active owner, skip
                except Exception:
                    pass

                # No active session, no live owner — remove orphaned directory
                try:
                    shutil.rmtree(dir_path, ignore_errors=True)
                    logger.info(
                        "Cleaned up orphaned HLS directory: %s", dir_path,
                    )
                except Exception as e:
                    logger.debug("Failed to clean orphaned directory %s: %s", dir_path, e)
        except Exception as e:
            logger.debug("Error scanning for orphaned HLS directories: %s", e)

    # ------------------------------------------------------------------ #
    #  Session management
    # ------------------------------------------------------------------ #

    def get_or_start_session(self, channel_uuid: str) -> Optional[HLSSession]:
        """Get an existing session or start a new one.

        Args:
            channel_uuid: The channel UUID.

        Returns:
            An HLSSession instance, or None if unable to start.
        """
        if not hls_config.is_enabled:
            logger.warning("HLS output is disabled (HLS_PATH not set)")
            return None

        with self._sessions_lock:
            # Check for existing session
            session = self._sessions.get(channel_uuid)
            if session and (session.is_running or session.is_proxy):
                return session

            # Check if running on another worker
            if self._is_session_running_elsewhere(channel_uuid):
                logger.info(
                    "Session for %s running on another worker, creating proxy session",
                    channel_uuid,
                )
                # Task 4.1: Create a clearly-marked proxy session
                storage = _create_storage(channel_uuid)
                session = HLSSession(channel_uuid, storage, is_proxy=True)
                self._sessions[channel_uuid] = session
                # Do NOT register shutdown callbacks for proxy sessions
                return session

            # Try to acquire ownership
            if not self._try_acquire_ownership(channel_uuid):
                logger.info("Could not acquire ownership for %s", channel_uuid)
                return None

            # Look up channel and get stream URL + profile
            channel, stream = get_channel_or_stream(channel_uuid)
            stream_profile = None
            stream_info = None
            connection_allocated = False
            if channel:
                # Task 1.3: Use channel.get_stream() for connection allocation
                # get_stream() returns (stream_id: int, profile_id: int, error_reason)
                try:
                    from apps.channels.models import Stream as StreamModel
                    alloc_stream_id, alloc_profile_id, alloc_error = channel.get_stream()
                    if alloc_error or not alloc_stream_id:
                        logger.error(
                            "Stream allocation failed for channel %s: %s",
                            channel_uuid, alloc_error,
                        )
                        self._release_ownership(channel_uuid)
                        return None
                    # Look up the actual Stream object
                    stream_obj = StreamModel.objects.get(id=alloc_stream_id)
                    url = stream_obj.url
                    if not url:
                        logger.error("Allocated stream has no URL for %s", channel_uuid)
                        channel.release_stream()
                        self._release_ownership(channel_uuid)
                        return None
                    connection_allocated = True
                    # Get profile and user_agent from channel
                    stream_profile = channel.get_stream_profile()
                    user_agent = "VLC/3.0.20 LibVLC/3.0.20"
                    if stream_profile and stream_profile.user_agent:
                        user_agent = stream_profile.user_agent.user_agent
                    stream_info = {
                        "stream_id": alloc_stream_id,
                        "m3u_profile_id": alloc_profile_id,
                    }
                except Exception as e:
                    logger.error(
                        "Error allocating stream for channel %s: %s",
                        channel_uuid, e,
                    )
                    # Fallback to get_direct_stream_url if get_stream fails
                    url, user_agent, stream_profile, stream_info = get_direct_stream_url(channel)
            elif stream:
                url, user_agent, stream_profile, stream_info = get_direct_stream_url_for_stream(stream)
            else:
                logger.error("Channel/stream not found: %s", channel_uuid)
                self._release_ownership(channel_uuid)
                return None

            if not url:
                logger.error("No stream URL for %s", channel_uuid)
                self._release_ownership(channel_uuid)
                return None

            # If the channel's profile isn't HLS-aware, use the default HLS profile
            if stream_profile and not stream_profile.is_hls_profile():
                hls_profile = _get_default_hls_profile()
                if hls_profile:
                    stream_profile = hls_profile

            # Extract stream/profile info for stats integration
            stream_id = stream_info.get("stream_id") if stream_info else None
            m3u_profile_id = stream_info.get("m3u_profile_id") if stream_info else None
            profile_name = stream_profile.name if stream_profile else ""

            # Create storage and session
            storage = _create_storage(channel_uuid)
            session = HLSSession(
                channel_uuid,
                storage,
                stream_profile=stream_profile,
                stream_id=stream_id,
                stream_profile_name=profile_name,
                m3u_profile_id=m3u_profile_id,
                worker_id=self._worker_id,
            )

            if session.start(url, user_agent):
                self._sessions[channel_uuid] = session

                # Track stream allocation for release on stop
                if connection_allocated and channel is not None:
                    self._allocated_streams[channel_uuid] = {
                        "channel": channel,
                    }

                # Register shutdown callback
                def shutdown_cb(uuid=channel_uuid):
                    self.stop_session(uuid)

                hls_client_manager.register_shutdown_callback(channel_uuid, shutdown_cb)

                # Start client manager if not running
                hls_client_manager.start()

                # Start cleanup thread if not running
                self._start_cleanup_thread()

                # Log system event for channel start
                self._log_channel_event(
                    "channel_start", channel_uuid,
                    channel=channel, stream_id=stream_id,
                    stream_type="hls",
                )

                logger.info("HLS session started for %s", channel_uuid)
                return session
            else:
                self._release_ownership(channel_uuid)
                # Release the allocated stream slot on failure
                if connection_allocated and channel is not None:
                    try:
                        channel.release_stream()
                    except Exception as e:
                        logger.debug("Failed to release stream on start failure: %s", e)
                return None

    def stop_session(self, channel_uuid: str):
        """Stop an HLS session for a channel.

        Handles full cleanup including:
        - Stopping the session (FFmpeg, storage, metadata)
        - Releasing stream connection allocation
        - Triggering final stats push after metadata deletion
        - Releasing ownership
        """
        self._stop_session_internal(channel_uuid, release_ownership=True)

    def _stop_session_internal(self, channel_uuid: str, release_ownership: bool = True):
        """Internal session stop with configurable ownership release."""
        with self._sessions_lock:
            session = self._sessions.pop(channel_uuid, None)

        if session:
            is_proxy = session.is_proxy

            if not is_proxy:
                # Stop the actual session (FFmpeg, storage, output dir, metadata)
                session.stop()

                # Task 2.2: Wait a small delay for Redis propagation, then push stats
                time.sleep(0.05)
                try:
                    hls_client_manager._trigger_stats_update()
                except Exception as e:
                    logger.debug("Failed to trigger final stats update: %s", e)

            hls_client_manager.unregister_shutdown_callback(channel_uuid)
            hls_client_manager.cleanup_channel(channel_uuid)

            if release_ownership and not is_proxy:
                self._release_ownership(channel_uuid)

            # Task 1.3: Release stream connection allocation
            alloc_info = self._allocated_streams.pop(channel_uuid, None)
            if alloc_info and not is_proxy:
                try:
                    alloc_info["channel"].release_stream()
                    logger.debug(
                        "Released stream connection for channel %s",
                        channel_uuid,
                    )
                except Exception as e:
                    logger.debug("Failed to release stream connection: %s", e)

            if not is_proxy:
                # Log system event for channel stop
                self._log_channel_event("channel_stop", channel_uuid, stream_type="hls")
                logger.info("HLS session stopped for %s", channel_uuid)
            else:
                logger.debug("HLS proxy session removed for %s", channel_uuid)

    def get_session(self, channel_uuid: str) -> Optional[HLSSession]:
        """Get an existing session without starting a new one."""
        return self._sessions.get(channel_uuid)

    def change_stream_url(self, channel_uuid: str, new_url: str, user_agent: str = None) -> bool:
        """Change the stream URL for an active session.

        Args:
            channel_uuid: The channel UUID.
            new_url: The new stream URL.
            user_agent: Optional new user agent.

        Returns:
            True if stream was changed successfully.
        """
        session = self._sessions.get(channel_uuid)
        if not session:
            logger.warning("No session to change stream for %s", channel_uuid)
            return False

        logger.info("Changing stream for %s to %s", channel_uuid, new_url[:60])
        return session.restart(new_url, user_agent)

    def stop_all_sessions(self):
        """Stop all active sessions. Called on shutdown."""
        self._stop_cleanup_thread()

        with self._sessions_lock:
            channel_uuids = list(self._sessions.keys())

        for uuid_str in channel_uuids:
            try:
                self.stop_session(uuid_str)
            except Exception as e:
                logger.error("Error stopping session %s: %s", uuid_str, e)

        logger.info("All HLS sessions stopped")

    def get_active_channels(self) -> list:
        """Get list of channel UUIDs with active sessions."""
        return list(self._sessions.keys())

    @staticmethod
    def _log_channel_event(event_type: str, channel_uuid: str, channel=None, **kwargs):
        """Log a system event for an HLS channel lifecycle event.

        Args:
            event_type: e.g. 'channel_start', 'channel_stop'
            channel_uuid: The channel UUID.
            channel: Optional Channel model instance (avoids extra DB lookup).
            **kwargs: Extra detail fields passed to log_system_event.
        """
        try:
            from core.utils import log_system_event

            channel_name = None
            if channel is not None:
                channel_name = getattr(channel, "name", None)
            else:
                # Try to look up channel name from DB
                try:
                    from apps.channels.models import Channel
                    ch = Channel.objects.filter(uuid=channel_uuid).first()
                    if ch:
                        channel_name = ch.name
                except Exception:
                    pass

            # Look up stream name if stream_id provided
            stream_id = kwargs.get("stream_id")
            if stream_id and "stream_name" not in kwargs:
                try:
                    from apps.channels.models import Stream
                    s = Stream.objects.filter(id=stream_id).first()
                    if s:
                        kwargs["stream_name"] = s.name
                except Exception:
                    pass

            log_system_event(
                event_type,
                channel_id=channel_uuid,
                channel_name=channel_name,
                **kwargs,
            )
        except Exception as e:
            logger.debug("Failed to log system event %s for %s: %s", event_type, channel_uuid, e)


# Singleton instance
hls_manager = HLSOutputManager()


def cleanup_all_hls_sessions():
    """Module-level cleanup function for use with atexit."""
    try:
        hls_manager.stop_all_sessions()
    except Exception as e:
        logger.error("Error during HLS cleanup: %s", e)
