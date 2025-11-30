# HLS Output Configuration
import os
import json
import logging
import stat
from django.conf import settings as django_settings

logger = logging.getLogger(__name__)


class HLSConfig:
    """Configuration for HLS output.

    Note: Settings are always read fresh from the database to support
    multi-worker uwsgi environments where cache invalidation in one worker
    doesn't affect other workers.
    """

    # Default settings
    DEFAULT_OUTPUT_PATH = "/data/hls"
    DEFAULT_SEGMENT_DURATION = 6  # seconds
    DEFAULT_PLAYLIST_SIZE = 5  # number of segments in playlist
    DEFAULT_RETENTION_SECONDS = 0  # 0 = delete immediately when channel stops

    def __init__(self):
        pass

    def _load_settings(self):
        """Load HLS settings from CoreSettings.

        Always reads fresh from database to support multi-worker environments.
        """
        try:
            from core.models import CoreSettings, HLS_OUTPUT_SETTINGS_KEY
            settings_obj = CoreSettings.objects.filter(key=HLS_OUTPUT_SETTINGS_KEY).first()
            if settings_obj:
                return json.loads(settings_obj.value)
            else:
                return {}
        except Exception as e:
            logger.warning(f"Could not load HLS settings: {e}")
            return {}

    def _invalidate_cache(self):
        """Invalidate cached settings.

        This is kept for API compatibility but no longer does anything
        since settings are always read fresh from the database.
        """
        pass

    @property
    def output_path(self):
        """Get HLS output path.

        Returns the configured output path if it's writable, otherwise falls
        back to the default path.
        """
        settings = self._load_settings()
        path = settings.get("output_path", self.DEFAULT_OUTPUT_PATH)

        # Try to ensure the configured path exists and is writable
        if self._ensure_directory(path):
            return path

        # Fall back to default path if configured path fails
        if path != self.DEFAULT_OUTPUT_PATH:
            logger.warning(f"Configured HLS path {path} not usable, falling back to {self.DEFAULT_OUTPUT_PATH}")
            if self._ensure_directory(self.DEFAULT_OUTPUT_PATH):
                return self.DEFAULT_OUTPUT_PATH

        # Last resort - just return the path and let it fail later
        return path

    def _ensure_directory(self, path):
        """Ensure directory exists with proper permissions for HLS output.

        Returns True if directory exists and is writable, False otherwise.
        Does not raise exceptions - logs errors instead.
        """
        try:
            if not os.path.exists(path):
                os.makedirs(path, mode=0o755, exist_ok=True)
                logger.info(f"Created HLS output directory: {path}")

            # Verify we can write to the directory
            test_file = os.path.join(path, ".hls_write_test")
            try:
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
                logger.debug(f"HLS output directory verified writable: {path}")
                return True
            except (IOError, OSError) as e:
                logger.error(f"HLS output directory {path} is not writable: {e}")
                return False
        except Exception as e:
            logger.error(f"Failed to create/verify HLS output directory {path}: {e}")
            return False

    @property
    def segment_duration(self):
        """Get segment duration in seconds."""
        settings = self._load_settings()
        return settings.get("segment_duration", self.DEFAULT_SEGMENT_DURATION)

    @property
    def playlist_size(self):
        """Get number of segments to keep in playlist."""
        settings = self._load_settings()
        return settings.get("playlist_size", self.DEFAULT_PLAYLIST_SIZE)

    @property
    def retention_seconds(self):
        """Get retention time in seconds (0 = delete immediately on stop)."""
        settings = self._load_settings()
        return settings.get("retention_seconds", self.DEFAULT_RETENTION_SECONDS)

    @property
    def ll_hls_enabled(self):
        """Check if Low-Latency HLS is enabled."""
        settings = self._load_settings()
        return settings.get("ll_hls_enabled", False)

    def get_channel_path(self, channel_uuid):
        """Get the output path for a specific channel."""
        channel_path = os.path.join(self.output_path, str(channel_uuid))
        self._ensure_directory(channel_path)
        return channel_path

    def initialize(self):
        """Initialize HLS output directory on startup.

        This should be called during Django app initialization to ensure
        the HLS output directory exists before any streams are started.
        """
        try:
            path = self.output_path  # This triggers directory creation
            logger.info(f"HLS output directory initialized: {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize HLS output directory: {e}")
            return False

    def to_dict(self):
        """Return all settings as a dictionary."""
        return {
            "output_path": self.output_path,
            "segment_duration": self.segment_duration,
            "playlist_size": self.playlist_size,
            "retention_seconds": self.retention_seconds,
            "ll_hls_enabled": self.ll_hls_enabled,
        }


# Global config instance
hls_config = HLSConfig()

