"""HLS Output Client Manager.

Redis-backed client tracking for HLS output sessions.
Tracks connected clients using TS-compatible Redis keys so that
HLS channels appear on the Stats page alongside TS channels.
Handles TTL-based cleanup, heartbeat thread, and manages
session shutdown after the last client disconnects.
"""

import time
import json
import uuid
import threading
import logging
from typing import Dict, Optional, Set

from apps.proxy.ts_proxy.constants import ChannelMetadataField

logger = logging.getLogger(__name__)

# ----- Redis key patterns -----
# TS-compatible keys (used by Stats API & WebSocket push)
TS_CLIENT_SET_KEY = "ts_proxy:channel:{uuid}:clients"
TS_CLIENT_DATA_KEY = "ts_proxy:channel:{uuid}:clients:{client_id}"

# HLS-specific keys (kept for HLS-specific shutdown logic)
HLS_COOLDOWN_KEY = "hls_output:channel:{uuid}:cooldown"
HLS_ACTIVE_CHANNELS_KEY = "hls_output:active_channels"

# Timeouts
CLIENT_TTL = 60  # Seconds before a client record expires in Redis
HEARTBEAT_INTERVAL = 10  # Seconds between heartbeat refreshes
CLEANUP_INTERVAL = 10  # Seconds between cleanup runs
COOLDOWN_DURATION = 5  # Seconds to wait before allowing shutdown after last client


