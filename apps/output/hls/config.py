# HLS Output Configuration
import os
import json
import logging
from django.conf import settings as django_settings

logger = logging.getLogger(__name__)


class HLSConfig:
    """Configuration for HLS output."""

    # Default settings
    DEFAULT_OUTPUT_PATH = "/data/hls"
    DEFAULT_SEGMENT_DURATION = 6  # seconds
    DEFAULT_PLAYLIST_SIZE = 5  # number of segments in playlist
    DEFAULT_RETENTION_SECONDS = 0  # 0 = delete immediately when channel stops

    def __init__(self):
        self._settings = None

    def _load_settings(self):
        """Load HLS settings from CoreSettings."""
        if self._settings is not None:
            return self._settings

        try:
            from core.models import CoreSettings, HLS_OUTPUT_SETTINGS_KEY
            settings_obj = CoreSettings.objects.filter(key=HLS_OUTPUT_SETTINGS_KEY).first()
            if settings_obj:
                self._settings = json.loads(settings_obj.value)
            else:
                self._settings = {}
        except Exception as e:
            logger.warning(f"Could not load HLS settings: {e}")
            self._settings = {}

        return self._settings

    def _invalidate_cache(self):
        """Invalidate cached settings."""
        self._settings = None

    @property
    def output_path(self):
        """Get HLS output path."""
        settings = self._load_settings()
        path = settings.get("output_path", self.DEFAULT_OUTPUT_PATH)
        # Ensure directory exists
        os.makedirs(path, exist_ok=True)
        return path

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
        os.makedirs(channel_path, exist_ok=True)
        return channel_path

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

