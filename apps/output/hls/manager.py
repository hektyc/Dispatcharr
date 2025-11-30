# HLS Output Manager
# Manages FFmpeg processes for HLS segment generation

import os
import time
import subprocess
import threading
import shutil
import logging
from typing import Dict, Optional
from .config import hls_config

logger = logging.getLogger(__name__)


class HLSChannelSession:
    """Manages HLS output for a single channel."""

    def __init__(self, channel_uuid: str, ts_url: str):
        self.channel_uuid = channel_uuid
        self.ts_url = ts_url
        self.process: Optional[subprocess.Popen] = None
        self.output_path = hls_config.get_channel_path(channel_uuid)
        self.is_running = False
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self):
        """Start the FFmpeg process for HLS output."""
        if self.is_running:
            logger.warning(f"HLS session for {self.channel_uuid} already running")
            return False

        # Clean up any stale segments
        self._cleanup_segments()

        # Build FFmpeg command
        cmd = self._build_ffmpeg_command()
        logger.info(f"Starting HLS output for {self.channel_uuid}: {' '.join(cmd)}")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            self.is_running = True
            self._stop_event.clear()

            # Start monitor thread
            self._monitor_thread = threading.Thread(
                target=self._monitor_process,
                daemon=True,
                name=f"hls-monitor-{self.channel_uuid[:8]}"
            )
            self._monitor_thread.start()

            logger.info(f"HLS output started for {self.channel_uuid}, PID: {self.process.pid}")
            return True
        except Exception as e:
            logger.error(f"Failed to start HLS output for {self.channel_uuid}: {e}")
            self.is_running = False
            return False

    def stop(self):
        """Stop the FFmpeg process and cleanup."""
        if not self.is_running:
            return

        logger.info(f"Stopping HLS output for {self.channel_uuid}")
        self._stop_event.set()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as e:
                logger.error(f"Error stopping FFmpeg for {self.channel_uuid}: {e}")
            finally:
                self.process = None

        self.is_running = False

        # Cleanup based on retention settings
        retention = hls_config.retention_seconds
        if retention == 0:
            self._cleanup_segments()
        else:
            # Schedule delayed cleanup
            threading.Timer(retention, self._cleanup_segments).start()

    def _build_ffmpeg_command(self):
        """Build the FFmpeg command for HLS output."""
        segment_duration = hls_config.segment_duration
        playlist_size = hls_config.playlist_size
        playlist_path = os.path.join(self.output_path, "stream.m3u8")
        segment_pattern = os.path.join(self.output_path, "segment_%05d.ts")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-i", self.ts_url,
            "-c", "copy",  # Copy without re-encoding
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_list_size", str(playlist_size),
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", segment_pattern,
            playlist_path,
        ]
        return cmd

    def _monitor_process(self):
        """Monitor FFmpeg process and log any errors."""
        while not self._stop_event.is_set() and self.process:
            if self.process.poll() is not None:
                # Process ended unexpectedly
                if not self._stop_event.is_set():
                    stderr = self.process.stderr.read().decode() if self.process.stderr else ""
                    logger.error(f"FFmpeg for {self.channel_uuid} exited unexpectedly: {stderr}")
                    self.is_running = False
                break
            time.sleep(1)

    def _cleanup_segments(self):
        """Remove all HLS segments and playlist for this channel."""
        try:
            if os.path.exists(self.output_path):
                shutil.rmtree(self.output_path)
                logger.info(f"Cleaned up HLS segments for {self.channel_uuid}")
        except Exception as e:
            logger.error(f"Failed to cleanup HLS segments for {self.channel_uuid}: {e}")

    @property
    def playlist_path(self):
        """Get the path to the HLS playlist file."""
        return os.path.join(self.output_path, "stream.m3u8")

    @property
    def playlist_exists(self):
        """Check if the playlist file exists."""
        return os.path.exists(self.playlist_path)


class HLSOutputManager:
    """
    Singleton manager for all HLS output sessions.
    Handles starting/stopping HLS output for channels on demand.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._sessions: Dict[str, HLSChannelSession] = {}
        self._sessions_lock = threading.Lock()
        self._initialized = True
        logger.info("HLS Output Manager initialized")

    @classmethod
    def get_instance(cls) -> "HLSOutputManager":
        """Get the singleton instance."""
        return cls()

    def get_or_start_session(self, channel_uuid: str, ts_url: str) -> Optional[HLSChannelSession]:
        """
        Get an existing HLS session or start a new one.
        Returns the session if successful, None otherwise.
        """
        with self._sessions_lock:
            # Check for existing session
            if channel_uuid in self._sessions:
                session = self._sessions[channel_uuid]
                if session.is_running:
                    return session
                # Session exists but not running, clean it up
                del self._sessions[channel_uuid]

            # Create new session
            session = HLSChannelSession(channel_uuid, ts_url)
            if session.start():
                self._sessions[channel_uuid] = session
                return session

            return None

    def stop_session(self, channel_uuid: str):
        """Stop an HLS session for a channel."""
        with self._sessions_lock:
            if channel_uuid in self._sessions:
                session = self._sessions[channel_uuid]
                session.stop()
                del self._sessions[channel_uuid]
                logger.info(f"HLS session stopped for {channel_uuid}")

    def get_session(self, channel_uuid: str) -> Optional[HLSChannelSession]:
        """Get an existing session without starting a new one."""
        with self._sessions_lock:
            return self._sessions.get(channel_uuid)

    def is_session_active(self, channel_uuid: str) -> bool:
        """Check if a session is active for a channel."""
        with self._sessions_lock:
            session = self._sessions.get(channel_uuid)
            return session is not None and session.is_running

    def stop_all_sessions(self):
        """Stop all active HLS sessions."""
        with self._sessions_lock:
            for channel_uuid in list(self._sessions.keys()):
                self._sessions[channel_uuid].stop()
            self._sessions.clear()
            logger.info("All HLS sessions stopped")

    def get_active_sessions(self) -> Dict[str, bool]:
        """Get a dict of channel_uuid -> is_running for all sessions."""
        with self._sessions_lock:
            return {
                uuid: session.is_running
                for uuid, session in self._sessions.items()
            }


# Global manager instance
hls_manager = HLSOutputManager.get_instance()

