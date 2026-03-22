"""HLS Output Session.

Per-channel session managing an FFmpeg process that converts
an input stream to HLS output segments.
"""

import os
import re
import time
import json
import signal
import subprocess
import threading
import logging
from typing import Optional, Dict, Any

from .config import hls_config

logger = logging.getLogger(__name__)

# FFmpeg stderr parsing patterns
STREAM_INFO_RE = re.compile(
    r"Stream #\d+:\d+.*?: (Video|Audio): (\S+)"
    r"(?:.*?(\d{2,5})x(\d{2,5}))?"
    r"(?:.*?(\d+(?:\.\d+)?)\s*fps)?"
    r"(?:.*?(\d+)\s*kb/s)?"
)
PROGRESS_RE = re.compile(
    r"speed=\s*(\d+(?:\.\d+)?)x"
)
FPS_RE = re.compile(r"fps=\s*(\d+(?:\.\d+)?)")
BITRATE_RE = re.compile(r"bitrate=\s*(\d+(?:\.\d+)?)kbits/s")


class HLSSession:
    """Manages an FFmpeg process for a single channel's HLS output.

    Handles FFmpeg lifecycle, stderr parsing for stream info,
    and integrates with the storage backend.
    """

    def __init__(self, channel_uuid: str, storage, stream_profile=None):
        """Initialize session.

        Args:
            channel_uuid: The channel identifier.
            storage: A SegmentStore instance for this channel.
            stream_profile: Optional StreamProfile instance for command building.
                If the profile contains {hlsOutputPath}, it will be used to
                build the FFmpeg command. Otherwise, falls back to the internal builder.
        """
        self.channel_uuid = channel_uuid
        self.storage = storage
        self._stream_profile = stream_profile
        self._process = None
        self._monitor_thread = None
        self._stderr_thread = None
        self._stop_event = threading.Event()
        self._watcher = None
        self._stream_url = None
        self._user_agent = None
        self._started_at = None
        self._stream_info = {}
        self._redis = None

    @property
    def output_path(self) -> str:
        """Get the output directory for this channel."""
        return hls_config.get_channel_path(self.channel_uuid)

    @property
    def playlist_path(self) -> str:
        """Get the full path to the playlist file."""
        return os.path.join(self.output_path, "index.m3u8")

    @property
    def is_running(self) -> bool:
        """Check if the FFmpeg process is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def playlist_exists(self) -> bool:
        """Check if the playlist is available."""
        if hls_config.storage_backend == "redis":
            return self.storage.get_playlist(self.channel_uuid) is not None
        return os.path.exists(self.playlist_path)

    @property
    def redis_client(self):
        """Lazy Redis client for metadata storage."""
        if self._redis is None:
            try:
                import redis as redis_lib
                import os
                from django.conf import settings as django_settings
                host = os.environ.get("REDIS_HOST", getattr(django_settings, "REDIS_HOST", "localhost"))
                port = int(os.environ.get("REDIS_PORT", getattr(django_settings, "REDIS_PORT", 6379)))
                db = int(os.environ.get("REDIS_DB", getattr(django_settings, "REDIS_DB", 0)))
                self._redis = redis_lib.Redis(host=host, port=port, db=db)
            except Exception:
                pass
        return self._redis

    def start(self, stream_url: str, user_agent: str = None) -> bool:
        """Start the FFmpeg process for HLS output.

        Args:
            stream_url: The input stream URL.
            user_agent: Optional user agent for the input connection.

        Returns:
            True if started successfully.
        """
        if self.is_running:
            logger.warning("Session already running for channel %s", self.channel_uuid)
            return True

        self._stream_url = stream_url
        self._user_agent = user_agent or "VLC/3.0.20 LibVLC/3.0.20"
        self._stop_event.clear()

        # Ensure output directory
        output_path = self.output_path
        if not output_path:
            logger.error("No HLS output path configured")
            return False

        os.makedirs(output_path, exist_ok=True)

        # Clean up stale HLS files from previous runs to prevent
        # append_list from referencing old non-existent segments
        self._cleanup_stale_hls_files(output_path)

        # Build and start FFmpeg command
        cmd = self._build_ffmpeg_command(stream_url, self._user_agent, output_path)
        logger.info(
            "Starting HLS output for channel %s: %s",
            self.channel_uuid, " ".join(cmd[:6]) + "...",
        )

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
            )
            self._started_at = time.time()

            # Start stderr reader thread
            self._stderr_thread = threading.Thread(
                target=self._read_ffmpeg_stderr,
                name=f"hls-stderr-{self.channel_uuid[:8]}",
                daemon=True,
            )
            self._stderr_thread.start()

            # Start monitor thread
            self._monitor_thread = threading.Thread(
                target=self._monitor_process,
                name=f"hls-monitor-{self.channel_uuid[:8]}",
                daemon=True,
            )
            self._monitor_thread.start()

            # Start watcher for Redis mode
            if hls_config.storage_backend == "redis":
                from .watcher import FileWatcher
                self._watcher = FileWatcher(
                    self.channel_uuid, output_path, self.storage
                )
                self._watcher.start()

            # Update Redis metadata
            self._update_metadata("status", "running")
            self._update_metadata("stream_url", stream_url)
            self._update_metadata("started_at", str(self._started_at))

            return True
        except Exception as e:
            logger.error("Failed to start FFmpeg for channel %s: %s", self.channel_uuid, e)
            return False

    def stop(self):
        """Stop the FFmpeg process and clean up."""
        self._stop_event.set()

        # Stop watcher
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

        # Stop FFmpeg process
        if self._process is not None:
            try:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

            self._process = None

        # Wait for threads
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=3)
            self._stderr_thread = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=3)
            self._monitor_thread = None

        # Clean up storage
        self.storage.cleanup_channel(self.channel_uuid)

        # Update Redis metadata
        self._update_metadata("status", "stopped")

        logger.info("HLS output stopped for channel %s", self.channel_uuid)

    def restart(self, stream_url: str = None, user_agent: str = None) -> bool:
        """Restart the session with a new stream URL."""
        self.stop()
        time.sleep(0.5)
        return self.start(
            stream_url or self._stream_url,
            user_agent or self._user_agent,
        )

    def _build_ffmpeg_command(self, stream_url: str, user_agent: str, output_path: str) -> list:
        """Build the FFmpeg command for HLS output.

        If a stream profile with {hlsOutputPath} is available, delegates to
        StreamProfile.build_command(). Otherwise, falls back to the internal
        command builder using HLS settings from config.
        """
        # Use stream profile if it's HLS-aware
        if self._stream_profile and self._stream_profile.is_hls_profile():
            try:
                cmd = self._stream_profile.build_command(
                    stream_url, user_agent, hls_output_path=output_path
                )
                if cmd:
                    logger.info(
                        "Using stream profile '%s' for HLS output on channel %s",
                        self._stream_profile.name, self.channel_uuid,
                    )
                    return cmd
            except Exception as e:
                logger.warning(
                    "Stream profile build_command() failed for channel %s, "
                    "falling back to internal builder: %s",
                    self.channel_uuid, e,
                )

        # Fallback: build command internally from HLS config settings
        settings = hls_config._load_settings()
        segment_duration = settings.get("segment_duration", 6)
        playlist_size = settings.get("playlist_size", 10)
        use_fmp4 = settings.get("use_fmp4_segments", False)
        ll_hls = settings.get("ll_hls_enabled", False)

        segment_ext = "m4s" if use_fmp4 or ll_hls else "ts"

        hls_flags = "append_list+omit_endlist+program_date_time"
        if ll_hls:
            hls_flags += "+independent_segments"

        # Determine if source is HLS
        is_hls_source = self._is_source_hls(stream_url)

        cmd = ["ffmpeg", "-hide_banner"]

        # Input options
        if not is_hls_source:
            cmd.extend([
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ])

        cmd.extend([
            "-user_agent", user_agent,
            "-i", stream_url,
        ])

        # Output options - copy codec (no transcoding)
        cmd.extend([
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_list_size", str(playlist_size),
            "-hls_flags", hls_flags,
            "-hls_segment_filename",
            os.path.join(output_path, f"index%d.{segment_ext}"),
        ])

        # fMP4 options
        if use_fmp4 or ll_hls:
            cmd.extend([
                "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", "init.mp4",
            ])

        # Output file
        cmd.append(os.path.join(output_path, "index.m3u8"))

        return cmd

    @staticmethod
    def _cleanup_stale_hls_files(output_path: str):
        """Remove stale HLS files from a previous session run.

        This prevents the append_list HLS flag from referencing old
        segment indices that no longer exist on disk after a restart.
        """
        import glob

        try:
            patterns = [
                os.path.join(output_path, "index*.ts"),
                os.path.join(output_path, "index*.m4s"),
                os.path.join(output_path, "index.m3u8"),
                os.path.join(output_path, "init.mp4"),
            ]
            removed = 0
            for pattern in patterns:
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                        removed += 1
                    except OSError:
                        pass
            if removed:
                logger.debug(
                    "Cleaned up %d stale HLS files from %s", removed, output_path
                )
        except Exception as e:
            logger.warning("Failed to clean up stale HLS files: %s", e)

    @staticmethod
    def _is_source_hls(url: str) -> bool:
        """Check if the source URL is an HLS stream."""
        if not url:
            return False
        lower = url.lower().split("?")[0]
        return lower.endswith(".m3u8") or lower.endswith(".m3u")

    def _monitor_process(self):
        """Monitor the FFmpeg process and handle crashes."""
        while not self._stop_event.is_set():
            if self._process is None:
                break

            returncode = self._process.poll()
            if returncode is not None:
                if not self._stop_event.is_set():
                    logger.warning(
                        "FFmpeg exited with code %d for channel %s",
                        returncode, self.channel_uuid,
                    )
                    self._update_metadata("status", "crashed")
                    # Attempt automatic restart
                    self._try_automatic_restart()
                break

            self._stop_event.wait(2)

    def _try_automatic_restart(self):
        """Try to restart FFmpeg after a crash."""
        if self._stop_event.is_set():
            return

        max_retries = 3
        for attempt in range(max_retries):
            if self._stop_event.is_set():
                return

            logger.info(
                "Attempting restart %d/%d for channel %s",
                attempt + 1, max_retries, self.channel_uuid,
            )
            time.sleep(2)

            if self.start(self._stream_url, self._user_agent):
                logger.info("Successfully restarted channel %s", self.channel_uuid)
                return

        logger.error(
            "Failed to restart channel %s after %d attempts",
            self.channel_uuid, max_retries,
        )
        self._update_metadata("status", "failed")

    def _read_ffmpeg_stderr(self):
        """Read FFmpeg stderr output for stream info and progress."""
        if self._process is None or self._process.stderr is None:
            return

        buffer = ""
        try:
            while not self._stop_event.is_set() and self._process.poll() is None:
                byte = self._process.stderr.read(1)
                if not byte:
                    break

                char = byte.decode("utf-8", errors="replace")
                if char in ("\n", "\r"):
                    if buffer.strip():
                        self._parse_ffmpeg_line(buffer.strip())
                    buffer = ""
                else:
                    buffer += char

            # Process remaining buffer
            if buffer.strip():
                self._parse_ffmpeg_line(buffer.strip())
        except Exception as e:
            if not self._stop_event.is_set():
                logger.debug("Stderr reader error for %s: %s", self.channel_uuid, e)

    def _parse_ffmpeg_line(self, line: str):
        """Parse a single line of FFmpeg stderr output."""
        # Stream info detection
        match = STREAM_INFO_RE.search(line)
        if match:
            stream_type = match.group(1)
            codec = match.group(2)
            if stream_type == "Video":
                self._stream_info["video_codec"] = codec
                if match.group(3) and match.group(4):
                    width = match.group(3)
                    height = match.group(4)
                    self._stream_info["resolution"] = f"{width}x{height}"
                    self._update_metadata("resolution", f"{width}x{height}")
                if match.group(5):
                    self._stream_info["fps"] = match.group(5)
                    self._update_metadata("fps", match.group(5))
                if match.group(6):
                    self._stream_info["bitrate"] = f"{match.group(6)}kb/s"
                self._update_metadata("video_codec", codec)
            elif stream_type == "Audio":
                self._stream_info["audio_codec"] = codec
                self._update_metadata("audio_codec", codec)
            return

        # Progress line
        progress = PROGRESS_RE.search(line)
        if progress:
            speed = progress.group(1)
            self._update_metadata("speed", f"{speed}x")

        fps_match = FPS_RE.search(line)
        if fps_match:
            self._update_metadata("current_fps", fps_match.group(1))

        bitrate_match = BITRATE_RE.search(line)
        if bitrate_match:
            self._update_metadata("current_bitrate", f"{bitrate_match.group(1)}kb/s")

        # Error detection
        if "error" in line.lower() and "error hiding" not in line.lower():
            logger.warning("FFmpeg error for %s: %s", self.channel_uuid, line)

    def _update_metadata(self, field: str, value: str):
        """Store metadata in Redis for dashboard display."""
        try:
            rc = self.redis_client
            if rc is None:
                return
            key = f"hls_output:{self.channel_uuid}:meta:{field}"
            rc.setex(key, 300, value)  # 5 minute TTL
        except Exception:
            pass

    def get_stat(self, field: str) -> Optional[str]:
        """Get a metadata value from Redis."""
        try:
            rc = self.redis_client
            if rc is None:
                return None
            key = f"hls_output:{self.channel_uuid}:meta:{field}"
            data = rc.get(key)
            if data:
                return data.decode("utf-8") if isinstance(data, bytes) else data
            return None
        except Exception:
            return None
