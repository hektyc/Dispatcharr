"""File watcher for Redis storage backend.

Monitors the FFmpeg staging directory for new/updated segments and playlists,
pushes them to Redis, then deletes the staged files to save disk space.
"""

import os
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FileWatcher:
    """Watches FFmpeg output directory and pushes data to a SegmentStore.

    Used in Redis storage mode to bridge FFmpeg's filesystem output
    to the Redis-backed segment store.
    """

    def __init__(self, channel_uuid: str, watch_path: str, store, poll_interval: float = 0.5):
        """Initialize the file watcher.

        Args:
            channel_uuid: The channel identifier.
            watch_path: Path to the directory FFmpeg writes to.
            store: The SegmentStore implementation to push data to.
            poll_interval: How often to check for changes (seconds).
        """
        self.channel_uuid = channel_uuid
        self.watch_path = watch_path
        self.store = store
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread = None
        self._known_segments = {}  # name -> mtime
        self._playlist_mtime = 0

    def start(self):
        """Start the watcher thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name=f"hls-watcher-{self.channel_uuid[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info("File watcher started for channel %s at %s", self.channel_uuid, self.watch_path)

    def stop(self):
        """Stop the watcher thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("File watcher stopped for channel %s", self.channel_uuid)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _watch_loop(self):
        """Main watch loop that monitors the staging directory."""
        while not self._stop_event.is_set():
            try:
                if os.path.exists(self.watch_path):
                    self._check_playlist()
                    self._check_segments()
            except Exception as e:
                logger.error(
                    "Watcher error for channel %s: %s",
                    self.channel_uuid, e
                )
            self._stop_event.wait(self.poll_interval)

    def _check_playlist(self):
        """Check for playlist updates and push to store."""
        playlist_path = os.path.join(self.watch_path, "index.m3u8")
        if not os.path.exists(playlist_path):
            return

        try:
            mtime = os.path.getmtime(playlist_path)
            if mtime > self._playlist_mtime:
                with open(playlist_path, "r") as f:
                    content = f.read()
                if self.store.store_playlist(self.channel_uuid, content):
                    self._playlist_mtime = mtime
        except (OSError, IOError) as e:
            logger.debug("Watcher: playlist read error for %s: %s", self.channel_uuid, e)

    def _check_segments(self):
        """Check for new/updated segments and push to store."""
        try:
            files = os.listdir(self.watch_path)
        except OSError:
            return

        for filename in files:
            # Only process segment files
            if not self._is_segment_file(filename):
                continue

            filepath = os.path.join(self.watch_path, filename)
            try:
                mtime = os.path.getmtime(filepath)
                known_mtime = self._known_segments.get(filename, 0)

                if mtime > known_mtime:
                    with open(filepath, "rb") as f:
                        data = f.read()
                    if self.store.store_segment(self.channel_uuid, filename, data):
                        self._known_segments[filename] = mtime
                        # Optionally remove staged file after pushing to Redis
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
            except (OSError, IOError) as e:
                logger.debug(
                    "Watcher: segment read error %s for %s: %s",
                    filename, self.channel_uuid, e
                )

    @staticmethod
    def _is_segment_file(filename: str) -> bool:
        """Check if a filename is an HLS segment."""
        if filename == "init.mp4":
            return True
        if filename.startswith("index") and (
            filename.endswith(".ts") or filename.endswith(".m4s")
        ):
            return True
        return False
