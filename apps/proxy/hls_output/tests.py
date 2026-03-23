"""Tests for HLS Output module.

Covers lifecycle, client tracking, cleanup, and ownership remediation
behavior introduced by the HLS Output Comprehensive Remediation Plan.
"""

import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock

from django.test import TestCase, RequestFactory


class HLSOutputConfigTest(TestCase):
    """Tests for HLS output configuration."""

    def test_config_defaults(self):
        from .config import HLSOutputConfig, HLS_OUTPUT_DEFAULTS

        config = HLSOutputConfig()
        # When HLS_PATH is not set, module should be disabled
        # (depends on environment - in test this checks graceful handling)
        self.assertIsNotNone(config.segment_duration)
        self.assertIsNotNone(config.playlist_size)

    def test_config_to_dict(self):
        from .config import HLSOutputConfig

        config = HLSOutputConfig()
        data = config.to_dict()
        self.assertIn("segment_duration", data)
        self.assertIn("playlist_size", data)
        self.assertIn("storage_backend", data)
        self.assertIn("is_enabled", data)


class SegmentStoreBaseTest(TestCase):
    """Tests for the SegmentStore base interface."""

    def test_filesystem_store_init(self):
        from .storage.filesystem_store import FilesystemSegmentStore

        store = FilesystemSegmentStore("/tmp/test_hls")
        self.assertEqual(store.base_path, "/tmp/test_hls")

    def test_redis_store_init(self):
        """Test RedisSegmentStore can be instantiated with a mock."""
        mock_redis = MagicMock()
        from .storage.redis_store import RedisSegmentStore

        store = RedisSegmentStore(mock_redis, ttl=60)
        self.assertEqual(store._ttl, 60)


# ------------------------------------------------------------------ #
#  Task 1.2: Client ID uniqueness
# ------------------------------------------------------------------ #

class ClientIDGenerationTest(TestCase):
    """Tests for unique per-session client ID generation."""

    def test_get_client_id_with_cid_query_param(self):
        """When cid is present in the query string, it should be returned as-is."""
        from .views import _get_client_id

        factory = RequestFactory()
        request = factory.get("/fake?cid=abc123")
        self.assertEqual(_get_client_id(request), "abc123")

    def test_get_client_id_generates_unique_ids(self):
        """Without cid, each call should produce a unique ID."""
        from .views import _get_client_id

        factory = RequestFactory()
        req1 = factory.get("/fake")
        req2 = factory.get("/fake")
        id1 = _get_client_id(req1)
        id2 = _get_client_id(req2)
        self.assertNotEqual(id1, id2)
        self.assertTrue(id1.startswith("hls_"))

    def test_same_ip_ua_different_ids(self):
        """Two requests with the same IP and UA should still get different IDs."""
        from .views import _get_client_id

        factory = RequestFactory()
        req1 = factory.get("/fake", HTTP_USER_AGENT="VLC", REMOTE_ADDR="1.2.3.4")
        req2 = factory.get("/fake", HTTP_USER_AGENT="VLC", REMOTE_ADDR="1.2.3.4")
        self.assertNotEqual(_get_client_id(req1), _get_client_id(req2))


# ------------------------------------------------------------------ #
#  Task 1.4: Atomic metadata deletion
# ------------------------------------------------------------------ #

