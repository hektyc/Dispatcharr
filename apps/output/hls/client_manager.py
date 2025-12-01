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
    CLIENT_TTL = 60  # Seconds before client is considered disconnected
    HEARTBEAT_INTERVAL = 10  # Seconds between heartbeat updates
    CLEANUP_CHECK_INTERVAL = 5  # Seconds between cleanup checks
    INACTIVITY_TIMEOUT = 30  # Seconds of no client activity before stopping session

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
                self.redis_client.sadd(clients_key, client_id)
                self.redis_client.expire(clients_key, self.CLIENT_TTL)
                
                # Store client metadata
                client_key = self._get_client_key(channel_uuid, client_id)
                self.redis_client.hset(client_key, mapping={
                    "ip_address": client_ip,
                    "user_agent": user_agent or "Unknown",
                    "connected_at": current_time,
                    "last_active": current_time,
                })
                self.redis_client.expire(client_key, self.CLIENT_TTL)
                
                # Update channel metadata
                metadata_key = self._get_channel_metadata_key(channel_uuid)
                self.redis_client.hset(metadata_key, mapping={
                    "state": "running",
                    "type": "hls",
                    "last_activity": current_time,
                })
                self.redis_client.expire(metadata_key, self.CLIENT_TTL * 2)
            
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

                # Update client last_active
                client_key = self._get_client_key(channel_uuid, client_id)
                self.redis_client.hset(client_key, "last_active", current_time)
                self.redis_client.expire(client_key, self.CLIENT_TTL)

                # Refresh client set TTL
                clients_key = self._get_channel_clients_key(channel_uuid)
                self.redis_client.expire(clients_key, self.CLIENT_TTL)

                # Update channel metadata
                metadata_key = self._get_channel_metadata_key(channel_uuid)
                self.redis_client.hset(metadata_key, "last_activity", current_time)
                self.redis_client.expire(metadata_key, self.CLIENT_TTL * 2)

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
        """Get detailed info for a channel including clients."""
        try:
            if not self.redis_client:
                return None

            metadata_key = self._get_channel_metadata_key(channel_uuid)
            metadata = self.redis_client.hgetall(metadata_key)

            if not metadata:
                return None

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
            init_time = float(metadata.get("init_time", time.time()))
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

            return {
                "channel_id": channel_uuid,
                "channel_name": channel_name,
                "state": metadata.get("state", "unknown"),
                "type": "hls",
                "client_count": client_count,
                "clients": clients,
                "uptime": uptime,
                "started_at": metadata.get("init_time", "0"),
            }

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

    def set_channel_active(self, channel_uuid: str, stream_url: str = None, pid: int = None):
        """Mark a channel as having an active HLS session.

        Args:
            channel_uuid: The channel UUID
            stream_url: The stream URL being processed
            pid: The FFmpeg process ID (critical for multi-worker cleanup)
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

                self.redis_client.hset(metadata_key, mapping=mapping)
                self.redis_client.expire(metadata_key, self.CLIENT_TTL * 10)

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
        2. No clients have made requests within INACTIVITY_TIMEOUT seconds

        The client activity is tracked by the TTL on Redis keys - if the client
        keys have expired, it means no recent requests were made.
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
                    # Get the channel's last activity time
                    metadata_key = self._get_channel_metadata_key(channel_uuid)
                    last_activity_str = self.redis_client.hget(metadata_key, "last_activity")

                    if last_activity_str is None:
                        # No metadata - channel may have been cleaned up
                        continue

                    last_activity = float(last_activity_str)
                    inactive_seconds = current_time - last_activity

                    # Check if there are any active clients
                    clients_key = self._get_channel_clients_key(channel_uuid)
                    client_count = self.redis_client.scard(clients_key) or 0

                    # Also check if any individual client keys still exist (TTL-based)
                    # This is a more accurate check since client keys expire with TTL
                    if client_count > 0:
                        # Verify clients are actually active (keys haven't expired)
                        client_ids = list(self.redis_client.smembers(clients_key) or set())
                        active_clients = 0
                        for client_id in client_ids:
                            client_key = self._get_client_key(channel_uuid, client_id)
                            if self.redis_client.exists(client_key):
                                active_clients += 1
                        client_count = active_clients

                    if client_count == 0 and inactive_seconds > self.INACTIVITY_TIMEOUT:
                        # No active clients and no recent activity - stop the session
                        logger.info(
                            f"HLS channel {channel_uuid} inactive for {inactive_seconds:.1f}s "
                            f"with no clients - stopping session"
                        )

                        # Stop the HLS session using PID from Redis
                        # This works across workers since PID is stored in Redis
                        self._stop_session_by_pid(channel_uuid, metadata_key)

                    elif client_count == 0 and inactive_seconds > 5:
                        # Log a warning that we're tracking inactivity
                        logger.debug(
                            f"HLS channel {channel_uuid} no clients, "
                            f"inactive for {inactive_seconds:.1f}s "
                            f"(will stop after {self.INACTIVITY_TIMEOUT}s)"
                        )

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

    def get_inactivity_timeout(self) -> int:
        """Get the configured inactivity timeout in seconds."""
        return self.INACTIVITY_TIMEOUT


# Global instance
hls_client_manager = HLSClientManager()