class HLSClientManager:
    """Singleton manager for HLS output client tracking.

    Uses Redis to track connected clients across multiple workers.
    Writes TS-compatible client metadata so the existing Stats API
    and WebSocket channel_stats push automatically include HLS clients.
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
        self._heartbeat_thread = None
        self._stop_event = threading.Event()
        self._shutdown_callbacks = {}  # channel_uuid -> callback
        self._worker_id = str(uuid.uuid4())[:8]
        # Local tracking: channel_uuid -> set of client_ids
        self._local_clients: Dict[str, Set[str]] = {}
        self._local_clients_lock = threading.Lock()

    @property
    def redis_client(self):
        """Lazy Redis client initialization."""
        if self._redis is None:
            try:
                import redis
                import os
                from django.conf import settings as django_settings
                host = os.environ.get("REDIS_HOST", getattr(django_settings, "REDIS_HOST", "localhost"))
                port = int(os.environ.get("REDIS_PORT", getattr(django_settings, "REDIS_PORT", 6379)))
                db = int(os.environ.get("REDIS_DB", getattr(django_settings, "REDIS_DB", 0)))
                self._redis = redis.Redis(
                    host=host,
                    port=port,
                    db=db,
                    decode_responses=False,
                )
            except Exception as e:
                logger.error("Failed to connect to Redis: %s", e)
        return self._redis

    def start(self):
        """Start the cleanup and heartbeat threads."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._stop_event.clear()

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="hls-client-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="hls-client-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        logger.info("HLS client manager started (cleanup + heartbeat threads)")

    def stop(self):
        """Stop the cleanup and heartbeat threads."""
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

    def register_shutdown_callback(self, channel_uuid: str, callback):
        """Register a callback to be called when all clients disconnect."""
        self._shutdown_callbacks[channel_uuid] = callback

    def unregister_shutdown_callback(self, channel_uuid: str):
        """Remove shutdown callback for a channel."""
        self._shutdown_callbacks.pop(channel_uuid, None)

    def add_client(
        self,
        channel_uuid: str,
        client_id: str,
        ip_address: str = "unknown",
        user_agent: str = "unknown",
    ) -> bool:
        """Register a client for a channel using TS-compatible Redis keys.

        Args:
            channel_uuid: The channel identifier.
            client_id: Unique client identifier.
            ip_address: Client IP address.
            user_agent: Client user-agent string.

        Returns:
            True if client was added successfully.
        """
        try:
            rc = self.redis_client
            if rc is None:
                return False

            now = str(time.time())

            # ---- TS-compatible client metadata hash ----
            client_data_key = TS_CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )
            client_data = {
                ChannelMetadataField.CONNECTED_AT: now,
                ChannelMetadataField.LAST_ACTIVE: now,
                ChannelMetadataField.BYTES_SENT: "0",
                ChannelMetadataField.IP_ADDRESS: ip_address,
                ChannelMetadataField.USER_AGENT: user_agent,
                ChannelMetadataField.WORKER_ID: self._worker_id,
            }
            rc.hset(client_data_key, mapping=client_data)
            rc.expire(client_data_key, CLIENT_TTL)

            # ---- TS-compatible client set ----
            client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.sadd(client_set_key, client_id)
            rc.expire(client_set_key, CLIENT_TTL * 2)

            # ---- HLS-specific active channel tracking ----
            rc.sadd(HLS_ACTIVE_CHANNELS_KEY, channel_uuid)

            # Clear any cooldown
            cooldown_key = HLS_COOLDOWN_KEY.format(uuid=channel_uuid)
            rc.delete(cooldown_key)

            # Track locally for heartbeat
            with self._local_clients_lock:
                if channel_uuid not in self._local_clients:
                    self._local_clients[channel_uuid] = set()
                self._local_clients[channel_uuid].add(client_id)

            # Trigger channel_stats WebSocket push
            self._trigger_stats_update()

            # Log system event for client connect
            self._log_client_event(
                "client_connect", channel_uuid,
                client_id=client_id, ip_address=ip_address,
                user_agent=user_agent, stream_type="hls",
            )

            return True
        except Exception as e:
            logger.error("Failed to add client %s to channel %s: %s", client_id, channel_uuid, e)
            return False

    def update_client_activity(
        self,
        channel_uuid: str,
        client_id: str,
        bytes_sent: int = 0,
    ):
        """Update client activity timestamp and bytes_sent.

        Called on every playlist/segment request.
        """
        try:
            rc = self.redis_client
            if rc is None:
                return

            now = str(time.time())
            client_data_key = TS_CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )

            updates = {
                ChannelMetadataField.LAST_ACTIVE: now,
            }
            if bytes_sent > 0:
                # Increment bytes_sent
                rc.hincrby(client_data_key, ChannelMetadataField.BYTES_SENT, bytes_sent)
                # Remove from updates dict since we used hincrby
            rc.hset(client_data_key, mapping=updates)
            rc.expire(client_data_key, CLIENT_TTL)

            # Refresh the client set TTL
            client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.expire(client_set_key, CLIENT_TTL * 2)
        except Exception as e:
            logger.debug("Failed to update client activity: %s", e)

    def remove_client(self, channel_uuid: str, client_id: str):
        """Remove a client from a channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return

            # Remove from TS-compatible client set
            client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
            rc.srem(client_set_key, client_id)

            # Remove TS-compatible client data hash
            client_data_key = TS_CLIENT_DATA_KEY.format(
                uuid=channel_uuid, client_id=client_id
            )
            rc.delete(client_data_key)

            # Remove from local tracking
            with self._local_clients_lock:
                clients = self._local_clients.get(channel_uuid)
                if clients:
                    clients.discard(client_id)
                    if not clients:
                        del self._local_clients[channel_uuid]

            # Trigger channel_stats WebSocket push
            self._trigger_stats_update()

            # Log system event for client disconnect
            self._log_client_event(
                "client_disconnect", channel_uuid,
                client_id=client_id, stream_type="hls",
            )
        except Exception as e:
            logger.error("Failed to remove client %s from channel %s: %s", client_id, channel_uuid, e)

    def get_client_count(self, channel_uuid: str) -> int:
        """Get the number of active clients for a channel."""
        try:
            rc = self.redis_client
            if rc is None:
                return 0
            client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
            return rc.scard(client_set_key) or 0
        except Exception:
            return 0

    def get_total_client_count(self) -> int:
        """Get total client count across all HLS channels."""
        try:
            rc = self.redis_client
            if rc is None:
                return 0
            channels = rc.smembers(HLS_ACTIVE_CHANNELS_KEY)
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
            cooldown_key = HLS_COOLDOWN_KEY.format(uuid=channel_uuid)
            rc.delete(cooldown_key)
        except Exception:
            pass

    def cleanup_channel(self, channel_uuid: str):
        """Full cleanup of a channel from the client manager on session stop.

        Removes the channel from the HLS active channels set and clears
        all local tracking state.
        """
        try:
            rc = self.redis_client
            if rc is not None:
                # Remove from HLS active channels set
                rc.srem(HLS_ACTIVE_CHANNELS_KEY, channel_uuid)
                # Clear cooldown
                cooldown_key = HLS_COOLDOWN_KEY.format(uuid=channel_uuid)
                rc.delete(cooldown_key)
        except Exception as e:
            logger.debug("Failed to clean up channel %s from client manager: %s", channel_uuid, e)

        # Remove from local tracking
        with self._local_clients_lock:
            self._local_clients.pop(channel_uuid, None)

    # ------------------------------------------------------------------ #
    #  Heartbeat thread
    # ------------------------------------------------------------------ #

    def _heartbeat_loop(self):
        """Periodically refresh TTLs on client keys for locally-tracked clients."""
        while not self._stop_event.is_set():
            try:
                self._refresh_client_ttls()
            except Exception as e:
                logger.error("HLS heartbeat error: %s", e)
            self._stop_event.wait(HEARTBEAT_INTERVAL)

    def _refresh_client_ttls(self):
        """Refresh TTL on all locally-tracked client hashes and sets."""
        rc = self.redis_client
        if rc is None:
            return

        with self._local_clients_lock:
            channels_snapshot = {
                ch: set(clients) for ch, clients in self._local_clients.items()
            }

        if not channels_snapshot:
            return

        pipe = rc.pipeline()
        for channel_uuid, client_ids in channels_snapshot.items():
            client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
            for client_id in client_ids:
                client_data_key = TS_CLIENT_DATA_KEY.format(
                    uuid=channel_uuid, client_id=client_id
                )
                # Only refresh TTL, don't update last_active
                pipe.expire(client_data_key, CLIENT_TTL)
                pipe.sadd(client_set_key, client_id)
            pipe.expire(client_set_key, CLIENT_TTL * 2)
        try:
            pipe.execute()
        except Exception as e:
            logger.debug("Heartbeat pipeline error: %s", e)

    # ------------------------------------------------------------------ #
    #  Cleanup thread
    # ------------------------------------------------------------------ #

    def _cleanup_loop(self):
        """Periodically clean up inactive clients."""
        while not self._stop_event.is_set():
            try:
                self._cleanup_inactive_clients()
            except Exception as e:
                logger.error("Client cleanup error: %s", e)
            self._stop_event.wait(CLEANUP_INTERVAL)

    def _cleanup_inactive_clients(self):
        """Remove clients whose data keys have expired (ghost clients)."""
        rc = self.redis_client
        if rc is None:
            return

        try:
            channels = rc.smembers(HLS_ACTIVE_CHANNELS_KEY)
            if not channels:
                return

            for ch in channels:
                channel_uuid = ch.decode("utf-8") if isinstance(ch, bytes) else ch
                client_set_key = TS_CLIENT_SET_KEY.format(uuid=channel_uuid)
                clients = rc.smembers(client_set_key)

                if not clients:
                    # No clients - check cooldown for shutdown
                    self._handle_empty_channel(channel_uuid)
                    continue

                # Check each client's data key existence (TTL-based expiry)
                stale_ids = []
                for client_id_raw in clients:
                    client_id = (
                        client_id_raw.decode("utf-8")
                        if isinstance(client_id_raw, bytes)
                        else client_id_raw
                    )
                    client_data_key = TS_CLIENT_DATA_KEY.format(
                        uuid=channel_uuid, client_id=client_id
                    )
                    if not rc.exists(client_data_key):
                        stale_ids.append(client_id_raw)

                # Remove stale entries from set
                if stale_ids:
                    rc.srem(client_set_key, *stale_ids)
                    # Also remove from local tracking
                    with self._local_clients_lock:
                        local_set = self._local_clients.get(channel_uuid)
                        if local_set:
                            for sid in stale_ids:
                                s = sid.decode("utf-8") if isinstance(sid, bytes) else sid
                                local_set.discard(s)

                # Re-check if channel has clients after cleanup
                remaining = rc.scard(client_set_key) or 0
                if remaining == 0:
                    self._handle_empty_channel(channel_uuid)
                    self._trigger_stats_update()

        except Exception as e:
            logger.error("Cleanup error: %s", e)

    def _handle_empty_channel(self, channel_uuid: str):
        """Handle a channel with no active clients - start cooldown or trigger shutdown."""
        rc = self.redis_client
        if rc is None:
            return

        cooldown_key = HLS_COOLDOWN_KEY.format(uuid=channel_uuid)

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
                rc.srem(HLS_ACTIVE_CHANNELS_KEY, channel_uuid)

    # ------------------------------------------------------------------ #
    #  WebSocket stats push (channel_stats)
    # ------------------------------------------------------------------ #

    def _trigger_stats_update(self):
        """Trigger a channel_stats WebSocket update in a background thread.

        Uses the same pattern as the TS proxy ClientManager: scans all
        ts_proxy:channel:*:clients keys, builds channel info, and pushes
        a channel_stats message through the 'updates' WebSocket group.
        """
        threading.Thread(target=self._do_stats_update, daemon=True).start()

    def _do_stats_update(self):
        """Perform the stats update (runs in background thread)."""
        try:
            from apps.proxy.ts_proxy.channel_status import ChannelStatus
            import redis
            from django.conf import settings

            redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
            redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
            all_channels = []
            cursor = 0

            while True:
                cursor, keys = redis_client.scan(
                    cursor, match="ts_proxy:channel:*:clients", count=100
                )
                for key in keys:
                    parts = key.split(":")
                    if len(parts) >= 4:
                        ch_id = parts[2]
                        channel_info = ChannelStatus.get_basic_channel_info(ch_id)
                        if channel_info:
                            all_channels.append(channel_info)

                if cursor == 0:
                    break

            from core.utils import send_websocket_update

            send_websocket_update(
                "updates",
                "update",
                {
                    "success": True,
                    "type": "channel_stats",
                    "stats": json.dumps(
                        {"channels": all_channels, "count": len(all_channels)}
                    ),
                },
            )
        except Exception as e:
            logger.debug("Failed to trigger stats update: %s", e)

    # ------------------------------------------------------------------ #
    #  System event logging
    # ------------------------------------------------------------------ #

    @staticmethod
    def _log_client_event(event_type: str, channel_uuid: str, **kwargs):
        """Log a system event for an HLS client lifecycle event.

        Runs in a background thread to avoid blocking the request path.
        """
        def _do_log():
            try:
                from core.utils import log_system_event

                channel_name = None
                try:
                    from apps.channels.models import Channel
                    ch = Channel.objects.filter(uuid=channel_uuid).first()
                    if ch:
                        channel_name = ch.name
                except Exception:
                    pass

                log_system_event(
                    event_type,
                    channel_id=channel_uuid,
                    channel_name=channel_name,
                    **kwargs,
                )
            except Exception as e:
                logger.debug("Failed to log system event %s: %s", event_type, e)

        threading.Thread(target=_do_log, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Legacy WebSocket push (hls_client_update) — kept for backward compat
    # ------------------------------------------------------------------ #

    def _send_websocket_update(self, channel_uuid: str):
        """Send a WebSocket update about client count changes (legacy)."""
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
