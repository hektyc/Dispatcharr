import atexit
import logging
import signal
import os
import shutil
from django.apps import AppConfig

logger = logging.getLogger(__name__)


def cleanup_all_hls_sessions():
    """Cleanup all HLS sessions on shutdown."""
    try:
        from apps.output.hls.manager import hls_manager
        from apps.output.hls.config import hls_config

        # Stop all active sessions
        hls_manager.stop_all_sessions()

        # Also clean up any orphaned channel directories
        output_path = hls_config.output_path
        if os.path.exists(output_path):
            for item in os.listdir(output_path):
                item_path = os.path.join(output_path, item)
                if os.path.isdir(item_path):
                    try:
                        shutil.rmtree(item_path)
                        logger.info(f"Cleaned up orphaned HLS directory: {item_path}")
                    except Exception as e:
                        logger.warning(f"Failed to cleanup orphaned HLS directory {item_path}: {e}")

        logger.info("HLS cleanup completed on shutdown")
    except Exception as e:
        logger.warning(f"Error during HLS cleanup on shutdown: {e}")


class OutputConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.output'
    verbose_name = "Output"

    def ready(self):
        """Initialize HLS output directory on app startup."""
        # Import here to avoid circular imports
        try:
            from apps.output.hls.config import hls_config
            if hls_config.initialize():
                logger.info("HLS output module initialized successfully")
            else:
                logger.warning("HLS output module initialization failed - check permissions")

            # Register cleanup on shutdown
            atexit.register(cleanup_all_hls_sessions)
            logger.debug("Registered HLS cleanup on shutdown")

        except Exception as e:
            logger.warning(f"Could not initialize HLS module: {e}")
