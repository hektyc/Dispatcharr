# HLS Output Manager
# Manages FFmpeg processes for HLS segment generation

import os
import time
import subprocess
import threading
import shutil
import shlex
import logging
from typing import Dict, Optional, Tuple
from .config import hls_config

logger = logging.getLogger(__name__)


def get_direct_stream_url(channel) -> Tuple[Optional[str], Optional[str]]:
    """
    Get the direct stream URL and user agent for a channel.

    This bypasses the TS proxy and gets the actual source URL directly,
    avoiding circular dependencies when HLS output consumes from TS proxy.

    Args:
        channel: Channel model instance

    Returns:
        Tuple[stream_url, user_agent]: The direct stream URL and user agent, or (None, None) on error
    """
    try:
        from apps.proxy.ts_proxy.url_utils import transform_url

        # Get stream and profile for this channel
        stream_id, profile_id, error_reason = channel.get_stream()

        if not stream_id or not profile_id:
            logger.error(f"No stream available for channel {channel.uuid}: {error_reason}")
            return None, None

        # Get the stream and M3U profile
        from apps.channels.models import Stream
        from apps.m3u.models import M3UAccountProfile

        stream = Stream.objects.get(pk=stream_id)
        m3u_profile = M3UAccountProfile.objects.get(pk=profile_id)

        # Get user agent from M3U account
        m3u_account = stream.m3u_account
        user_agent = m3u_account.get_user_agent().user_agent

        # Transform URL using M3U profile patterns
        stream_url = transform_url(
            stream.url,
            m3u_profile.search_pattern,
            m3u_profile.replace_pattern
        )

        logger.debug(f"Got direct stream URL for channel {channel.uuid}: {stream_url[:50]}...")
        return stream_url, user_agent

    except Exception as e:
        logger.error(f"Error getting direct stream URL for channel {channel.uuid}: {e}")
        return None, None


