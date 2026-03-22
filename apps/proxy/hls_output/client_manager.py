"""HLS Output Client Manager.

Redis-backed client tracking for HLS output sessions.
Tracks connected clients, handles TTL-based cleanup, and manages
session shutdown after the last client disconnects.
"""

import time
import json
import uuid
import threading
import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# Redis key patterns
CLIENT_SET_KEY = "hls_output:channel:{uuid}:clients"
CLIENT_DATA_KEY = "hls_output:channel:{uuid}:client:{client_id}"
COOLDOWN_KEY = "hls_output:channel:{uuid}:cooldown"
ACTIVE_CHANNELS_KEY = "hls_output:active_channels"

# Timeouts
CLIENT_TTL = 30  # Seconds before a client is considered inactive
CLEANUP_INTERVAL = 10  # Seconds between cleanup runs
COOLDOWN_DURATION = 5  # Seconds to wait before allowing shutdown after last client


class HLSClientManager:
    """Singleton manager for HLS output client tracking.

    Uses Redis to track connected clients across multiple workers.
    Automatically detects inactive clients and triggers session shutdown.
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
        self._redis = None
        self._cleanup_thread = None
        self._stop_event = threading.Event()
        self._shutdown_callbacks = {}  # channel_uuid -> callback

    @property
    def redis_client(self):
        """Lazy Redis client initialization."""
        if self._redis is None:
            try:
                import redis
                self._redis = redis.Redis(
                    host="redis",
                    port=6379,
                    db=0,
                    decode_responses=False,
                )
            except Exception as e:
                logger.error("Failed to connect to Redis: %s", e)
        return self._redis

    def start(self):
        """Start the cleanup thread."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="hls-client-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()
        logger.info("HLS client manager cleanup thread started")

    def stop(self):
        """Stop the cleanup thread."""
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None

    def register_shutdown_callback(self, channel_uuid: str, callback):
        """Register a callback to be called when all clients disconnect."""
        self._shutdown_callbacks[channel_uuid] = callback

    def unregister_shutdown_callback(self, channel_uuid: str):
        """Remove shutdown callback for a channel."""
        self._shutdown_callbacks.pop(channel_uuid, None)

    def add_client(self, channel_uuid: str, client_id: str) -> bool:
        """Register a client for a channel.

        Args:
            channel_uuid: The channel identifier.
            client_id: Unique client identifier.

        Returns:
            True if client was added successfully.
        """
        try:
            rc = self.redis_client
            if rc is None:
                return False

            now = time.time()
            # Add to channel's client set
            client_set_key = CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.sadd(client_set_key, client_id)
            rc.expire(client_set_key, CLIENT_TTL * 2)

            # Store client data with activity timestamp
            client_data_key = CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )
            rc.setex(
                client_data_key,
                CLIENT_TTL,
                json.dumps({"last_activity": now}).encode("utf-8"),
            )

            # Track as active channel
            rc.sadd(ACTIVE_CHANNELS_KEY, channel_uuid)

            # Clear any cooldown
            cooldown_key = COOLDOWN_KEY.format(uuid=channel_uuid)
            rc.delete(cooldown_key)

            self._send_websocket_update(channel_uuid)
            return True
        except Exception as e:
            logger.error("Failed to add client %s to channel %s: %s", client_id, channel_uuid, e)
            return False

    def update_client_activity(self, channel_uuid: str, client_id: str):
        """Update client activity timestamp (called on segment requests)."""
        try:
            rc = self.redis_client
            if rc is None:
                return

            now = time.time()
            client_data_key = CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )
            rc.setex(
                client_data_key,
                CLIENT_TTL,
                json.dumps({"last_activity": now}).encode("utf-8"),
            )

            # Refresh the client set TTL
            client_set_key = CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.expire(client_set_key, CLIENT_TTL * 2)
        except Exception as e:
            logger.debug("Failed to update client activity: %s", e)

    def remove_client(self, channel_uuid: str, client_id: str):
        """Remove a client from a channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return

            client_set_key = CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.srem(client_set_key, client_id)

            client_data_key = CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )
            rc.delete(client_data_key)

            self._send_websocket_update(channel_uuid)
        except Exception as e:
            logger.error("Failed to remove client %s from channel %s: %s", client_id, channel_uuid, e)

    def get_client_count(self, channel_uuid: str) -> int:
        """Get the number of active clients for a channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return 0
            client_set_key = CLIENT_SET_KEY.format(uuid=channel_uuid)
            return rc.scard(client_set_key) or 0
        except Exception:
            return 0

    def get_total_client_count(self) -> int:
        """Get total client count across all channels."""
        try:
            rc = self.redis_client
            if rc is None:
                return 0
            channels = rc.smembers(ACTIVE_CHANNELS_KEY)
            total = 0
            for ch in channels:
                ch_uuid = ch.decode("utf-8") if isinstance(ch, bytes) else ch
                total += self.get_client_count(ch_uuid)
            return total
        except Exception:
            return 0

    def is_channel_active(self, channel_uuid: str) -> bool:
        """Check if a channel has active clients."""
        return self.get_client_count(channel_uuid) > 0

    def clear_cooldown(self, channel_uuid: str):
        """Clear the shutdown cooldown for a channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return
            cooldown_key = COOLDOWN_KEY.format(uuid=channel_uuid)
            rc.delete(cooldown_key)
        except Exception:
            pass

    def _cleanup_loop(self):
        """Periodically clean up inactive clients."""
        while not self._stop_event.is_set():
            try:
                self._cleanup_inactive_clients()
            except Exception as e:
                logger.error("Client cleanup error: %s", e)
            self._stop_event.wait(CLEANUP_INTERVAL)

    def _cleanup_inactive_clients(self):
        """Remove clients that haven't been active within TTL."""
        rc = self.redis_client
        if rc is None:
            return

        try:
            channels = rc.smembers(ACTIVE_CHANNELS_KEY)
            if not channels:
                return

            for ch in channels:
                channel_uuid = ch.decode("utf-8") if isinstance(ch, bytes) else ch
                client_set_key = CLIENT_SET_KEY.format(uuid=channel_uuid)
                clients = rc.smembers(client_set_key)

                if not clients:
                    # No clients - check cooldown for shutdown
                    self._handle_empty_channel(channel_uuid)
                    continue

                # Check each client's TTL (Redis auto-expires client data keys)
                for client_id_raw in clients:
                    client_id = (
                        client_id_raw.decode("utf-8")
                        if isinstance(client_id_raw, bytes)
                        else client_id_raw
                    )
                    client_data_key = CLIENT_DATA_KEY.format(
                        uuid=channel_uuid, client_id=client_id
                    )
                    if not rc.exists(client_data_key):
                        # Client data expired - remove from set
                        rc.srem(client_set_key, client_id)

                # Re-check if channel has clients after cleanup
                remaining = rc.scard(client_set_key) or 0
                if remaining == 0:
                    self._handle_empty_channel(channel_uuid)
                    self._send_websocket_update(channel_uuid)

        except Exception as e:
            logger.error("Cleanup error: %s", e)

    def _handle_empty_channel(self, channel_uuid: str):
        """Handle a channel with no active clients - start cooldown or trigger shutdown."""
        rc = self.redis_client
        if rc is None:
            return

        cooldown_key = COOLDOWN_KEY.format(uuid=channel_uuid)

        if not rc.exists(cooldown_key):
            # Start cooldown
            from .config import hls_config

            delay = hls_config.shutdown_delay
            rc.setex(cooldown_key, delay, "1")
            logger.info(
                "Channel %s has no clients, starting %ds shutdown cooldown",
                channel_uuid, delay,
            )
        else:
            # Cooldown expired (key still exists means within TTL) - check if expired
            ttl = rc.ttl(cooldown_key)
            if ttl <= 0:
                # Cooldown expired - trigger shutdown
                callback = self._shutdown_callbacks.get(channel_uuid)
                if callback:
                    logger.info("Channel %s shutdown cooldown expired, stopping session", channel_uuid)
                    try:
                        callback()
                    except Exception as e:
                        logger.error("Shutdown callback error for %s: %s", channel_uuid, e)
                # Remove from active channels
                rc.srem(ACTIVE_CHANNELS_KEY, channel_uuid)

    def _send_websocket_update(self, channel_uuid: str):
        """Send a WebSocket update about client count changes."""
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            if channel_layer:
                count = self.get_client_count(channel_uuid)
                async_to_sync(channel_layer.group_send)(
                    "updates",
                    {
                        "type": "hls_client_update",
                        "channel_uuid": channel_uuid,
                        "client_count": count,
                    },
                )
        except Exception as e:
            logger.debug("WebSocket update failed: %s", e)


# Singleton instance
hls_client_manager = HLSClientManager()
