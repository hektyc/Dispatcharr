"""
HLS Output Configuration.

Reads settings from CoreSettings (database) and HLS_PATH environment variable.
Provides cached access to all HLS output configuration values.
"""

import os
import time
import logging
import tempfile

logger = logging.getLogger(__name__)

# Default settings values
HLS_OUTPUT_DEFAULTS = {
    "storage_backend": "filesystem",
    "segment_duration": 6,
    "playlist_size": 10,
    "shutdown_delay": 30,
    "ll_hls_enabled": False,
    "use_fmp4_segments": False,
    "redis_segment_ttl": 120,
}

# HLS.js player defaults (served to frontend)
HLS_PLAYER_DEFAULTS = {
    "enable_worker": True,
    "low_latency_mode": False,
    "back_buffer_length": 30,
    "max_buffer_length": 30,
    "max_max_buffer_length": 60,
    "max_buffer_size": 60 * 1000 * 1000,
    "max_buffer_hole": 0.5,
    "level_loading_max_retry": 4,
    "frag_loading_max_retry": 6,
    "manifest_loading_max_retry": 4,
}


def get_hls_path():
    """Get the HLS output path from environment.

    The path is configured via HLS_PATH environment variable
    in docker-compose.yml. Supports any writable path:
    - /dev/shm (RAM disk - recommended)
    - Custom tmpfs mounts
    - Regular disk paths
    - Network mounts

    Returns empty string if not configured (HLS output disabled).
    """
    return os.environ.get("HLS_PATH", "")


class HLSOutputConfig:
    """Configuration manager for HLS output with caching."""

    _settings_cache = None
    _settings_cache_time = 0
    _settings_cache_ttl = 10  # Cache for 10 seconds

    def __init__(self):
        self._initialized = False
        self._path_validated = False

    def _load_settings(self):
        """Load settings from CoreSettings with caching."""
        now = time.time()
        if (
            self._settings_cache is not None
            and (now - self._settings_cache_time) < self._settings_cache_ttl
        ):
            return self._settings_cache

        try:
            from core.models import CoreSettings

            settings = CoreSettings.get_hls_output_settings()
            self._settings_cache = settings
            self._settings_cache_time = now
            return settings
        except Exception as e:
            logger.warning("Failed to load HLS output settings: %s", e)
            return dict(HLS_OUTPUT_DEFAULTS)

    @property
    def output_path(self):
        """Get the HLS output base path from HLS_PATH env var."""
        return get_hls_path()

    @property
    def is_enabled(self):
        """Check if HLS output is enabled (HLS_PATH is set)."""
        path = self.output_path
        return bool(path and path.strip())

    @property
    def segment_duration(self):
        return self._load_settings().get(
            "segment_duration", HLS_OUTPUT_DEFAULTS["segment_duration"]
        )

    @property
    def playlist_size(self):
        return self._load_settings().get(
            "playlist_size", HLS_OUTPUT_DEFAULTS["playlist_size"]
        )

    @property
    def shutdown_delay(self):
        return self._load_settings().get(
            "shutdown_delay", HLS_OUTPUT_DEFAULTS["shutdown_delay"]
        )

    @property
    def ll_hls_enabled(self):
        return self._load_settings().get(
            "ll_hls_enabled", HLS_OUTPUT_DEFAULTS["ll_hls_enabled"]
        )

    @property
    def use_fmp4_segments(self):
        return self._load_settings().get(
            "use_fmp4_segments", HLS_OUTPUT_DEFAULTS["use_fmp4_segments"]
        )

    @property
    def storage_backend(self):
        return self._load_settings().get(
            "storage_backend", HLS_OUTPUT_DEFAULTS["storage_backend"]
        )

    @property
    def redis_segment_ttl(self):
        return self._load_settings().get(
            "redis_segment_ttl", HLS_OUTPUT_DEFAULTS["redis_segment_ttl"]
        )

    @property
    def retention_seconds(self):
        """Calculate retention time based on playlist size and segment duration."""
        return self.playlist_size * self.segment_duration * 3

    def get_channel_path(self, channel_uuid):
        """Get the output path for a specific channel."""
        base = self.output_path
        if not base:
            return ""
        return os.path.join(base, str(channel_uuid))

    def _ensure_directory(self, path):
        """Ensure directory exists and is writable."""
        if not path:
            return False

        try:
            os.makedirs(path, exist_ok=True)
            # Write test
            test_file = os.path.join(path, ".write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except (OSError, IOError) as e:
            logger.error("Directory not writable: %s - %s", path, e)
            return False

    def initialize(self):
        """Initialize HLS output configuration. Called on startup."""
        if self._initialized:
            return

        path = self.output_path
        if not path:
            logger.info("HLS output disabled: HLS_PATH not set")
            self._initialized = True
            return

        if self._ensure_directory(path):
            logger.info("HLS output initialized: path=%s", path)
            self._path_validated = True
        else:
            logger.error(
                "HLS output path not writable: %s. HLS output will be disabled.", path
            )

        self._initialized = True

    def invalidate_cache(self):
        """Force-invalidate the settings cache."""
        self._settings_cache = None
        self._settings_cache_time = 0

    def to_dict(self):
        """Return all settings as a dictionary (useful for debugging)."""
        return {
            "output_path": self.output_path,
            "is_enabled": self.is_enabled,
            "storage_backend": self.storage_backend,
            "segment_duration": self.segment_duration,
            "playlist_size": self.playlist_size,
            "shutdown_delay": self.shutdown_delay,
            "ll_hls_enabled": self.ll_hls_enabled,
            "use_fmp4_segments": self.use_fmp4_segments,
            "redis_segment_ttl": self.redis_segment_ttl,
            "retention_seconds": self.retention_seconds,
        }


# Singleton instance
hls_config = HLSOutputConfig()
