"""Tests for HLS Output module."""

from django.test import TestCase


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
        from unittest.mock import MagicMock
        from .storage.redis_store import RedisSegmentStore

        mock_redis = MagicMock()
        store = RedisSegmentStore(mock_redis, ttl=60)
        self.assertEqual(store._ttl, 60)