class HLSChannelSession:
    """Manages HLS output for a single channel."""

    def __init__(self, channel_uuid: str, stream_url: str, user_agent: str = None, channel=None):
        self.channel_uuid = channel_uuid
        self.stream_url = stream_url  # Direct stream URL (not TS proxy URL)
        self.user_agent_override = user_agent  # User agent from M3U account
        self.channel = channel  # Store channel reference for profile lookup
        self.process: Optional[subprocess.Popen] = None
        self.output_path = hls_config.get_channel_path(channel_uuid)
        self.is_running = False
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stream_profile = None  # Cache the stream profile

    def _ensure_output_directory(self):
        """
        Ensure the HLS output directory exists and is writable.

        This is called before starting FFmpeg to guarantee the directory
        exists and has proper write permissions.

        Returns:
            bool: True if directory exists and is writable, False otherwise
        """
        try:
            # Create directory if it doesn't exist
            if not os.path.exists(self.output_path):
                os.makedirs(self.output_path, mode=0o755, exist_ok=True)
                logger.info(f"Created HLS output directory: {self.output_path}")

            # Verify directory is writable by creating a test file
            test_file = os.path.join(self.output_path, ".write_test")
            try:
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
                logger.debug(f"HLS output directory verified writable: {self.output_path}")
                return True
            except (IOError, OSError) as e:
                logger.error(f"HLS output directory {self.output_path} is not writable: {e}")
                return False

        except Exception as e:
            logger.error(f"Failed to ensure HLS output directory {self.output_path}: {e}")
            return False

    def _get_stream_profile(self):
        """Get the HLS stream profile for this channel."""
        if self._stream_profile:
            return self._stream_profile

        if self.channel:
            try:
                self._stream_profile = self.channel.get_hls_stream_profile()
                return self._stream_profile
            except Exception as e:
                logger.warning(f"Could not get HLS profile for channel {self.channel_uuid}: {e}")

        # Fall back to default HLS FFmpeg profile
        from core.models import StreamProfile, HLS_FFMPEG_PROFILE_NAME, PROFILE_TYPE_HLS
        try:
            self._stream_profile = StreamProfile.objects.get(
                name=HLS_FFMPEG_PROFILE_NAME,
                locked=True
            )
            return self._stream_profile
        except StreamProfile.DoesNotExist:
            logger.error("Default HLS FFmpeg profile not found!")
            return None

    def _get_user_agent(self):
        """Get the user agent string for this session.

        Priority:
        1. User agent override (from M3U account via get_direct_stream_url)
        2. User agent from the HLS stream profile
        3. Default user agent
        """
        # First, use the override if provided (this comes from the M3U account)
        if self.user_agent_override:
            return self.user_agent_override

        # Otherwise, try the profile's user agent
        profile = self._get_stream_profile()
        if profile and profile.user_agent:
            return profile.user_agent.user_agent

        # Default user agent
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def start(self):
        """Start the FFmpeg process for HLS output."""
        if self.is_running:
            logger.warning(f"HLS session for {self.channel_uuid} already running")
            return False

        # Ensure output directory exists before starting FFmpeg
        if not self._ensure_output_directory():
            logger.error(f"Failed to create output directory for {self.channel_uuid}")
            return False

        # Clean up any stale segments
        self._cleanup_segments()

        # Build FFmpeg command
        cmd = self._build_ffmpeg_command()
        if not cmd:
            logger.error(f"Failed to build FFmpeg command for {self.channel_uuid}")
            return False

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
        """
        Build the FFmpeg command for HLS output using the Stream Profile system.

        Uses the channel's HLS stream profile if available, otherwise falls back
        to the default HLS FFmpeg profile. The profile's command and parameters
        are used with placeholder substitution for {streamUrl}, {userAgent}, and
        {hlsOutputPath}.

        Returns:
            List of command arguments, or empty list if using HLS Proxy profile
        """
        profile = self._get_stream_profile()

        if not profile:
            # No profile found, use fallback hardcoded command
            logger.warning(f"No HLS profile found for {self.channel_uuid}, using fallback")
            return self._build_fallback_command()

        # Check if this is an HLS Proxy profile (no FFmpeg needed)
        if profile.is_hls_proxy():
            logger.info(f"HLS Proxy profile detected for {self.channel_uuid} - passthrough mode")
            # HLS Proxy means the source is already HLS, just serve it
            # This would require different handling (proxying HLS directly)
            return []

        # Get user agent
        user_agent = self._get_user_agent()

        # Build command using profile
        try:
            cmd = profile.build_command(
                stream_url=self.stream_url,
                user_agent=user_agent,
                hls_output_path=self.output_path
            )

            if cmd:
                # Add hide_banner and loglevel for cleaner output
                # Insert after 'ffmpeg' command but before other arguments
                if cmd[0] == "ffmpeg":
                    cmd.insert(1, "-hide_banner")
                    cmd.insert(2, "-loglevel")
                    cmd.insert(3, "warning")

                logger.debug(f"Built HLS command from profile '{profile.name}': {cmd}")
                return cmd
            else:
                # Empty command means proxy mode or error
                logger.warning(f"Profile '{profile.name}' returned empty command")
                return self._build_fallback_command()

        except Exception as e:
            logger.error(f"Error building command from profile '{profile.name}': {e}")
            return self._build_fallback_command()

    def _build_fallback_command(self):
        """Build a fallback FFmpeg command when no profile is available."""
        segment_duration = hls_config.segment_duration
        playlist_size = hls_config.playlist_size
        playlist_path = os.path.join(self.output_path, "stream.m3u8")
        segment_pattern = os.path.join(self.output_path, "segment_%05d.ts")
        user_agent = self._get_user_agent()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            # Input options - must come before -i
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-user_agent", user_agent,
            "-i", self.stream_url,
            # Output options
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
        """Remove all HLS segments and playlist files for this channel.

        Note: This only removes files inside the directory, not the directory itself.
        The directory must remain for FFmpeg to write new segments.
        """
        try:
            if os.path.exists(self.output_path):
                # Remove only the files inside, not the directory itself
                files_removed = 0
                for filename in os.listdir(self.output_path):
                    file_path = os.path.join(self.output_path, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            files_removed += 1
                    except Exception as e:
                        logger.warning(f"Failed to remove file {file_path}: {e}")

                if files_removed > 0:
                    logger.info(f"Cleaned up {files_removed} HLS files for {self.channel_uuid}")
                else:
                    logger.debug(f"No HLS files to clean up for {self.channel_uuid}")
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

    def get_or_start_session(
        self,
        channel_uuid: str,
        stream_url: str,
        user_agent: str = None,
        channel=None
    ) -> Optional[HLSChannelSession]:
        """
        Get an existing HLS session or start a new one.

        Args:
            channel_uuid: The UUID of the channel
            stream_url: The direct stream URL (not TS proxy URL)
            user_agent: Optional user agent from the M3U account
            channel: Optional Channel model instance for profile lookup

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

            # Create new session with direct stream URL and channel reference
            session = HLSChannelSession(
                channel_uuid,
                stream_url,
                user_agent=user_agent,
                channel=channel
            )
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