class AtomicMetadataDeletionTest(TestCase):
    """Tests for pipeline-based atomic metadata deletion."""

    def test_delete_unified_metadata_uses_pipeline(self):
        """_delete_unified_metadata should use a Redis pipeline."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_redis.scan.return_value = (0, [b"ts_proxy:channel:test:clients:c1"])

        from .session import HLSSession

        session = HLSSession("test-uuid", MagicMock())
        session._redis = mock_redis

        session._delete_unified_metadata()

        # Verify pipeline was used
        mock_redis.pipeline.assert_called_once_with(transaction=True)
        # Pipeline should have delete calls
        self.assertTrue(mock_pipe.delete.called)
        mock_pipe.execute.assert_called_once()


# ------------------------------------------------------------------ #
#  Task 4.1: Proxy session flag
# ------------------------------------------------------------------ #

class ProxySessionTest(TestCase):
    """Tests for the is_proxy flag on HLSSession."""

    def test_session_defaults_to_not_proxy(self):
        from .session import HLSSession

        session = HLSSession("test-uuid", MagicMock())
        self.assertFalse(session.is_proxy)

    def test_session_proxy_flag(self):
        from .session import HLSSession

        session = HLSSession("test-uuid", MagicMock(), is_proxy=True)
        self.assertTrue(session.is_proxy)

    def test_proxy_session_start_returns_false(self):
        """A proxy session should not start FFmpeg."""
        from .session import HLSSession

        session = HLSSession("test-uuid", MagicMock(), is_proxy=True)
        result = session.start("http://example.com/stream", "VLC/3.0")
        self.assertFalse(result)

    def test_proxy_session_stop_is_noop(self):
        """Stopping a proxy session should not attempt FFmpeg shutdown."""
        from .session import HLSSession

        session = HLSSession("test-uuid", MagicMock(), is_proxy=True)
        # Should not raise
        session.stop()


# ------------------------------------------------------------------ #
#  Task 1.1: Ownership refresh
# ------------------------------------------------------------------ #

class OwnershipRefreshTest(TestCase):
    """Tests for the ownership refresh mechanism in HLSOutputManager."""

    def _make_manager(self):
        """Create a fresh HLSOutputManager instance for testing."""
        from .manager import HLSOutputManager

        # Reset singleton for isolated tests
        HLSOutputManager._instance = None
        manager = HLSOutputManager()
        manager._redis = MagicMock()
        return manager

    def test_refresh_ownership_extends_ttl(self):
        """When we still own the key, refresh should extend TTL."""
        manager = self._make_manager()
        manager._redis.get.return_value = manager._worker_id.encode("utf-8")

        result = manager._refresh_ownership("test-uuid")

        self.assertTrue(result)
        manager._redis.expire.assert_called_once()

    def test_refresh_ownership_detects_lost_key(self):
        """When key expired and another worker took it, return False."""
        manager = self._make_manager()
        manager._redis.get.return_value = None
        manager._redis.set.return_value = False  # Another worker took it

        result = manager._refresh_ownership("test-uuid")

        self.assertFalse(result)

    def test_refresh_ownership_reacquires_expired_key(self):
        """When key expired and we re-acquire it, return True."""
        manager = self._make_manager()
        manager._redis.get.return_value = None
        manager._redis.set.return_value = True  # Re-acquired

        result = manager._refresh_ownership("test-uuid")

        self.assertTrue(result)

    def test_refresh_ownership_detects_different_owner(self):
        """When another worker owns the key, return False."""
        manager = self._make_manager()
        manager._redis.get.return_value = b"other-worker"

        result = manager._refresh_ownership("test-uuid")

        self.assertFalse(result)

    def tearDown(self):
        # Reset singleton
        from .manager import HLSOutputManager
        HLSOutputManager._instance = None


# ------------------------------------------------------------------ #
#  Task 1.1 / 3.3: Cleanup cycle
# ------------------------------------------------------------------ #

class CleanupCycleTest(TestCase):
    """Tests for the cleanup/heartbeat cycle."""

    def _make_manager(self):
        from .manager import HLSOutputManager

        HLSOutputManager._instance = None
        manager = HLSOutputManager()
        manager._redis = MagicMock()
        return manager

    def test_cleanup_cycle_sends_worker_heartbeat(self):
        """The cleanup cycle should send a worker heartbeat to Redis."""
        manager = self._make_manager()

        manager._cleanup_cycle()

        manager._redis.setex.assert_called()
        # First call should be heartbeat
        call_args = manager._redis.setex.call_args_list[0]
        self.assertIn("hls_output:worker:", call_args[0][0])

    def test_cleanup_cycle_skips_proxy_sessions(self):
        """Proxy sessions should not have ownership refreshed."""
        manager = self._make_manager()

        mock_session = MagicMock()
        mock_session.is_proxy = True
        manager._sessions["proxy-ch"] = mock_session

        # Should not call _refresh_ownership for proxy sessions
        with patch.object(manager, "_refresh_ownership") as mock_refresh:
            manager._cleanup_cycle()
            mock_refresh.assert_not_called()

    def tearDown(self):
        from .manager import HLSOutputManager
        HLSOutputManager._instance = None


# ------------------------------------------------------------------ #
#  Task 3.1: Precise shutdown delay
# ------------------------------------------------------------------ #

class PreciseShutdownDelayTest(TestCase):
    """Tests for the precise cooldown-based shutdown delay."""

    def _make_client_manager(self):
        """Create a fresh HLSClientManager for testing."""
        from .client_manager import HLSClientManager

        HLSClientManager._instance = None
        cm = HLSClientManager()
        cm._redis = MagicMock()
        return cm

    def test_cooldown_stores_timestamp(self):
        """First call to _handle_empty_channel should store current time."""
        cm = self._make_client_manager()
        cm._redis.get.return_value = None  # No cooldown yet

        with patch("apps.proxy.hls_output.client_manager.hls_config") as mock_config:
            mock_config.shutdown_delay = 30
            cm._handle_empty_channel("test-uuid")

        # Should call setex with a time value
        cm._redis.setex.assert_called_once()
        call_args = cm._redis.setex.call_args[0]
        # Value should be a timestamp string
        float(call_args[2])  # Should not raise

    def test_cooldown_triggers_shutdown_after_delay(self):
        """Second call after delay should trigger shutdown callback."""
        cm = self._make_client_manager()
        # Simulate cooldown started 31 seconds ago
        cooldown_start = str(time.time() - 31)
        cm._redis.get.return_value = cooldown_start.encode("utf-8")

        mock_callback = MagicMock()
        cm._shutdown_callbacks["test-uuid"] = mock_callback

        with patch("apps.proxy.hls_output.client_manager.hls_config") as mock_config:
            mock_config.shutdown_delay = 30
            cm._handle_empty_channel("test-uuid")

        mock_callback.assert_called_once()

    def test_cooldown_does_not_trigger_before_delay(self):
        """Second call before delay should NOT trigger shutdown."""
        cm = self._make_client_manager()
        # Simulate cooldown started 10 seconds ago
        cooldown_start = str(time.time() - 10)
        cm._redis.get.return_value = cooldown_start.encode("utf-8")

        mock_callback = MagicMock()
        cm._shutdown_callbacks["test-uuid"] = mock_callback

        with patch("apps.proxy.hls_output.client_manager.hls_config") as mock_config:
            mock_config.shutdown_delay = 30
            cm._handle_empty_channel("test-uuid")

        mock_callback.assert_not_called()

    def tearDown(self):
        from .client_manager import HLSClientManager
        HLSClientManager._instance = None


# ------------------------------------------------------------------ #
#  Task 1.3: Stream connection allocation
# ------------------------------------------------------------------ #

class StreamConnectionAllocationTest(TestCase):
    """Tests for stream connection allocation/release."""

    def _make_manager(self):
        from .manager import HLSOutputManager

        HLSOutputManager._instance = None
        manager = HLSOutputManager()
        manager._redis = MagicMock()
        return manager

    def test_stop_session_releases_stream(self):
        """stop_session should call channel.release_stream()."""
        manager = self._make_manager()

        mock_session = MagicMock()
        mock_session.is_proxy = False
        mock_session.is_running = True
        manager._sessions["test-uuid"] = mock_session

        mock_channel = MagicMock()
        manager._allocated_streams["test-uuid"] = {
            "channel": mock_channel,
            "stream_id": 1,
        }

        with patch.object(manager, "_release_ownership"), \
             patch("apps.proxy.hls_output.manager.hls_client_manager"):
            manager.stop_session("test-uuid")

        mock_channel.release_stream.assert_called_once()

    def test_stop_proxy_session_does_not_release_stream(self):
        """Stopping a proxy session should not release stream connections."""
        manager = self._make_manager()

        mock_session = MagicMock()
        mock_session.is_proxy = True
        manager._sessions["test-uuid"] = mock_session

        mock_channel = MagicMock()
        # This should NOT be accessed for proxy sessions
        manager._allocated_streams["test-uuid"] = {
            "channel": mock_channel,
            "stream_id": 1,
        }

        with patch("apps.proxy.hls_output.manager.hls_client_manager"):
            manager.stop_session("test-uuid")

        # Proxy sessions still pop from _allocated_streams but don't call release
        # Actually, the proxy flag prevents release_stream from being called
        # The alloc_info is popped but release_stream is gated on `not is_proxy`
        # The mock_channel should NOT have been called
        mock_channel.release_stream.assert_not_called()

    def tearDown(self):
        from .manager import HLSOutputManager
        HLSOutputManager._instance = None


# ------------------------------------------------------------------ #
#  Task 4.2: Watcher lock
# ------------------------------------------------------------------ #

class WatcherLockTest(TestCase):
    """Tests for the watcher Redis lock to prevent duplicates."""

    def test_watcher_lock_acquired(self):
        """_start_watcher_with_lock should acquire lock before starting watcher."""
        from .session import HLSSession

        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # Lock acquired

        session = HLSSession("test-uuid", MagicMock(), worker_id="w1")
        session._redis = mock_redis

        with patch("apps.proxy.hls_output.session.FileWatcher") as MockWatcher:
            mock_watcher_instance = MagicMock()
            MockWatcher.return_value = mock_watcher_instance

            session._start_watcher_with_lock("/tmp/test")

        MockWatcher.assert_called_once()
        mock_watcher_instance.start.assert_called_once()
        self.assertTrue(session._watcher_lock_held)

    def test_watcher_lock_not_acquired(self):
        """If lock is already held, watcher should not start."""
        from .session import HLSSession

        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # Lock NOT acquired

        session = HLSSession("test-uuid", MagicMock(), worker_id="w1")
        session._redis = mock_redis

        with patch("apps.proxy.hls_output.session.FileWatcher") as MockWatcher:
            session._start_watcher_with_lock("/tmp/test")

        MockWatcher.assert_not_called()
        self.assertFalse(session._watcher_lock_held)

    def test_watcher_lock_released_on_stop(self):
        """_stop_watcher should release the Redis lock."""
        from .session import HLSSession

        mock_redis = MagicMock()
        mock_redis.get.return_value = b"w1"

        session = HLSSession("test-uuid", MagicMock(), worker_id="w1")
        session._redis = mock_redis
        session._watcher = MagicMock()
        session._watcher_lock_held = True

        session._stop_watcher()

        mock_redis.delete.assert_called()
        self.assertFalse(session._watcher_lock_held)


# ------------------------------------------------------------------ #
#  Task 2.2: Stats update in _do_stats_update skips deleted channels
# ------------------------------------------------------------------ #

class StatsUpdateRaceGuardTest(TestCase):
    """Tests for the metadata-existence check in _do_stats_update.

    _do_stats_update uses local imports, so we must patch at the
    correct source modules rather than at the client_manager namespace.
    """

    @patch("core.utils.send_websocket_update")
    @patch("apps.proxy.ts_proxy.channel_status.ChannelStatus.get_basic_channel_info")
    def test_stats_update_skips_deleted_metadata(self, mock_get_info, mock_ws):
        """Channels whose metadata has been deleted should be excluded from stats."""
        from .client_manager import HLSClientManager

        HLSClientManager._instance = None
        cm = HLSClientManager()

        # Provide a mock Redis client that the scan will use
        mock_redis_client = MagicMock()
        mock_redis_client.scan.return_value = (0, ["ts_proxy:channel:ch1:clients"])
        # Metadata key does NOT exist (deleted)
        mock_redis_client.exists.return_value = False

        with patch("redis.Redis.from_url", return_value=mock_redis_client):
            cm._do_stats_update()

        # ChannelStatus should NOT be called because metadata was deleted
        mock_get_info.assert_not_called()

        # WebSocket should still be called with empty channel list
        mock_ws.assert_called_once()
        call_args = mock_ws.call_args[0]
        import json
        stats = json.loads(call_args[2]["stats"])
        self.assertEqual(stats["count"], 0)

    def tearDown(self):
        from .client_manager import HLSClientManager
        HLSClientManager._instance = None


# ------------------------------------------------------------------ #
#  Worker heartbeat key pattern
# ------------------------------------------------------------------ #

class WorkerHeartbeatKeyTest(TestCase):
    """Tests for the worker heartbeat key generation."""

    def test_worker_heartbeat_key_format(self):
        from .manager import HLSOutputManager

        HLSOutputManager._instance = None
        manager = HLSOutputManager()
        key = manager._get_worker_heartbeat_key()
        self.assertTrue(key.startswith("hls_output:worker:"))
        self.assertTrue(key.endswith(":heartbeat"))

    def tearDown(self):
        from .manager import HLSOutputManager
        HLSOutputManager._instance = None


# ------------------------------------------------------------------ #
#  Session metadata refresh
# ------------------------------------------------------------------ #

class MetadataRefreshTest(TestCase):
    """Tests for the metadata TTL refresh called by cleanup thread."""

    def test_refresh_metadata_ttl(self):
        from .session import HLSSession, METADATA_TTL

        mock_redis = MagicMock()
        session = HLSSession("test-uuid", MagicMock())
        session._redis = mock_redis

        session.refresh_metadata_ttl()

        mock_redis.expire.assert_called_once_with(
            f"ts_proxy:channel:test-uuid:metadata",
            METADATA_TTL,
        )


# ------------------------------------------------------------------ #
#  _is_session_running_elsewhere with heartbeat check
# ------------------------------------------------------------------ #

class SessionRunningElsewhereTest(TestCase):
    """Tests for the enhanced _is_session_running_elsewhere with heartbeat check."""

    def _make_manager(self):
        from .manager import HLSOutputManager

        HLSOutputManager._instance = None
        manager = HLSOutputManager()
        manager._redis = MagicMock()
        return manager

    def test_returns_true_when_owner_with_heartbeat(self):
        """If another worker owns the key and has a heartbeat, return True."""
        manager = self._make_manager()
        manager._redis.get.return_value = b"other-worker"
        manager._redis.exists.return_value = True  # heartbeat exists

        result = manager._is_session_running_elsewhere("test-uuid")
        self.assertTrue(result)

    def test_returns_false_when_owner_without_heartbeat(self):
        """If another worker owns the key but has no heartbeat, return False."""
        manager = self._make_manager()
        manager._redis.get.return_value = b"other-worker"
        manager._redis.exists.return_value = False  # no heartbeat

        result = manager._is_session_running_elsewhere("test-uuid")
        self.assertFalse(result)

    def test_returns_false_when_no_owner(self):
        """If no one owns the key, return False."""
        manager = self._make_manager()
        manager._redis.get.return_value = None

        result = manager._is_session_running_elsewhere("test-uuid")
        self.assertFalse(result)

    def test_returns_false_when_we_own_it(self):
        """If we own the key, return False."""
        manager = self._make_manager()
        manager._redis.get.return_value = manager._worker_id.encode("utf-8")

        result = manager._is_session_running_elsewhere("test-uuid")
        self.assertFalse(result)

    def tearDown(self):
        from .manager import HLSOutputManager
        HLSOutputManager._instance = None
