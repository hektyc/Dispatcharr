# HLS Output Configuration
import os
import json
import logging
import stat
from django.conf import settings as django_settings

logger = logging.getLogger(__name__)

# Environment variable for HLS output path
HLS_OUTPUT_PATH_ENV = "HLS_OUTPUT_PATH"


class HLSConfig:
    """Configuration for HLS output.

    Note: The output path is read from the HLS_OUTPUT_PATH environment variable
    (set in docker-compose.yml or .env file). This ensures the path is configured
    at container startup when volume mounts are defined.

    Other settings are read fresh from the database to support multi-worker
    uwsgi environments.
    """

    # Default settings
    DEFAULT_OUTPUT_PATH = "/data/hls"
    DEFAULT_SEGMENT_DURATION = 6  # seconds
    DEFAULT_PLAYLIST_SIZE = 10  # number of segments in playlist (10 * 6s = 60s buffer)
    DEFAULT_RETENTION_SECONDS = 0  # 0 = delete immediately when channel stops

    def __init__(self):
        # Output path is read fresh from environment on each access
        # This ensures changes to the environment are picked up
        pass

    def _load_settings(self):
        """Load HLS settings from CoreSettings.

        Always reads fresh from database to support multi-worker environments.
        Note: output_path is NOT included here - it comes from environment variable.
        """
        try:
            from core.models import CoreSettings, HLS_OUTPUT_SETTINGS_KEY
            # Log the key we're looking for
            logger.info(f"Looking for HLS settings with key: '{HLS_OUTPUT_SETTINGS_KEY}'")

            settings_obj = CoreSettings.objects.filter(key=HLS_OUTPUT_SETTINGS_KEY).first()
            if settings_obj:
                raw_value = settings_obj.value
                logger.info(f"Found HLS settings in database: key='{settings_obj.key}', raw_value='{raw_value}'")
                settings = json.loads(raw_value)
                logger.info(f"Parsed HLS settings: {settings}")
                return settings
            else:
                # Log all CoreSettings keys to help debug
                all_keys = list(CoreSettings.objects.values_list('key', flat=True))
                logger.warning(f"HLS settings not found in database. Available keys: {all_keys}")
                return {}
        except Exception as e:
            logger.error(f"Could not load HLS settings: {e}", exc_info=True)
            return {}

    def _invalidate_cache(self):
        """Invalidate cached settings.

        This is kept for API compatibility but no longer does anything
        since settings are always read fresh from the database.
        """
        pass

    @property
    def output_path(self):
        """Get HLS output path from environment variable.

        The path is read FRESH from the HLS_OUTPUT_PATH environment variable
        on EVERY access. This ensures changes to the environment are picked up
        without requiring a restart of the Python process.

        If not set, defaults to /data/hls.

        This is configured via docker-compose.yml or .env file, not via
        the Settings UI, because Docker volume mounts must be defined at
        container startup.
        """
        # Always read fresh from environment - no caching
        path = os.environ.get(HLS_OUTPUT_PATH_ENV, self.DEFAULT_OUTPUT_PATH)
        logger.debug(f"HLS output path from environment: {path}")

        # Ensure directory exists and is writable
        self._ensure_directory(path)
        return path

    def _ensure_directory(self, path):
        """Ensure directory exists with proper permissions for HLS output.

        Returns True if directory exists and is writable, False otherwise.
        Does not raise exceptions - logs errors instead.

        For ramdisk/tmpfs mounts, users should either:
        1. Use Docker's native tmpfs mount (recommended):
           volumes:
             - type: tmpfs
               target: /data/hls
               tmpfs:
                 size: 1073741824  # 1GB

        2. Or mount a host ramdisk with proper permissions:
           volumes:
             - /mnt/user/ramdisk:/data/hls:rw

           And ensure the host directory is writable by the container user:
           chown -R $PUID:$PGID /mnt/user/ramdisk
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
                # Get current user info for helpful error message
                import pwd
                try:
                    current_user = pwd.getpwuid(os.getuid())
                    user_info = f"uid={current_user.pw_uid}, gid={current_user.pw_gid}"
                except:
                    user_info = f"uid={os.getuid()}"

                # Check directory ownership
                try:
                    dir_stat = os.stat(path)
                    dir_info = f"dir owner uid={dir_stat.st_uid}, gid={dir_stat.st_gid}"
                except:
                    dir_info = "could not stat directory"

                logger.error(
                    f"HLS output directory {path} is not writable: {e}. "
                    f"Container user: {user_info}. Directory: {dir_info}. "
                    f"Fix: Either use Docker tmpfs mount, or run 'chown -R $PUID:$PGID {path}' on the host."
                )
                return False
        except PermissionError as e:
            logger.error(
                f"Permission denied creating HLS directory {path}: {e}. "
                f"For ramdisk/tmpfs, use Docker's native tmpfs mount: "
                f"volumes: [{{type: tmpfs, target: /data/hls, tmpfs: {{size: 1073741824}}}}] "
                f"or ensure host directory is writable by container user (PUID/PGID)."
            )
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

    @property
    def use_fmp4_segments(self):
        """Check if fMP4 segments should be used instead of MPEG-TS.

        fMP4 (fragmented MP4) is a more modern container format that:
        - Supports more codecs (H.265/HEVC, VP9, AV1, AAC, AC-3, etc.)
        - Is more efficient (~10-15% smaller file sizes)
        - Provides better seeking precision
        - Is the basis for CMAF (Common Media Application Format)

        Note: This is automatically enabled when LL-HLS is enabled.
        """
        settings = self._load_settings()
        # fMP4 is enabled if explicitly set OR if LL-HLS is enabled
        return settings.get("use_fmp4_segments", False) or self.ll_hls_enabled

    @property
    def shutdown_delay(self):
        """Get HLS-specific shutdown delay in seconds.

        This is independent from the TS Proxy shutdown delay because HLS
        streaming has different timing characteristics (segment-based vs
        continuous streaming).

        Default is 30 seconds to account for HLS segment duration and
        client buffering behavior.

        Note: Minimum of 15 seconds is enforced at save time via the serializer.
        The runtime respects whatever value is stored in the database.
        """
        settings = self._load_settings()
        return settings.get("shutdown_delay", 30)

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
            "use_fmp4_segments": self.use_fmp4_segments,
            "shutdown_delay": self.shutdown_delay,
        }


# Global config instance
hls_config = HLSConfig()

