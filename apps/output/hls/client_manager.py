# HLS Client Manager
# Tracks active HLS client connections and sends WebSocket updates

import threading
import time
import json
import logging
import redis
from typing import Dict, Set, Optional
from django.conf import settings
from core.utils import send_websocket_update

logger = logging.getLogger(__name__)


class HLSClientManager:
    """
    Manages HLS client connections with Redis backing for multi-worker support.

    This mirrors the TS proxy's client tracking pattern:
    - Clients are tracked per channel in Redis
    - WebSocket updates are sent when clients connect/disconnect
    - Client activity is tracked with TTL for automatic cleanup
    - Cleanup thread stops HLS sessions when all clients disconnect
    """

    # Redis key patterns
    CHANNEL_KEY_PREFIX = "hls_output:channel:"
    # CLIENT_TTL should be slightly longer than segment duration to survive between requests
    # HLS clients typically request segments every segment_duration seconds
    # We use a small fixed buffer since the shutdown_delay setting controls actual cleanup timing
    CLIENT_TTL_BUFFER = 3  # Seconds buffer on top of segment_duration for CLIENT_TTL
    HEARTBEAT_INTERVAL = 5  # Seconds between heartbeat updates
    CLEANUP_CHECK_INTERVAL = 1  # Seconds between cleanup checks (fast for quick response)
    DEFAULT_INACTIVITY_TIMEOUT = 5  # Fallback if database setting unavailable
    LOCK_TTL = 30  # Seconds to hold cleanup lock

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

        self._redis_client = None
        self._local_clients: Dict[str, Set[str]] = {}  # channel_uuid -> set of client_ids
        self._client_lock = threading.Lock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_running = False
        self._initialized = True
        logger.info("HLS Client Manager initialized")

        # Start the cleanup thread
        self._start_cleanup_thread()
    
    @property
    def redis_client(self):
        """Get or create Redis client."""
        if self._redis_client is None:
            try:
                redis_host = getattr(settings, 'REDIS_HOST', 'redis')
                redis_port = getattr(settings, 'REDIS_PORT', 6379)
                self._redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True
                )
                # Test connection
                self._redis_client.ping()
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {e}")
                self._redis_client = None
        return self._redis_client
    
    def _get_channel_clients_key(self, channel_uuid: str) -> str:
        """Get Redis key for channel's client set."""
        return f"{self.CHANNEL_KEY_PREFIX}{channel_uuid}:clients"
    
    def _get_client_key(self, channel_uuid: str, client_id: str) -> str:
        """Get Redis key for client metadata."""
        return f"{self.CHANNEL_KEY_PREFIX}{channel_uuid}:client:{client_id}"
    
    def _get_channel_metadata_key(self, channel_uuid: str) -> str:
        """Get Redis key for channel metadata."""
        return f"{self.CHANNEL_KEY_PREFIX}{channel_uuid}:metadata"

    def _get_cleanup_lock_key(self, channel_uuid: str) -> str:
        """Get Redis key for cleanup lock (prevents race conditions)."""
        return f"{self.CHANNEL_KEY_PREFIX}{channel_uuid}:cleanup_lock"

    def _acquire_cleanup_lock(self, channel_uuid: str) -> bool:
        """
        Try to acquire a distributed lock for cleanup operations.
        Returns True if lock acquired, False if another worker holds it.
        """
        if not self.redis_client:
            return True  # Allow cleanup if Redis unavailable

        lock_key = self._get_cleanup_lock_key(channel_uuid)
        # SETNX returns True only if key didn't exist
        acquired = self.redis_client.set(lock_key, "1", nx=True, ex=self.LOCK_TTL)
        return bool(acquired)

    def _release_cleanup_lock(self, channel_uuid: str):
        """Release the cleanup lock."""
        if self.redis_client:
            lock_key = self._get_cleanup_lock_key(channel_uuid)
            self.redis_client.delete(lock_key)

    def _get_client_ttl(self) -> int:
        """
        Get the TTL for client keys in Redis.

        This should be just long enough for clients to survive between segment requests.
        HLS clients request segments every segment_duration seconds, so we use:
        segment_duration + a small buffer.

        The actual cleanup timing is controlled by shutdown_delay setting.
        """
        from apps.output.hls.config import hls_config
        segment_duration = hls_config.segment_duration or 6
        return segment_duration + self.CLIENT_TTL_BUFFER

    def add_client(self, channel_uuid: str, client_id: str, client_ip: str,
                   user_agent: str = None) -> bool:
        """
        Add a client connection for an HLS channel.
        
        Args:
            channel_uuid: The channel UUID
            client_id: Unique client identifier
            client_ip: Client IP address
            user_agent: Client user agent string
            
        Returns:
            True if client was added, False if already exists or error
        """
        try:
            # Track locally
            with self._client_lock:
                if channel_uuid not in self._local_clients:
                    self._local_clients[channel_uuid] = set()
                
                if client_id in self._local_clients[channel_uuid]:
                    # Already tracked
                    return False
                
                self._local_clients[channel_uuid].add(client_id)
            
            # Track in Redis
            if self.redis_client:
                current_time = str(time.time())
                
                # Add to client set
                clients_key = self._get_channel_clients_key(channel_uuid)
                client_ttl = self._get_client_ttl()
                self.redis_client.sadd(clients_key, client_id)
                self.redis_client.expire(clients_key, client_ttl)

                # Store client metadata
                client_key = self._get_client_key(channel_uuid, client_id)
                self.redis_client.hset(client_key, mapping={
                    "ip_address": client_ip,
                    "user_agent": user_agent or "Unknown",
                    "connected_at": current_time,
                    "last_active": current_time,
                })
                self.redis_client.expire(client_key, client_ttl)

                # Update channel metadata
                metadata_key = self._get_channel_metadata_key(channel_uuid)
                self.redis_client.hset(metadata_key, mapping={
                    "state": "running",
                    "type": "hls",
                    "last_activity": current_time,
                })
                self.redis_client.expire(metadata_key, client_ttl * 2)
            
            logger.info(f"HLS client connected: {client_id} for channel {channel_uuid}")
            
            # Send WebSocket update
            self._trigger_stats_update()
            
            return True
            
        except Exception as e:
            logger.error(f"Error adding HLS client {client_id}: {e}")
            return False
    
    def remove_client(self, channel_uuid: str, client_id: str) -> bool:
        """Remove a client connection."""
        try:
            # Remove from local tracking
            with self._client_lock:
                if channel_uuid in self._local_clients:
                    self._local_clients[channel_uuid].discard(client_id)
                    if not self._local_clients[channel_uuid]:
                        del self._local_clients[channel_uuid]
            
            # Remove from Redis
            if self.redis_client:
                clients_key = self._get_channel_clients_key(channel_uuid)
                self.redis_client.srem(clients_key, client_id)
                
                client_key = self._get_client_key(channel_uuid, client_id)
                self.redis_client.delete(client_key)
            
            logger.info(f"HLS client disconnected: {client_id} from channel {channel_uuid}")

            # Send WebSocket update
            self._trigger_stats_update()

            return True

        except Exception as e:
            logger.error(f"Error removing HLS client {client_id}: {e}")
            return False

    def update_client_activity(self, channel_uuid: str, client_id: str) -> bool:
        """Update client's last activity timestamp."""
        try:
            if self.redis_client:
                current_time = str(time.time())
                client_ttl = self._get_client_ttl()

                # Ensure client is in the set (re-add in case it expired)
                clients_key = self._get_channel_clients_key(channel_uuid)
                self.redis_client.sadd(clients_key, client_id)
                self.redis_client.expire(clients_key, client_ttl)

                # Update client last_active
                client_key = self._get_client_key(channel_uuid, client_id)
                self.redis_client.hset(client_key, "last_active", current_time)
                self.redis_client.expire(client_key, client_ttl)

                # Update channel metadata
                metadata_key = self._get_channel_metadata_key(channel_uuid)
                self.redis_client.hset(metadata_key, "last_activity", current_time)
                self.redis_client.expire(metadata_key, client_ttl * 2)

            return True
        except Exception as e:
            logger.debug(f"Error updating HLS client activity: {e}")
            return False

    def get_client_count(self, channel_uuid: str) -> int:
        """Get the number of clients for a channel."""
        try:
            if self.redis_client:
                clients_key = self._get_channel_clients_key(channel_uuid)
                return self.redis_client.scard(clients_key) or 0
            return 0
        except Exception as e:
            logger.debug(f"Error getting HLS client count: {e}")
            return 0

    def get_all_hls_channels(self) -> list:
        """Get info for all active HLS channels."""
        channels = []
        try:
            if self.redis_client:
                # Scan for all HLS channel metadata keys
                cursor = 0
                while True:
                    cursor, keys = self.redis_client.scan(
                        cursor,
                        match=f"{self.CHANNEL_KEY_PREFIX}*:metadata",
                        count=100
                    )

                    for key in keys:
                        # Extract channel UUID from key
                        # Key format: hls_output:channel:{uuid}:metadata
                        parts = key.split(":")
                        if len(parts) >= 4:
                            channel_uuid = parts[2]
                            channel_info = self._get_channel_info(channel_uuid)
                            if channel_info:
                                channels.append(channel_info)

                    if cursor == 0:
                        break

        except Exception as e:
            logger.error(f"Error getting HLS channels: {e}")

        return channels

    def _get_channel_info(self, channel_uuid: str) -> Optional[dict]:
        """Get detailed info for a channel including clients and stream info."""
        try:
            if not self.redis_client:
                return None

            metadata_key = self._get_channel_metadata_key(channel_uuid)
            metadata = self.redis_client.hgetall(metadata_key)

            if not metadata:
                return None

            # Helper to decode Redis bytes
            def get_field(field_name, default=None, cast=str):
                """Get a field from metadata, handling bytes and type casting."""
                key = field_name.encode('utf-8') if isinstance(field_name, str) else field_name
                value = metadata.get(key) or metadata.get(field_name)
                if value is None:
                    return default
                if isinstance(value, bytes):
                    value = value.decode('utf-8')
                try:
                    return cast(value) if cast != str else value
                except (ValueError, TypeError):
                    return default

            # Get client count
            clients_key = self._get_channel_clients_key(channel_uuid)
            client_count = self.redis_client.scard(clients_key) or 0

            # Get client details
            client_ids = self.redis_client.smembers(clients_key) or set()
            clients = []

            for client_id in list(client_ids)[:10]:  # Limit to 10 clients
                client_key = self._get_client_key(channel_uuid, client_id)
                client_data = self.redis_client.hgetall(client_key)
                if client_data:
                    clients.append({
                        "client_id": client_id,
                        "ip_address": client_data.get("ip_address", "Unknown"),
                        "user_agent": client_data.get("user_agent", "Unknown"),
                        "connected_at": float(client_data.get("connected_at", 0)),
                        "last_active": float(client_data.get("last_active", 0)),
                    })

            # Calculate uptime
            init_time = float(get_field("init_time", time.time(), float))
            uptime = time.time() - init_time

            # Try to get channel name from database
            channel_name = channel_uuid
            try:
                from apps.channels.models import Channel
                channel = Channel.objects.filter(uuid=channel_uuid).first()
                if channel:
                    channel_name = channel.name
            except Exception:
                pass

            # Build info dict with all available fields
            info = {
                "channel_id": channel_uuid,
                "channel_name": channel_name,
                "state": get_field("state", "unknown"),
                "type": "hls",
                "client_count": client_count,
                "clients": clients,
                "uptime": uptime,
                "started_at": get_field("init_time", "0"),
            }

            # Add stream identification fields (for Active Connections display)
            stream_id = get_field("stream_id")
            if stream_id:
                info["stream_id"] = int(stream_id)

            stream_name = get_field("stream_name")
            if stream_name:
                info["stream_name"] = stream_name

            stream_profile = get_field("stream_profile")
            if stream_profile:
                info["stream_profile"] = stream_profile

            stream_profile_name = get_field("stream_profile_name")
            if stream_profile_name:
                info["stream_profile_name"] = stream_profile_name

            m3u_profile_id = get_field("m3u_profile_id")
            if m3u_profile_id:
                info["m3u_profile_id"] = m3u_profile_id

            m3u_profile_name = get_field("m3u_profile_name")
            if m3u_profile_name:
                info["m3u_profile_name"] = m3u_profile_name

            m3u_account_name = get_field("m3u_account_name")
            if m3u_account_name:
                info["m3u_account_name"] = m3u_account_name

            # Add stream info fields (same as TS proxy for consistency)
            video_codec = get_field("video_codec")
            if video_codec:
                info["video_codec"] = video_codec

            resolution = get_field("resolution")
            if resolution:
                info["resolution"] = resolution

            source_fps = get_field("source_fps", cast=float)
            if source_fps:
                info["source_fps"] = source_fps

            audio_codec = get_field("audio_codec")
            if audio_codec:
                info["audio_codec"] = audio_codec

            audio_channels = get_field("audio_channels")
            if audio_channels:
                info["audio_channels"] = audio_channels

            sample_rate = get_field("sample_rate", cast=int)
            if sample_rate:
                info["sample_rate"] = sample_rate

            audio_bitrate = get_field("audio_bitrate", cast=float)
            if audio_bitrate:
                info["audio_bitrate"] = audio_bitrate

            stream_type = get_field("stream_type")
            if stream_type:
                info["stream_type"] = stream_type

            # Add FFmpeg performance stats
            ffmpeg_speed = get_field("ffmpeg_speed", cast=float)
            if ffmpeg_speed:
                info["ffmpeg_speed"] = ffmpeg_speed

            ffmpeg_fps = get_field("ffmpeg_fps", cast=float)
            if ffmpeg_fps:
                info["ffmpeg_fps"] = ffmpeg_fps

            ffmpeg_bitrate = get_field("ffmpeg_bitrate", cast=float)
            if ffmpeg_bitrate:
                info["ffmpeg_bitrate"] = ffmpeg_bitrate

            return info

        except Exception as e:
            logger.error(f"Error getting HLS channel info for {channel_uuid}: {e}")
            return None

    def _trigger_stats_update(self):
        """Send WebSocket update with current HLS channel stats."""
        try:
            # Get all HLS channels
            hls_channels = self.get_all_hls_channels()

            # Also get TS proxy channels for combined update
            ts_channels = []
            try:
                from apps.proxy.ts_proxy.channel_status import ChannelStatus
                # Scan for TS proxy channels
                if self.redis_client:
                    cursor = 0
                    while True:
                        cursor, keys = self.redis_client.scan(
                            cursor,
                            match="ts_proxy:channel:*:metadata",
                            count=100
                        )
                        for key in keys:
                            parts = key.split(":")
                            if len(parts) >= 4:
                                ch_id = parts[2]
                                channel_info = ChannelStatus.get_basic_channel_info(ch_id)
                                if channel_info:
                                    ts_channels.append(channel_info)
                        if cursor == 0:
                            break
            except Exception as e:
                logger.debug(f"Could not get TS channels: {e}")

            # Combine all channels
            all_channels = ts_channels + hls_channels

            # Send WebSocket update
            send_websocket_update(
                "updates",
                "update",
                {
                    "success": True,
                    "type": "channel_stats",
                    "stats": json.dumps({
                        "channels": all_channels,
                        "count": len(all_channels)
                    })
                }
            )

            logger.debug(f"Sent HLS stats update: {len(hls_channels)} HLS, {len(ts_channels)} TS channels")

        except Exception as e:
            logger.debug(f"Failed to trigger HLS stats update: {e}")

    def set_channel_active(self, channel_uuid: str, stream_url: str = None, pid: int = None,
                           stream_metadata: dict = None):
        """Mark a channel as having an active HLS session.

        Args:
            channel_uuid: The channel UUID
            stream_url: The stream URL being processed
            pid: The FFmpeg process ID (critical for multi-worker cleanup)
            stream_metadata: Additional stream metadata (stream_id, stream_name, m3u_profile, etc.)
        """
        try:
            if self.redis_client:
                current_time = str(time.time())
                metadata_key = self._get_channel_metadata_key(channel_uuid)

                mapping = {
                    "state": "running",
                    "type": "hls",
                    "init_time": current_time,
                    "last_activity": current_time,
                    "url": stream_url or "",
                }

                # Store PID so any worker can stop the process
                if pid is not None:
                    mapping["pid"] = str(pid)

                # Store stream metadata for Active Connections display
                if stream_metadata:
                    if stream_metadata.get("stream_id"):
                        mapping["stream_id"] = stream_metadata["stream_id"]
                    if stream_metadata.get("stream_name"):
                        mapping["stream_name"] = stream_metadata["stream_name"]
                    if stream_metadata.get("stream_profile"):
                        mapping["stream_profile"] = stream_metadata["stream_profile"]
                    if stream_metadata.get("stream_profile_name"):
                        mapping["stream_profile_name"] = stream_metadata["stream_profile_name"]
                    if stream_metadata.get("m3u_profile_id"):
                        mapping["m3u_profile_id"] = stream_metadata["m3u_profile_id"]
                    if stream_metadata.get("m3u_profile_name"):
                        mapping["m3u_profile_name"] = stream_metadata["m3u_profile_name"]
                    if stream_metadata.get("m3u_account_name"):
                        mapping["m3u_account_name"] = stream_metadata["m3u_account_name"]

                self.redis_client.hset(metadata_key, mapping=mapping)
                self.redis_client.expire(metadata_key, self._get_client_ttl() * 10)

                logger.info(f"HLS channel marked active: {channel_uuid} (PID: {pid})")
                self._trigger_stats_update()

        except Exception as e:
            logger.error(f"Error setting HLS channel active: {e}")

    def set_channel_inactive(self, channel_uuid: str):
        """Mark a channel as no longer having an active HLS session."""
        try:
            # Clean up all channel data in Redis
            if self.redis_client:
                # Delete metadata
                metadata_key = self._get_channel_metadata_key(channel_uuid)
                self.redis_client.delete(metadata_key)

                # Delete all clients
                clients_key = self._get_channel_clients_key(channel_uuid)
                client_ids = self.redis_client.smembers(clients_key) or set()

                for client_id in client_ids:
                    client_key = self._get_client_key(channel_uuid, client_id)
                    self.redis_client.delete(client_key)

                self.redis_client.delete(clients_key)

            # Clean up local tracking
            with self._client_lock:
                if channel_uuid in self._local_clients:
                    del self._local_clients[channel_uuid]

            logger.info(f"HLS channel marked inactive: {channel_uuid}")
            self._trigger_stats_update()

        except Exception as e:
            logger.error(f"Error setting HLS channel inactive: {e}")

    def _start_cleanup_thread(self):
        """Start the background cleanup thread that stops inactive HLS sessions."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return

        self._cleanup_running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="hls-client-cleanup"
        )
        self._cleanup_thread.start()
        logger.info("HLS client cleanup thread started")

    def _stop_cleanup_thread(self):
        """Stop the cleanup thread."""
        self._cleanup_running = False
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None

    def _cleanup_loop(self):
        """
        Background loop that checks for inactive HLS channels and stops them.

        This is the key mechanism that detects when clients have stopped watching
        and stops the FFmpeg process to prevent runaway segment generation.
        """
        logger.info("HLS cleanup loop starting")

        while self._cleanup_running:
            try:
                self._check_and_cleanup_inactive_channels()
            except Exception as e:
                logger.error(f"Error in HLS cleanup loop: {e}")

            # Sleep in small increments so we can exit quickly
            for _ in range(self.CLEANUP_CHECK_INTERVAL):
                if not self._cleanup_running:
                    break
                time.sleep(1)

        logger.info("HLS cleanup loop stopped")

    def _check_and_cleanup_inactive_channels(self):
        """
        Check for channels with no recent client activity and stop them.

        A channel is considered inactive if:
        1. It has an active FFmpeg session (metadata exists in Redis)
        2. No clients are registered AND no requests within shutdown_delay seconds

        IMPORTANT: HLS clients fetch segments every ~6 seconds (segment duration).
        We must account for this by checking client count, not just last_activity.
        A channel with registered clients is NEVER considered inactive.
        """
        if not self.redis_client:
            return

        try:
            # Scan for all HLS channel metadata keys
            channels_to_check = []
            cursor = 0
            while True:
                cursor, keys = self.redis_client.scan(
                    cursor,
                    match=f"{self.CHANNEL_KEY_PREFIX}*:metadata",
                    count=100
                )

                for key in keys:
                    # Extract channel UUID from key
                    # Key format: hls_output:channel:{uuid}:metadata
                    parts = key.split(":")
                    if len(parts) >= 4:
                        channel_uuid = parts[2]
                        channels_to_check.append(channel_uuid)

                if cursor == 0:
                    break

            # Check each channel for activity
            current_time = time.time()

            for channel_uuid in channels_to_check:
                try:
                    # First check if there are any clients registered
                    # If there are clients, the channel is ACTIVE regardless of last_activity
                    clients_key = self._get_channel_clients_key(channel_uuid)
                    client_count = self.redis_client.scard(clients_key) or 0

                    if client_count > 0:
                        # Channel has active clients - refresh last_activity and skip
                        metadata_key = self._get_channel_metadata_key(channel_uuid)
                        self.redis_client.hset(metadata_key, "last_activity", str(current_time))
                        continue

                    # No clients - check how long since last activity
                    metadata_key = self._get_channel_metadata_key(channel_uuid)
                    last_activity_str = self.redis_client.hget(metadata_key, "last_activity")

                    if last_activity_str is None:
                        # No metadata - channel may have been cleaned up
                        continue

                    last_activity = float(last_activity_str)
                    inactive_seconds = current_time - last_activity

                    # Get shutdown delay from database (same setting as TS proxy)
                    shutdown_delay = self._get_shutdown_delay()

                    # Only stop if NO clients AND inactive longer than shutdown_delay
                    if inactive_seconds > shutdown_delay:
                        # Try to acquire cleanup lock (prevents race condition)
                        if not self._acquire_cleanup_lock(channel_uuid):
                            logger.debug(
                                f"HLS channel {channel_uuid} cleanup already in progress "
                                f"by another worker"
                            )
                            continue

                        try:
                            # Double-check: still no clients after acquiring lock?
                            client_count = self.redis_client.scard(clients_key) or 0
                            if client_count > 0:
                                logger.debug(f"HLS channel {channel_uuid} has {client_count} clients, skipping cleanup")
                                continue

                            # Double-check metadata still exists
                            if not self.redis_client.exists(metadata_key):
                                continue

                            # No clients and inactive - stop the session
                            logger.info(
                                f"HLS channel {channel_uuid} has no clients and inactive for "
                                f"{inactive_seconds:.1f}s (shutdown_delay={shutdown_delay}s) - stopping session"
                            )

                            # Stop the HLS session using PID from Redis
                            self._stop_session_by_pid(channel_uuid, metadata_key)
                        finally:
                            self._release_cleanup_lock(channel_uuid)

                except Exception as e:
                    logger.error(f"Error checking HLS channel {channel_uuid}: {e}")

        except Exception as e:
            logger.error(f"Error in HLS cleanup check: {e}")

    def _stop_session_by_pid(self, channel_uuid: str, metadata_key: str):
        """
        Stop an HLS session using the PID stored in Redis.

        This allows any worker to stop an FFmpeg process started by another worker.
        After killing the process, cleans up files and Redis keys.
        """
        import os
        import signal
        import shutil
        from .config import hls_config

        try:
            # Get PID from Redis
            pid_str = self.redis_client.hget(metadata_key, "pid")

            if pid_str:
                pid = int(pid_str)
                try:
                    # Try to terminate the process gracefully first
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"Sent SIGTERM to HLS FFmpeg process {pid} for channel {channel_uuid}")

                    # Give it a moment to clean up
                    import time
                    time.sleep(1)

                    # Check if still running and force kill if needed
                    try:
                        os.kill(pid, 0)  # This just checks if process exists
                        os.kill(pid, signal.SIGKILL)
                        logger.info(f"Sent SIGKILL to HLS FFmpeg process {pid}")
                    except OSError:
                        pass  # Process already dead

                except OSError as e:
                    if e.errno == 3:  # No such process
                        logger.debug(f"HLS FFmpeg process {pid} already terminated")
                    else:
                        logger.error(f"Error killing HLS FFmpeg process {pid}: {e}")
            else:
                logger.warning(f"No PID found in Redis for HLS channel {channel_uuid}")

            # Clean up HLS files
            channel_path = hls_config.get_channel_path(channel_uuid)
            if os.path.exists(channel_path):
                try:
                    shutil.rmtree(channel_path)
                    logger.info(f"Cleaned up HLS directory: {channel_path}")
                except Exception as e:
                    logger.error(f"Error cleaning up HLS directory {channel_path}: {e}")

            # Clean up Redis keys
            self.set_channel_inactive(channel_uuid)

            logger.info(f"HLS session stopped for channel {channel_uuid}")

        except Exception as e:
            logger.error(f"Error stopping HLS session {channel_uuid}: {e}")

    def _get_shutdown_delay(self) -> int:
        """
        Get the HLS-specific channel shutdown delay from the database.

        This is independent from the TS Proxy shutdown delay because HLS
        streaming has different timing characteristics:
        - HLS clients fetch segments every ~6 seconds (segment duration)
        - Between segment requests, there's naturally no activity
        - A longer shutdown delay is needed to avoid premature session termination

        Falls back to DEFAULT_INACTIVITY_TIMEOUT (30s) if database is unavailable.
        """
        try:
            from .config import hls_config
            return hls_config.shutdown_delay
        except Exception as e:
            logger.debug(f"Could not get HLS shutdown delay from config: {e}")
            return self.DEFAULT_INACTIVITY_TIMEOUT

    def get_inactivity_timeout(self) -> int:
        """Get the configured inactivity timeout in seconds."""
        return self._get_shutdown_delay()


# Global instance
hls_client_manager = HLSClientManager()

