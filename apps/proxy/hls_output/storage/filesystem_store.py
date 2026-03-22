"""Filesystem-based segment storage for HLS output.

FFmpeg writes directly to the filesystem. Views serve files directly.
This is the simplest and recommended storage backend.
"""

import os
import glob
import shutil
import logging
from typing import Optional

from .base import SegmentStore

logger = logging.getLogger(__name__)


class FilesystemSegmentStore(SegmentStore):
    """Direct filesystem storage for HLS segments.

    FFmpeg writes segments directly to {HLS_PATH}/{uuid}/.
    Views read and serve files directly from the filesystem.
    """

    def __init__(self, base_path: str):
        """Initialize with the base HLS output path.

        Args:
            base_path: The root HLS path (from HLS_PATH env var).
        """
        self.base_path = base_path

    def _channel_dir(self, channel_uuid: str) -> str:
        return os.path.join(self.base_path, str(channel_uuid))

    def store_playlist(self, channel_uuid: str, content: str) -> bool:
        """Store playlist to filesystem.

        Note: In filesystem mode, FFmpeg writes the playlist directly.
        This method is provided for manual playlist updates if needed.
        """
        try:
            channel_dir = self._channel_dir(channel_uuid)
            os.makedirs(channel_dir, exist_ok=True)
            playlist_path = os.path.join(channel_dir, "index.m3u8")
            with open(playlist_path, "w") as f:
                f.write(content)
            return True
        except (OSError, IOError) as e:
            logger.error("Failed to store playlist for %s: %s", channel_uuid, e)
            return False

    def get_playlist(self, channel_uuid: str) -> Optional[str]:
        """Read playlist from filesystem."""
        playlist_path = os.path.join(self._channel_dir(channel_uuid), "index.m3u8")
        try:
            if not os.path.exists(playlist_path):
                return None
            with open(playlist_path, "r") as f:
                return f.read()
        except (OSError, IOError) as e:
            logger.error("Failed to read playlist for %s: %s", channel_uuid, e)
            return None

    def store_segment(self, channel_uuid: str, name: str, data: bytes) -> bool:
        """Store segment to filesystem.

        Note: In filesystem mode, FFmpeg writes segments directly.
        This method is provided for completeness.
        """
        try:
            channel_dir = self._channel_dir(channel_uuid)
            os.makedirs(channel_dir, exist_ok=True)
            segment_path = os.path.join(channel_dir, name)
            with open(segment_path, "wb") as f:
                f.write(data)
            return True
        except (OSError, IOError) as e:
            logger.error("Failed to store segment %s for %s: %s", name, channel_uuid, e)
            return False

    def get_segment(self, channel_uuid: str, name: str) -> Optional[bytes]:
        """Read segment from filesystem."""
        segment_path = os.path.join(self._channel_dir(channel_uuid), name)
        try:
            if not os.path.exists(segment_path):
                return None
            with open(segment_path, "rb") as f:
                return f.read()
        except (OSError, IOError) as e:
            logger.error("Failed to read segment %s for %s: %s", name, channel_uuid, e)
            return None

    def get_segment_path(self, channel_uuid: str, name: str) -> Optional[str]:
        """Get the filesystem path for a segment (for FileResponse).

        Returns:
            The full path to the segment file, or None if not found.
        """
        segment_path = os.path.join(self._channel_dir(channel_uuid), name)
        if os.path.exists(segment_path):
            return segment_path
        return None

    def get_segment_count(self, channel_uuid: str) -> int:
        """Count segment files on disk."""
        channel_dir = self._channel_dir(channel_uuid)
        ts_files = glob.glob(os.path.join(channel_dir, "index*.ts"))
        m4s_files = glob.glob(os.path.join(channel_dir, "index*.m4s"))
        return len(ts_files) + len(m4s_files)

    def cleanup_channel(self, channel_uuid: str):
        """Remove all files for a channel."""
        channel_dir = self._channel_dir(channel_uuid)
        try:
            if os.path.exists(channel_dir):
                shutil.rmtree(channel_dir)
                logger.info("Cleaned up HLS output for channel %s", channel_uuid)
        except (OSError, IOError) as e:
            logger.error("Failed to cleanup channel %s: %s", channel_uuid, e)
