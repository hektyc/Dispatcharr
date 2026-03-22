"""HLS Output Manager.

Singleton manager that coordinates all HLS output sessions.
Handles session creation/destruction, multi-worker ownership via Redis,
stream switching, and lifecycle management.
"""

import os
import time
import uuid
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

# Redis key patterns for ownership
OWNER_KEY_PREFIX = "hls_output:owner:{uuid}"
OWNER_TTL = 30  # Seconds before ownership expires


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
    """Get the direct stream URL, user agent, and stream profile for a channel.

    Follows the channel's stream selection to find the actual URL
    and resolves the channel's stream profile for HLS command building.

    Args:
        channel: A Channel model instance.

    Returns:
        Tuple of (url, user_agent_string, stream_profile) or (None, None, None).
    """
    try:
        # channel.streams is a ManyToManyField through ChannelStream,
        # .first() returns a Stream instance directly
        stream = channel.streams.first()
        if not stream:
            return None, None, None

        # Get the stream URL
        url = stream.url
        if not url:
            return None, None, None

        # Get the channel's stream profile (handles fallback to default)
        profile = channel.get_stream_profile()

        # Get user agent from the profile, or use a sensible default
        user_agent = "VLC/3.0.20 LibVLC/3.0.20"
        if profile and profile.user_agent:
            user_agent = profile.user_agent.user_agent

        return url, user_agent, profile
    except Exception as e:
        logger.error("Failed to get stream URL for channel %s: %s", channel.uuid, e)
        return None, None, None


def get_direct_stream_url_for_stream(stream):
    """Get the direct URL, user agent, and stream profile for a specific stream.

    Args:
        stream: A Stream model instance.

    Returns:
        Tuple of (url, user_agent_string, stream_profile) or (None, None, None).
    """
    try:
        url = stream.url
        if not url:
            return None, None, None

        # Get the default HLS profile for standalone streams
        profile = _get_default_hls_profile()

        user_agent = "VLC/3.0.20 LibVLC/3.0.20"
        if profile and profile.user_agent:
            user_agent = profile.user_agent.user_agent

        return url, user_agent, profile
    except Exception as e:
        logger.error("Failed to get stream URL: %s", e)
        return None, None, None


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

    def _refresh_ownership(self, channel_uuid: str):
        """Refresh the TTL on our ownership lock."""
        try:
            rc = self.redis_client
            if rc is None:
                return

            key = self._get_owner_key(channel_uuid)
            current_owner = rc.get(key)
            if current_owner and current_owner.decode("utf-8") == self._worker_id:
                rc.expire(key, OWNER_TTL)
        except Exception:
            pass

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
            return owner_id != self._worker_id
        except Exception:
            return False

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
            if session and session.is_running:
                return session

            # Check if running on another worker
            if self._is_session_running_elsewhere(channel_uuid):
                logger.info(
                    "Session for %s running on another worker, creating proxy session",
                    channel_uuid,
                )
                # Create a storage-only session for serving content
                storage = _create_storage(channel_uuid)
                session = HLSSession(channel_uuid, storage)
                self._sessions[channel_uuid] = session
                return session

            # Try to acquire ownership
            if not self._try_acquire_ownership(channel_uuid):
                logger.info("Could not acquire ownership for %s", channel_uuid)
                return None

            # Look up channel and get stream URL + profile
            channel, stream = get_channel_or_stream(channel_uuid)
            stream_profile = None
            if channel:
                url, user_agent, stream_profile = get_direct_stream_url(channel)
            elif stream:
                url, user_agent, stream_profile = get_direct_stream_url_for_stream(stream)
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

            # Create storage and session
            storage = _create_storage(channel_uuid)
            session = HLSSession(channel_uuid, storage, stream_profile=stream_profile)

            if session.start(url, user_agent):
                self._sessions[channel_uuid] = session

                # Register shutdown callback
                def shutdown_cb(uuid=channel_uuid):
                    self.stop_session(uuid)

                hls_client_manager.register_shutdown_callback(channel_uuid, shutdown_cb)

                # Start client manager if not running
                hls_client_manager.start()

                logger.info("HLS session started for %s", channel_uuid)
                return session
            else:
                self._release_ownership(channel_uuid)
                return None

    def stop_session(self, channel_uuid: str):
        """Stop an HLS session for a channel."""
        with self._sessions_lock:
            session = self._sessions.pop(channel_uuid, None)

        if session:
            session.stop()
            hls_client_manager.unregister_shutdown_callback(channel_uuid)
            self._release_ownership(channel_uuid)
            logger.info("HLS session stopped for %s", channel_uuid)

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


# Singleton instance
hls_manager = HLSOutputManager()


def cleanup_all_hls_sessions():
    """Module-level cleanup function for use with atexit."""
    try:
        hls_manager.stop_all_sessions()
    except Exception as e:
        logger.error("Error during HLS cleanup: %s", e)
