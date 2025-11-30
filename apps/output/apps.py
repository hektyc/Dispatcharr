import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


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
        except Exception as e:
            logger.warning(f"Could not initialize HLS module: {e}")
