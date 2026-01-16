# HLS Output Manager
# Manages FFmpeg processes for HLS segment generation

import os
import re
import time
import subprocess
import threading
import shutil
import shlex
import logging
from typing import Dict, Optional, Tuple
from .config import hls_config
from .client_manager import hls_client_manager

logger = logging.getLogger(__name__)


def get_channel_or_stream(identifier: str):
    """
    Get a Channel or Stream object by UUID or stream_hash.

    This mirrors the TS proxy's get_stream_object function to provide
    consistent behavior when previewing streams vs channels.

    Args:
        identifier: Channel UUID or Stream hash

    Returns:
        Tuple[Channel|Stream|None, str|None]: (object, identifier_to_use)
        For channels, identifier_to_use is the channel UUID
        For streams, identifier_to_use is the stream_hash
    """
    from apps.channels.models import Channel, Stream

    # Try as channel UUID first
    try:
        channel = Channel.objects.get(uuid=identifier)
        logger.debug(f"HLS: Found channel by UUID: {identifier}")
        return channel, str(channel.uuid)
    except Channel.DoesNotExist:
        pass
    except Exception as e:
        logger.debug(f"HLS: Error looking up channel {identifier}: {e}")

    # Try as stream hash
    try:
        stream = Stream.objects.get(stream_hash=identifier)
        logger.debug(f"HLS: Found stream by hash: {identifier}")
        return stream, stream.stream_hash
    except Stream.DoesNotExist:
        pass
    except Exception as e:
        logger.debug(f"HLS: Error looking up stream {identifier}: {e}")

    logger.warning(f"HLS: No channel or stream found for identifier: {identifier}")
    return None, None


def get_direct_stream_url_for_stream(stream) -> Tuple[Optional[str], Optional[str]]:
    """
    Get the direct stream URL and user agent for a Stream object.

    Args:
        stream: Stream model instance

    Returns:
        Tuple[stream_url, user_agent]: The direct stream URL and user agent
    """
    try:
        from apps.proxy.ts_proxy.url_utils import transform_url

        m3u_account = stream.m3u_account
        if not m3u_account:
            logger.error(f"Stream {stream.id} has no M3U account")
            return None, None

        # Get active default profile
        m3u_profiles = m3u_account.profiles.filter(is_active=True)
        m3u_profile = next((p for p in m3u_profiles if p.is_default), None)

        if not m3u_profile:
            # Fall back to any active profile
            m3u_profile = m3u_profiles.first()

        if not m3u_profile:
            logger.error(f"No active profile for M3U account {m3u_account.id}")
            return None, None

        user_agent = m3u_account.get_user_agent().user_agent
        stream_url = transform_url(
            stream.url,
            m3u_profile.search_pattern,
            m3u_profile.replace_pattern
        )

        logger.debug(f"Got direct stream URL for stream {stream.stream_hash}: {stream_url[:50]}...")
        return stream_url, user_agent

    except Exception as e:
        logger.error(f"Error getting direct stream URL for stream: {e}")
        return None, None


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


def get_stream_metadata(channel):
    """
    Get stream metadata for Active Connections display.

    Args:
        channel: Channel model instance

    Returns:
        dict: Stream metadata including stream_id, stream_name, m3u_profile info,
              or empty dict on error
    """
    try:
        # Get stream and profile for this channel
        stream_id, profile_id, error_reason = channel.get_stream()

        if not stream_id or not profile_id:
            return {}

        # Get the stream and M3U profile
        from apps.channels.models import Stream
        from apps.m3u.models import M3UAccountProfile

        stream = Stream.objects.get(pk=stream_id)
        m3u_profile = M3UAccountProfile.objects.get(pk=profile_id)

        # Get M3U account info
        m3u_account = stream.m3u_account

        # Get HLS stream profile
        hls_profile = channel.get_hls_stream_profile()

        return {
            "stream_id": str(stream_id),
            "stream_name": stream.name,
            "m3u_profile_id": str(profile_id),
            "m3u_profile_name": m3u_profile.name,
            "m3u_account_name": m3u_account.name if m3u_account else "Unknown",
            "stream_profile": str(hls_profile.id) if hls_profile else "",
            "stream_profile_name": hls_profile.name if hls_profile else "Unknown",
        }

    except Exception as e:
        logger.error(f"Error getting stream metadata for channel {channel.uuid}: {e}")
        return {}


class HLSChannelSession:
    """Manages HLS output for a single channel."""

    def __init__(self, channel_uuid: str, stream_url: str, user_agent: str = None, channel=None,
                 stream_metadata: dict = None):
        self.channel_uuid = channel_uuid
        self.stream_url = stream_url  # Direct stream URL (not TS proxy URL)
        self.user_agent_override = user_agent  # User agent from M3U account
        self.channel = channel  # Store channel reference for profile lookup
        self.stream_metadata = stream_metadata or {}  # Stream metadata for Active Connections
        self.process: Optional[subprocess.Popen] = None
        self._cached_output_path = None  # Cached during session for consistency
        self.is_running = False
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stream_profile = None  # Cache the stream profile
        self._ffmpeg_input_phase = True  # Track if we're parsing input info (before output phase)
        # Track tried streams for automatic failover
        self._tried_stream_ids: set = set()
        self._current_stream_id: Optional[int] = None
        # Extract current stream ID from metadata if available
        if stream_metadata and 'stream_id' in stream_metadata:
            try:
                self._current_stream_id = int(stream_metadata['stream_id'])
                self._tried_stream_ids.add(self._current_stream_id)
            except (ValueError, TypeError):
                pass

    @property
    def output_path(self):
        """Get the output path for this session.

        Returns the cached path if set, otherwise gets fresh path from config.
        The path is cached once start() is called to ensure consistency during
        the session (so FFmpeg writes to same location as we're reading from).
        """
        if self._cached_output_path is None:
            self._cached_output_path = hls_config.get_channel_path(self.channel_uuid)
            logger.info(f"HLS session output path for {self.channel_uuid}: {self._cached_output_path}")
        return self._cached_output_path

    def _refresh_output_path(self):
        """Refresh the output path from current config settings.

        This should be called at the start of a new session to pick up
        any changes to the HLS output path setting. The path is then cached
        for the duration of the session.
        """
        self._cached_output_path = hls_config.get_channel_path(self.channel_uuid)
        logger.info(f"Refreshed HLS output path for {self.channel_uuid}: {self._cached_output_path}")

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

    def start(self, preserve_segments: bool = False):
        """Start the FFmpeg process for HLS output.

        Args:
            preserve_segments: If True, don't clean up existing segments.
                             This is used during automatic stream switch to allow
                             seamless transition - the player can continue fetching
                             existing segments while FFmpeg starts writing new ones.
                             The hls_flags append_list ensures segment numbering continues.
        """
        if self.is_running:
            logger.warning(f"HLS session for {self.channel_uuid} already running")
            return False

        # Refresh output path from current config settings
        # This ensures we pick up any changes to the HLS output path
        self._refresh_output_path()

        # Ensure output directory exists before starting FFmpeg
        if not self._ensure_output_directory():
            logger.error(f"Failed to create output directory for {self.channel_uuid}")
            return False

        # Clean up any stale segments UNLESS we're preserving them for seamless switch
        if preserve_segments:
            logger.info(f"HLS {self.channel_uuid}: Preserving existing segments for seamless stream switch")
        else:
            self._cleanup_segments()

        # Build FFmpeg command
        cmd = self._build_ffmpeg_command()
        if not cmd:
            logger.error(f"Failed to build FFmpeg command for {self.channel_uuid}")
            return False

        # Extract hls_list_size from command for logging
        hls_list_size_value = "unknown"
        for i, arg in enumerate(cmd):
            if arg == "-hls_list_size" and i + 1 < len(cmd):
                hls_list_size_value = cmd[i + 1]
                break
        logger.info(f"Starting HLS output for {self.channel_uuid} with hls_list_size={hls_list_size_value}")
        logger.info(f"Full FFmpeg command: {' '.join(cmd)}")

        try:
            # Note: HLS output writes to files, not stdout, so we use DEVNULL
            # Using PIPE for stdout would cause buffer deadlock since FFmpeg
            # might write some data to stdout that never gets read
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
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

            # Notify client manager that channel is active
            # IMPORTANT: Pass PID so any worker can stop the process
            # Also pass stream metadata for Active Connections display
            hls_client_manager.set_channel_active(
                self.channel_uuid,
                self.stream_url,
                pid=self.process.pid,
                stream_metadata=self.stream_metadata
            )

            logger.info(f"HLS output started for {self.channel_uuid}, PID: {self.process.pid}")
            return True
        except Exception as e:
            logger.error(f"Failed to start HLS output for {self.channel_uuid}: {e}")
            self.is_running = False
            return False

    def stop(self, cleanup: bool = True, release_ownership: bool = True):
        """Stop the FFmpeg process and optionally cleanup.

        Args:
            cleanup: If True (default), cleanup HLS files based on retention settings.
                    If False, keep the HLS directory for session restart/switching.
            release_ownership: If True (default), release channel ownership in Redis.
                              If False, keep ownership for immediate session restart.
        """
        if not self.is_running:
            return

        logger.info(f"Stopping HLS output for {self.channel_uuid} (cleanup={cleanup})")
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

        if cleanup:
            # Notify client manager that channel is inactive
            hls_client_manager.set_channel_inactive(self.channel_uuid)

            # Cleanup based on retention settings
            # Use _cleanup_all() to remove both files AND directory
            retention = hls_config.retention_seconds
            if retention == 0:
                self._cleanup_all()
            else:
                # Schedule delayed cleanup
                threading.Timer(retention, self._cleanup_all).start()

        if release_ownership:
            # Release ownership so other workers know this channel is stopped
            hls_manager._release_ownership(self.channel_uuid)

    def _is_source_hls(self):
        """Check if the source URL is an HLS stream (ends with .m3u8)."""
        if not self.stream_url:
            return False
        # Check if URL ends with .m3u8 (case insensitive)
        url_lower = self.stream_url.lower().split('?')[0]  # Remove query params
        return url_lower.endswith('.m3u8')

    def _build_ffmpeg_command(self):
        """
        Build the FFmpeg command for HLS output using the Stream Profile system.

        Uses the channel's HLS stream profile if available, otherwise falls back
        to the default HLS FFmpeg profile. The profile's command and parameters
        are used with placeholder substitution for {streamUrl}, {userAgent},
        {hlsOutputPath}, {segmentDuration}, {playlistSize}, and {segmentExtension}.

        For HLS Proxy profile:
        - Returns fallback command since Proxy profile returns empty (no command)

        For HLS FFmpeg profile:
        - Uses build_command() with full HLS config including fMP4 settings
        - The profile uses {segmentExtension} placeholder for dynamic .ts/.m4s

        Returns:
            List of command arguments for FFmpeg
        """
        profile = self._get_stream_profile()

        if not profile:
            # No profile found, use fallback hardcoded command
            logger.warning(f"No HLS profile found for {self.channel_uuid}, using fallback")
            return self._build_fallback_command()

        # HLS Proxy returns empty command - use fallback for actual streaming
        if profile.is_hls_proxy():
            if self._is_source_hls():
                logger.info(f"HLS {profile.name}: source is HLS for {self.channel_uuid}, using remux")
            else:
                logger.info(f"HLS {profile.name}: source is MPEG-TS for {self.channel_uuid}, using remux")
            return self._build_fallback_command()

        # Get user agent
        user_agent = self._get_user_agent()

        # Build HLS config dict for placeholder substitution
        # Include use_fmp4_segments for dynamic segment extension
        use_fmp4 = hls_config.use_fmp4_segments  # Includes LL-HLS check
        hls_config_dict = {
            "segment_duration": hls_config.segment_duration,
            "playlist_size": hls_config.playlist_size,
            "use_fmp4_segments": use_fmp4,
        }

        logger.info(f"HLS {self.channel_uuid}: Building command from profile '{profile.name}' with segment_duration={hls_config_dict['segment_duration']}, playlist_size={hls_config_dict['playlist_size']} (hls_list_size), fmp4={use_fmp4}")
        logger.info(f"HLS {self.channel_uuid}: Profile parameters template: {profile.parameters}")

        # Build command using profile
        try:
            cmd = profile.build_command(
                stream_url=self.stream_url,
                user_agent=user_agent,
                hls_output_path=self.output_path,
                hls_config=hls_config_dict
            )

            if cmd:
                # Add hide_banner for cleaner output
                # Use loglevel 'info' to capture stream info (Video:/Audio: lines)
                # Note: 'warning' would suppress stream info needed for stats display
                if cmd[0] == "ffmpeg":
                    cmd.insert(1, "-hide_banner")
                    cmd.insert(2, "-loglevel")
                    cmd.insert(3, "info")

                # Log the final command to verify placeholder substitution worked
                logger.info(f"HLS {self.channel_uuid}: Built command from profile '{profile.name}': {' '.join(cmd)}")
                return cmd
            else:
                # Empty command means proxy mode or error
                logger.warning(f"Profile '{profile.name}' returned empty command")
                return self._build_fallback_command()

        except Exception as e:
            logger.error(f"Error building command from profile '{profile.name}': {e}")
            return self._build_fallback_command()

    def _build_fallback_command(self):
        """Build a fallback FFmpeg command when no profile is available.

        Reconnect flags are included with a short timeout (2 seconds):
        - FFmpeg handles brief network hiccups via reconnect
        - If reconnection fails after 2 seconds, Dispatcharr's automatic stream
          switch takes over and tries backup streams

        IMPORTANT: We do NOT use the delete_segments flag because:
        1. When FFmpeg runs faster than real-time (which it does initially),
           segments are deleted before clients can request them
        2. This causes playback failures and "segment not found" errors
        3. Segments are cleaned up when the session ends via _cleanup_all()
        """
        # Log what we're reading from the config to debug settings issues
        # Get all settings via the public properties (which read from database)
        segment_duration = hls_config.segment_duration
        playlist_size = hls_config.playlist_size
        ll_hls_enabled = hls_config.ll_hls_enabled
        use_fmp4 = hls_config.use_fmp4_segments  # Includes LL-HLS check

        # Also log all settings for debugging
        all_settings = hls_config.to_dict()
        logger.info(f"HLS {self.channel_uuid}: ALL SETTINGS FROM CONFIG: {all_settings}")
        logger.info(f"HLS {self.channel_uuid}: Building FALLBACK FFmpeg command with segment_duration={segment_duration}, playlist_size={playlist_size} (hls_list_size), ll_hls={ll_hls_enabled}, fmp4={use_fmp4}")

        playlist_path = os.path.join(self.output_path, "index.m3u8")
        user_agent = self._get_user_agent()

        # Build HLS flags
        # - append_list: Append to playlist instead of overwriting
        # - program_date_time: Add EXT-X-PROGRAM-DATE-TIME for better player sync
        # NOTE: We intentionally do NOT use delete_segments - see docstring above
        hls_flags = "append_list+program_date_time"

        # Determine segment extension based on fMP4 setting
        # fMP4 uses .m4s segments, regular HLS uses MPEG-TS (.ts)
        # Note: use_fmp4 is automatically true when LL-HLS is enabled
        if use_fmp4:
            segment_ext = "m4s"
            logger.info(f"HLS {self.channel_uuid}: Using fMP4 segments (.m4s)")
        else:
            segment_ext = "ts"
            logger.info(f"HLS {self.channel_uuid}: Using MPEG-TS segments (.ts)")

        # LL-HLS requires additional flags for lower latency
        if ll_hls_enabled:
            hls_flags += "+independent_segments"
            # Note: True LL-HLS requires HTTP/2 for server push. Without HTTP/2,
            # the benefit is limited to slightly faster segment availability.

        # Use %d instead of %05d to allow unlimited segment numbers (no 5-digit limit)
        # This supports indefinite streaming without segment number overflow
        segment_pattern = os.path.join(self.output_path, f"index%d.{segment_ext}")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",  # Need 'info' to capture stream info for stats display
            # Input options - must come before -i
            # Reconnect flags: Handle brief upstream disconnections gracefully
            # - FFmpeg handles hiccups < 2 seconds via reconnect
            # - Dispatcharr's automatic stream switch handles permanent failures
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",  # Short timeout so auto-switch can take over
            "-user_agent", user_agent,
            # Input buffer to handle source instability (prevents short freezes)
            # +igndts: Ignore DTS (decode timestamp) when PTS is available - prevents
            #          timestamp issues when source has inconsistent DTS values
            "-fflags", "+genpts+discardcorrupt+igndts",
            "-analyzeduration", "5000000",  # 5 seconds to analyze input
            "-probesize", "5000000",  # 5MB probe size
            "-i", self.stream_url,
            # Output options - Enterprise-level flags for smooth channel transitions
            "-c", "copy",  # Copy without re-encoding
            # Timestamp handling for seamless stream switches:
            # -copyts: Copy timestamps from input without modification
            # -start_at_zero: Start output timestamps at zero (with copyts)
            # -avoid_negative_ts make_zero: Shift negative timestamps to zero
            "-copyts",
            "-start_at_zero",
            "-avoid_negative_ts", "make_zero",
            # Reduce muxing delay for faster segment availability
            "-max_delay", "0",
            # HLS muxer options
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_list_size", str(playlist_size),
            "-hls_flags", hls_flags,
            # Prevent client caching issues during channel changes
            "-hls_allow_cache", "0",
            # Use default sequential numbering (0, 1, 2, ...)
            # No -hls_start_number_source means segments start at 0
            "-hls_segment_filename", segment_pattern,
        ]

        # Add fMP4 container format options (required for fMP4 segments)
        if use_fmp4:
            cmd.extend([
                # AAC ADTS to ASC conversion is REQUIRED for fMP4/MP4 container
                # MPEG-TS streams have AAC in ADTS format, but MP4/fMP4 requires ASC format
                # Without this, FFmpeg fails with "Malformed AAC bitstream detected"
                "-bsf:a", "aac_adtstoasc",
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_type", "fmp4",
            ])

        cmd.append(playlist_path)
        return cmd

    def _monitor_process(self):
        """Monitor FFmpeg process, parse output for stream info and stats."""
        self._ffmpeg_input_phase = True  # Track if we're still parsing input info

        # Start stderr reader thread
        stderr_thread = threading.Thread(
            target=self._read_ffmpeg_stderr,
            daemon=True,
            name=f"hls-stderr-{self.channel_uuid[:8]}"
        )
        stderr_thread.start()

        # Track time for periodic ownership refresh
        last_ownership_refresh = time.time()
        OWNERSHIP_REFRESH_INTERVAL = 30  # seconds

        while not self._stop_event.is_set() and self.process:
            if self.process.poll() is not None:
                # Process ended - check if it was an intentional shutdown
                if not self._stop_event.is_set():
                    # Check if the channel was already marked inactive in Redis
                    # This happens when client_manager kills the process due to no clients
                    if not hls_client_manager.is_channel_active(self.channel_uuid):
                        logger.info(f"HLS {self.channel_uuid}: FFmpeg exited, channel already inactive (intentional shutdown)")
                        self.is_running = False
                        break

                    logger.error(f"FFmpeg for {self.channel_uuid} exited unexpectedly")
                    self.is_running = False

                    # Try to switch to a backup stream automatically
                    if self._try_automatic_stream_switch():
                        logger.info(f"HLS {self.channel_uuid}: Automatic stream switch successful")
                        # Don't break - the new session will have its own monitor
                        return
                    else:
                        logger.warning(f"HLS {self.channel_uuid}: No backup streams available, cleaning up")
                        # Cleanup segments when process exits unexpectedly and no backup available
                        self._cleanup_all()
                        # Release ownership so another worker can take over if needed
                        hls_manager._release_ownership(self.channel_uuid)
                break

            # Refresh ownership periodically to prevent TTL expiry
            if time.time() - last_ownership_refresh > OWNERSHIP_REFRESH_INTERVAL:
                hls_manager._refresh_ownership(self.channel_uuid)
                last_ownership_refresh = time.time()

            time.sleep(1)

    def _read_ffmpeg_stderr(self):
        """Read and parse FFmpeg stderr output for stream info and stats."""
        logger.info(f"HLS {self.channel_uuid} stderr reader started")
        try:
            # Debug: Check if process and stderr are available
            if not self.process:
                logger.error(f"HLS {self.channel_uuid} stderr reader: process is None!")
                return
            if not self.process.stderr:
                logger.error(f"HLS {self.channel_uuid} stderr reader: process.stderr is None!")
                return

            logger.info(f"HLS {self.channel_uuid} stderr reader: process PID={self.process.pid}, stderr={self.process.stderr}")

            buffer = b""
            bytes_read = 0
            while self.process and self.process.stderr:
                try:
                    byte = self.process.stderr.read(1)
                    if not byte:
                        logger.warning(f"HLS {self.channel_uuid} stderr reader: read returned empty after {bytes_read} bytes")
                        break

                    bytes_read += 1
                    if bytes_read <= 10 or bytes_read % 1000 == 0:
                        logger.debug(f"HLS {self.channel_uuid} stderr reader: bytes_read={bytes_read}")

                    buffer += byte

                    # Check for frame= at the start of buffer (stats line)
                    if buffer == b"frame=":
                        while True:
                            next_byte = self.process.stderr.read(1)
                            if not next_byte:
                                break
                            buffer += next_byte
                            if next_byte in (b'\r', b'\n'):
                                break
                            if len(buffer) > 200:
                                break

                        if buffer.strip():
                            try:
                                stats_text = buffer.decode('utf-8', errors='ignore').strip()
                                if stats_text and "frame=" in stats_text:
                                    self._parse_ffmpeg_stats(stats_text)
                            except Exception as e:
                                logger.debug(f"Error parsing stats: {e}")
                        buffer = b""
                        continue

                    # Handle line breaks
                    elif byte == b'\n':
                        if buffer.strip():
                            line_text = buffer.decode('utf-8', errors='ignore').strip()
                            self._process_ffmpeg_line(line_text)
                        buffer = b""

                    # Handle carriage returns
                    elif byte == b'\r':
                        if b"frame=" in buffer:
                            try:
                                stats_text = buffer.decode('utf-8', errors='ignore').strip()
                                if stats_text and "frame=" in stats_text:
                                    self._parse_ffmpeg_stats(stats_text)
                            except Exception as e:
                                logger.debug(f"Error parsing stats: {e}")
                        elif buffer.strip():
                            line_text = buffer.decode('utf-8', errors='ignore').strip()
                            self._process_ffmpeg_line(line_text)
                        buffer = b""

                    # Prevent buffer overflow
                    elif len(buffer) > 1024 and b"frame=" not in buffer:
                        if buffer.strip():
                            line_text = buffer.decode('utf-8', errors='ignore').strip()
                            self._process_ffmpeg_line(line_text)
                        buffer = b""

                except Exception as e:
                    logger.debug(f"Error reading stderr byte: {e}")
                    break

        except Exception as e:
            logger.debug(f"Error in stderr reader for {self.channel_uuid}: {e}")

    def _process_ffmpeg_line(self, line):
        """Process a line of FFmpeg output."""
        if not line:
            return

        line_lower = line.lower()

        # Track FFmpeg phases
        if line_lower.startswith('input #') or 'decoder' in line_lower:
            self._ffmpeg_input_phase = True
            logger.debug(f"HLS {self.channel_uuid} entering input phase")
        if line_lower.startswith('output #') or 'encoder' in line_lower:
            self._ffmpeg_input_phase = False
            logger.debug(f"HLS {self.channel_uuid} entering output phase")

        # Parse stream info during input phase
        if ("stream #" in line_lower and
            ("video:" in line_lower or "audio:" in line_lower) and
            self._ffmpeg_input_phase):
            logger.info(f"HLS {self.channel_uuid} found stream line (input_phase={self._ffmpeg_input_phase}): {line}")
            if "video:" in line_lower:
                self._parse_stream_info(line, "video")
            elif "audio:" in line_lower:
                self._parse_stream_info(line, "audio")
        elif "stream #" in line_lower and ("video:" in line_lower or "audio:" in line_lower):
            # Log when we skip stream lines because we're in output phase
            logger.debug(f"HLS {self.channel_uuid} skipping stream line (output phase): {line}")

        # Parse input format
        if line_lower.startswith('input #0'):
            self._parse_input_format(line)

        # Log errors and warnings
        if any(kw in line_lower for kw in ['error', 'failed', 'cannot', 'invalid']):
            logger.error(f"FFmpeg HLS {self.channel_uuid}: {line}")
        elif any(kw in line_lower for kw in ['warning', 'deprecated']):
            logger.warning(f"FFmpeg HLS {self.channel_uuid}: {line}")
        elif any(kw in line_lower for kw in ['input', 'output', 'stream', 'video', 'audio']):
            logger.info(f"FFmpeg HLS {self.channel_uuid}: {line}")

    def _parse_input_format(self, line):
        """Parse input format from FFmpeg output (e.g., mpegts, hls, flv)."""
        try:
            match = re.search(r'Input #\d+,\s*([^,]+)', line)
            if match:
                input_format = match.group(1).strip()
                self._update_metadata_field("stream_type", input_format)
                logger.debug(f"HLS {self.channel_uuid} input format: {input_format}")
        except Exception as e:
            logger.debug(f"Error parsing input format: {e}")

    def _parse_stream_info(self, line, stream_type):
        """Parse video or audio stream info from FFmpeg output."""
        try:
            logger.info(f"HLS {self.channel_uuid} parsing {stream_type} info from: {line}")

            if stream_type == "video":
                # Parse video codec (e.g., h264, hevc, mpeg2video)
                codec_match = re.search(r'Video:\s*(\w+)', line, re.IGNORECASE)
                if codec_match:
                    codec = codec_match.group(1)
                    self._update_metadata_field("video_codec", codec)
                    logger.info(f"HLS {self.channel_uuid} video codec: {codec}")

                # Parse resolution (e.g., 1920x1080)
                res_match = re.search(r'(\d{2,5})x(\d{2,5})', line)
                if res_match:
                    width, height = res_match.groups()
                    resolution = f"{width}x{height}"
                    self._update_metadata_field("resolution", resolution)
                    self._update_metadata_field("width", width)
                    self._update_metadata_field("height", height)
                    logger.info(f"HLS {self.channel_uuid} resolution: {resolution}")

                # Parse FPS (e.g., 29.97 fps, 30 tbr)
                fps_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:fps|tbr)', line)
                if fps_match:
                    fps = fps_match.group(1)
                    self._update_metadata_field("source_fps", fps)
                    logger.info(f"HLS {self.channel_uuid} source_fps: {fps}")

                # Parse pixel format (e.g., yuv420p)
                pix_match = re.search(r'(yuv\d+p|rgb\d+|bgr\d+)', line, re.IGNORECASE)
                if pix_match:
                    pix_fmt = pix_match.group(1)
                    self._update_metadata_field("pixel_format", pix_fmt)
                    logger.debug(f"HLS {self.channel_uuid} pixel_format: {pix_fmt}")

                # Parse video bitrate
                bitrate_match = re.search(r'(\d+(?:\.\d+)?)\s*kb/s', line)
                if bitrate_match:
                    bitrate = bitrate_match.group(1)
                    self._update_metadata_field("video_bitrate", bitrate)
                    logger.debug(f"HLS {self.channel_uuid} video_bitrate: {bitrate}")

            elif stream_type == "audio":
                # Parse audio codec (e.g., aac, mp3, ac3)
                codec_match = re.search(r'Audio:\s*(\w+)', line, re.IGNORECASE)
                if codec_match:
                    codec = codec_match.group(1)
                    self._update_metadata_field("audio_codec", codec)
                    logger.info(f"HLS {self.channel_uuid} audio codec: {codec}")

                # Parse sample rate (e.g., 48000 Hz)
                rate_match = re.search(r'(\d+)\s*Hz', line)
                if rate_match:
                    rate = rate_match.group(1)
                    self._update_metadata_field("sample_rate", rate)
                    logger.debug(f"HLS {self.channel_uuid} sample_rate: {rate}")

                # Parse audio channels (e.g., stereo, 5.1, mono)
                if 'stereo' in line.lower():
                    self._update_metadata_field("audio_channels", "stereo")
                    logger.info(f"HLS {self.channel_uuid} audio_channels: stereo")
                elif '5.1' in line:
                    self._update_metadata_field("audio_channels", "5.1")
                    logger.info(f"HLS {self.channel_uuid} audio_channels: 5.1")
                elif 'mono' in line.lower():
                    self._update_metadata_field("audio_channels", "mono")
                    logger.info(f"HLS {self.channel_uuid} audio_channels: mono")

                # Parse audio bitrate
                bitrate_match = re.search(r'(\d+(?:\.\d+)?)\s*kb/s', line)
                if bitrate_match:
                    bitrate = bitrate_match.group(1)
                    self._update_metadata_field("audio_bitrate", bitrate)
                    logger.debug(f"HLS {self.channel_uuid} audio_bitrate: {bitrate}")

        except Exception as e:
            logger.error(f"Error parsing {stream_type} stream info: {e}")

    def _parse_ffmpeg_stats(self, stats_line):
        """Parse FFmpeg stats line for speed, fps, bitrate."""
        try:
            # Extract speed (e.g., "speed=1.02x")
            speed_match = re.search(r'speed=\s*([0-9.]+)x?', stats_line)
            if speed_match:
                speed_value = speed_match.group(1)
                self._update_metadata_field("ffmpeg_speed", speed_value)
                logger.debug(f"HLS {self.channel_uuid} parsed speed: {speed_value}")

            # Extract fps (e.g., "fps= 30")
            fps_match = re.search(r'fps=\s*([0-9.]+)', stats_line)
            if fps_match:
                fps_value = fps_match.group(1)
                self._update_metadata_field("ffmpeg_fps", fps_value)
                logger.debug(f"HLS {self.channel_uuid} parsed fps: {fps_value}")

            # Extract bitrate (e.g., "bitrate= 406.1kbits/s" or "bitrate=N/A")
            # Match various formats: kbits/s, Mbits/s, bits/s
            bitrate_match = re.search(r'bitrate=\s*([0-9.]+)\s*([kmg]?)bits/s', stats_line, re.IGNORECASE)
            if bitrate_match:
                bitrate_value = float(bitrate_match.group(1))
                unit = bitrate_match.group(2).lower()
                # Convert to kbps
                if unit == 'm':
                    bitrate_value *= 1000
                elif unit == 'g':
                    bitrate_value *= 1000000
                # If no unit or 'k', it's already in kbps
                self._update_metadata_field("ffmpeg_bitrate", str(round(bitrate_value, 1)))
                logger.debug(f"HLS {self.channel_uuid} parsed bitrate: {bitrate_value} kbps")

        except Exception as e:
            logger.debug(f"Error parsing FFmpeg stats: {e}")

    def _update_metadata_field(self, field, value):
        """Update a single metadata field in Redis.

        Note: Uses 'hls_output:channel:{uuid}:metadata' key pattern to match
        the HLSClientManager's key pattern for consistent metadata storage.
        """
        try:
            from core.utils import RedisClient
            redis_client = RedisClient.get_client()
            if redis_client:
                # Use same key pattern as HLSClientManager for consistency
                metadata_key = f"hls_output:channel:{self.channel_uuid}:metadata"
                redis_client.hset(metadata_key, field, str(value))
                logger.debug(f"HLS {self.channel_uuid} stored {field}={value} in Redis")
            else:
                logger.warning(f"HLS {self.channel_uuid} Redis client not available for {field}={value}")
        except Exception as e:
            logger.error(f"Error updating metadata field {field}: {e}")

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

    def _cleanup_all(self):
        """Remove all HLS files AND the channel directory.

        This is called when the session ends (either normally or unexpectedly)
        to fully clean up all traces of the HLS output.
        """
        try:
            if os.path.exists(self.output_path):
                shutil.rmtree(self.output_path)
                logger.info(f"Cleaned up HLS directory for {self.channel_uuid}: {self.output_path}")
        except Exception as e:
            logger.error(f"Failed to cleanup HLS directory for {self.channel_uuid}: {e}")

    def _try_automatic_stream_switch(self) -> bool:
        """
        Try to automatically switch to a backup stream when the current stream fails.

        This mirrors the TS Proxy's automatic stream switching behavior.
        Creates a new session directly since the current session's FFmpeg has exited.

        Returns:
            bool: True if successfully switched to a new stream, False otherwise
        """
        try:
            from apps.proxy.ts_proxy.url_utils import get_alternate_streams, get_stream_info_for_switch
            from apps.channels.models import Channel

            logger.info(f"HLS {self.channel_uuid}: Attempting automatic stream switch, "
                       f"current stream ID: {self._current_stream_id}, tried: {self._tried_stream_ids}")

            # Get alternate streams excluding ones we've already tried
            alternate_streams = get_alternate_streams(self.channel_uuid, self._current_stream_id)

            if not alternate_streams:
                logger.warning(f"HLS {self.channel_uuid}: No alternate streams available")
                return False

            # Filter out streams we've already tried
            untried_streams = [s for s in alternate_streams if s['stream_id'] not in self._tried_stream_ids]

            if not untried_streams:
                logger.warning(f"HLS {self.channel_uuid}: All {len(alternate_streams)} alternate streams "
                              f"have been tried: {self._tried_stream_ids}")
                return False

            logger.info(f"HLS {self.channel_uuid}: Found {len(untried_streams)} untried backup streams")

            # Get channel object
            try:
                channel = Channel.objects.get(uuid=self.channel_uuid)
            except Channel.DoesNotExist:
                logger.error(f"HLS {self.channel_uuid}: Channel not found in database")
                return False

            # Try each untried stream
            for next_stream in untried_streams:
                stream_id = next_stream['stream_id']
                profile_id = next_stream['profile_id']

                # Mark as tried
                self._tried_stream_ids.add(stream_id)

                # Get stream info including URL
                logger.info(f"HLS {self.channel_uuid}: Trying backup stream ID {stream_id} "
                           f"with profile ID {profile_id}")
                stream_info = get_stream_info_for_switch(self.channel_uuid, stream_id)

                if 'error' in stream_info or not stream_info.get('url'):
                    logger.error(f"HLS {self.channel_uuid}: Error getting info for stream {stream_id}: "
                                f"{stream_info.get('error', 'No URL')}")
                    continue

                new_url = stream_info['url']
                new_user_agent = stream_info.get('user_agent')

                # Build new stream metadata
                new_metadata = {
                    'stream_id': str(stream_id),
                    'm3u_profile_id': str(profile_id),
                }
                if 'stream_name' in stream_info:
                    new_metadata['stream_name'] = stream_info['stream_name']

                # Get HLS profile info
                hls_profile = channel.get_hls_stream_profile()
                if hls_profile:
                    new_metadata['stream_profile'] = str(hls_profile.id)
                    new_metadata['stream_profile_name'] = hls_profile.name

                # Create a new session directly (don't use change_stream_url since current session is dead)
                new_session = HLSChannelSession(
                    channel_uuid=self.channel_uuid,
                    stream_url=new_url,
                    user_agent=new_user_agent,
                    channel=channel,
                    stream_metadata=new_metadata
                )

                # Pass the accumulated tried streams to the new session
                # This prevents infinite retry loops when all streams fail with the same error
                # (e.g., AAC bitstream issue with fMP4 that affects all streams)
                new_session._tried_stream_ids = self._tried_stream_ids.copy()
                new_session._current_stream_id = stream_id

                # Try to start the new session
                # IMPORTANT: preserve_segments=True for seamless transition
                # This keeps existing segments so the player can continue fetching
                # them while FFmpeg starts writing new ones with append_list flag
                if new_session.start(preserve_segments=True):
                    # Replace the old session in the manager
                    with hls_manager._sessions_lock:
                        hls_manager._sessions[self.channel_uuid] = new_session

                    # Update Redis metadata
                    hls_client_manager.update_channel_metadata(
                        self.channel_uuid,
                        new_url,
                        new_metadata
                    )

                    logger.info(f"HLS {self.channel_uuid}: Automatic switch to stream {stream_id} successful")
                    return True
                else:
                    logger.warning(f"HLS {self.channel_uuid}: Failed to start session for stream {stream_id}")
                    continue

            logger.error(f"HLS {self.channel_uuid}: Tried all {len(untried_streams)} backup streams, none worked")
            return False

        except Exception as e:
            logger.error(f"HLS {self.channel_uuid}: Error during automatic stream switch: {e}", exc_info=True)
            return False

    @property
    def playlist_path(self):
        """Get the path to the HLS playlist file."""
        return os.path.join(self.output_path, "index.m3u8")

    @property
    def playlist_exists(self):
        """Check if the playlist file exists."""
        return os.path.exists(self.playlist_path)

    def get_stat(self, field: str, default=None):
        """Get a stat value from Redis metadata.

        Args:
            field: The stat field name (e.g., 'ffmpeg_speed', 'ffmpeg_fps')
            default: Default value if field not found

        Returns:
            The stat value or default
        """
        try:
            from core.utils import RedisClient
            redis_client = RedisClient.get_client()
            if redis_client:
                metadata_key = f"hls_output:channel:{self.channel_uuid}:metadata"
                value = redis_client.hget(metadata_key, field)
                return value if value is not None else default
        except Exception as e:
            logger.debug(f"HLS {self.channel_uuid} error getting stat {field}: {e}")
        return default


class HLSOutputManager:
    """
    Singleton manager for all HLS output sessions.
    Handles starting/stopping HLS output for channels on demand.

    Uses Redis-based coordination to prevent multiple uwsgi workers from
    starting duplicate FFmpeg processes for the same channel.
    """

    _instance = None
    _lock = threading.Lock()

    # Redis key prefixes for multi-worker coordination
    OWNER_KEY_PREFIX = "hls_output:channel:"
    OWNER_KEY_SUFFIX = ":owner"
    OWNER_TTL = 60  # seconds - refreshed by owner

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

        # Generate unique worker ID for this process
        import socket
        import os
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}"

        # Redis client for coordination - lazy initialization
        self._redis_client = None
        self._redis_init_attempted = False

        logger.info(f"HLS Output Manager initialized (worker_id={self._worker_id})")

    def _get_redis_client(self):
        """Get Redis client with lazy initialization (non-blocking)."""
        if self._redis_client is not None:
            return self._redis_client

        if self._redis_init_attempted:
            # Already tried and failed, don't retry on every call
            return None

        self._redis_init_attempted = True
        try:
            from core.utils import RedisClient
            # Use get_client with no retries to avoid blocking startup
            self._redis_client = RedisClient.get_client()
            if self._redis_client:
                logger.debug("HLS Manager: Redis client initialized")
            return self._redis_client
        except Exception as e:
            logger.warning(f"HLS Manager: Failed to init Redis client: {e}")
            self._redis_client = None
            return None

    def _get_owner_key(self, channel_uuid: str) -> str:
        """Get the Redis key for channel ownership."""
        return f"{self.OWNER_KEY_PREFIX}{channel_uuid}{self.OWNER_KEY_SUFFIX}"

    def _am_i_owner(self, channel_uuid: str) -> bool:
        """
        Check if this worker owns the channel.

        Unlike _try_acquire_ownership, this does NOT try to acquire ownership
        if no one owns it. It simply checks if WE currently own it.

        Returns:
            True if we own the channel, False otherwise
        """
        redis_client = self._get_redis_client()
        if not redis_client:
            # No Redis - check if we have a local session
            with self._sessions_lock:
                return channel_uuid in self._sessions

        try:
            owner_key = self._get_owner_key(channel_uuid)
            current_owner = redis_client.get(owner_key)

            if current_owner:
                current_owner = current_owner.decode('utf-8') if isinstance(current_owner, bytes) else current_owner
                return current_owner == self._worker_id

            # No owner in Redis - check local sessions as fallback
            with self._sessions_lock:
                return channel_uuid in self._sessions

        except Exception as e:
            logger.warning(f"HLS {channel_uuid}: Redis error checking ownership: {e}")
            # On Redis failure, check local sessions
            with self._sessions_lock:
                return channel_uuid in self._sessions

    def _try_acquire_ownership(self, channel_uuid: str) -> bool:
        """
        Try to acquire ownership of a channel using Redis SETNX.

        Returns True if we acquired ownership or already own it.
        Returns False if another worker owns it.
        """
        redis_client = self._get_redis_client()
        if not redis_client:
            # No Redis - allow local operation (single worker mode)
            return True

        try:
            owner_key = self._get_owner_key(channel_uuid)

            # Try atomic set-if-not-exists
            acquired = redis_client.setnx(owner_key, self._worker_id)

            if acquired:
                # We got it - set TTL
                redis_client.expire(owner_key, self.OWNER_TTL)
                logger.info(f"HLS {channel_uuid}: Worker {self._worker_id} acquired ownership")
                return True

            # Check if we already own it
            current_owner = redis_client.get(owner_key)
            if current_owner:
                current_owner = current_owner.decode('utf-8') if isinstance(current_owner, bytes) else current_owner
                if current_owner == self._worker_id:
                    # Refresh TTL
                    redis_client.expire(owner_key, self.OWNER_TTL)
                    return True
                else:
                    logger.debug(f"HLS {channel_uuid}: Owned by {current_owner}, not {self._worker_id}")
                    return False

            # Key expired between setnx and get - try again
            return redis_client.setnx(owner_key, self._worker_id)

        except Exception as e:
            logger.warning(f"HLS {channel_uuid}: Redis error in ownership check: {e}")
            return True  # Allow operation on Redis failure

    def _release_ownership(self, channel_uuid: str):
        """Release ownership of a channel if we own it."""
        redis_client = self._get_redis_client()
        if not redis_client:
            return

        try:
            owner_key = self._get_owner_key(channel_uuid)
            current_owner = redis_client.get(owner_key)

            if current_owner:
                current_owner = current_owner.decode('utf-8') if isinstance(current_owner, bytes) else current_owner
                if current_owner == self._worker_id:
                    redis_client.delete(owner_key)
                    logger.info(f"HLS {channel_uuid}: Released ownership")
        except Exception as e:
            logger.warning(f"HLS {channel_uuid}: Error releasing ownership: {e}")

    def _refresh_ownership(self, channel_uuid: str):
        """Refresh ownership TTL if we own the channel."""
        redis_client = self._get_redis_client()
        if not redis_client:
            return

        try:
            owner_key = self._get_owner_key(channel_uuid)
            current_owner = redis_client.get(owner_key)

            if current_owner:
                current_owner = current_owner.decode('utf-8') if isinstance(current_owner, bytes) else current_owner
                if current_owner == self._worker_id:
                    redis_client.expire(owner_key, self.OWNER_TTL)
        except Exception as e:
            logger.debug(f"HLS {channel_uuid}: Error refreshing ownership: {e}")

    def _is_session_active_in_redis(self, channel_uuid: str) -> bool:
        """Check if any worker has an active session for this channel.

        A session is considered active only if BOTH:
        1. The owner key exists (a worker claims ownership)
        2. The metadata key exists (the session has been properly initialized)

        If only the owner key exists but no metadata, it's a stale session
        that wasn't properly cleaned up.
        """
        redis_client = self._get_redis_client()
        if not redis_client:
            return False

        try:
            owner_key = self._get_owner_key(channel_uuid)
            metadata_key = f"hls_output:channel:{channel_uuid}:metadata"

            # Check both keys exist
            owner_exists = redis_client.exists(owner_key)
            if not owner_exists:
                return False

            # Owner exists - check if metadata also exists
            metadata_exists = redis_client.exists(metadata_key)
            if not metadata_exists:
                # Stale owner key - session wasn't properly cleaned up
                logger.debug(f"HLS {channel_uuid}: Owner key exists but no metadata - stale session")
                return False

            return True
        except Exception as e:
            logger.debug(f"HLS {channel_uuid}: Error checking Redis session: {e}")
            return False

    def _force_cleanup_stale_session(self, channel_uuid: str):
        """
        Force cleanup of a stale HLS session.

        This is called when an owner key exists but no playlist is being produced,
        indicating the previous owner died without proper cleanup.

        This method:
        1. Deletes the stale owner key
        2. Kills any orphaned FFmpeg process using the PID from metadata
        3. Cleans up all Redis keys for the channel
        4. Cleans up the HLS output directory
        """
        import os
        import signal
        import shutil

        redis_client = self._get_redis_client()
        if not redis_client:
            return

        try:
            logger.info(f"HLS {channel_uuid}: Force cleaning up stale session")

            # Get PID from metadata before deleting it
            metadata_key = f"hls_output:channel:{channel_uuid}:metadata"
            pid_str = redis_client.hget(metadata_key, "pid")

            # Kill orphaned FFmpeg process if PID exists
            if pid_str:
                try:
                    pid = int(pid_str)
                    try:
                        os.kill(pid, signal.SIGTERM)
                        logger.info(f"HLS {channel_uuid}: Sent SIGTERM to orphaned FFmpeg process {pid}")
                        # Wait briefly for process to die
                        time.sleep(0.5)
                        try:
                            os.kill(pid, 0)  # Check if still running
                            os.kill(pid, signal.SIGKILL)
                            logger.info(f"HLS {channel_uuid}: Sent SIGKILL to orphaned FFmpeg process {pid}")
                        except OSError:
                            pass  # Process already dead
                    except OSError as e:
                        if e.errno != 3:  # 3 = No such process
                            logger.debug(f"HLS {channel_uuid}: Error killing orphaned process {pid}: {e}")
                except (ValueError, TypeError):
                    pass

            # Delete all Redis keys for this channel
            keys_to_delete = [
                self._get_owner_key(channel_uuid),
                metadata_key,
                f"hls_output:channel:{channel_uuid}:clients",
                f"hls_output:channel:{channel_uuid}:cleanup_lock",
            ]

            # Also delete any client keys
            client_keys = redis_client.keys(f"hls_output:channel:{channel_uuid}:client:*")
            keys_to_delete.extend(client_keys)

            if keys_to_delete:
                redis_client.delete(*keys_to_delete)
                logger.debug(f"HLS {channel_uuid}: Deleted {len(keys_to_delete)} stale Redis keys")

            # Clean up HLS output directory
            channel_path = hls_config.get_channel_path(channel_uuid)
            if os.path.exists(channel_path):
                try:
                    shutil.rmtree(channel_path)
                    logger.info(f"HLS {channel_uuid}: Cleaned up stale HLS directory")
                except Exception as e:
                    logger.warning(f"HLS {channel_uuid}: Error cleaning up stale directory: {e}")

            logger.info(f"HLS {channel_uuid}: Stale session cleanup complete")

        except Exception as e:
            logger.error(f"HLS {channel_uuid}: Error in force cleanup: {e}")

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

        Uses Redis-based coordination to prevent multiple uwsgi workers
        from starting duplicate FFmpeg processes for the same channel.

        Args:
            channel_uuid: The UUID of the channel
            stream_url: The direct stream URL (not TS proxy URL)
            user_agent: Optional user agent from the M3U account
            channel: Optional Channel model instance for profile lookup

        Returns the session if successful, None otherwise.
        """
        with self._sessions_lock:
            # Check for existing local session first
            if channel_uuid in self._sessions:
                session = self._sessions[channel_uuid]
                if session.is_running:
                    # Refresh our ownership TTL
                    self._refresh_ownership(channel_uuid)
                    return session
                # Session exists but not running, clean it up
                self._release_ownership(channel_uuid)
                del self._sessions[channel_uuid]

            # Check if another worker already has an active session
            if self._is_session_active_in_redis(channel_uuid):
                # Another worker owns this channel
                # Return a "virtual" session that just waits for the playlist
                logger.info(f"HLS {channel_uuid}: Session owned by another worker, waiting for playlist")
                # Create a placeholder session that doesn't start FFmpeg
                # We just need to check if the playlist exists
                placeholder = HLSChannelSession(
                    channel_uuid,
                    stream_url,
                    user_agent=user_agent,
                    channel=channel,
                    stream_metadata={}
                )
                # Don't start FFmpeg - just check if playlist is ready
                # The session's output_path will be set, so playlist_exists will work
                if placeholder.playlist_exists:
                    return placeholder
                # Wait briefly for playlist to appear (other worker is creating it)
                for _ in range(30):  # Wait up to 3 seconds
                    time.sleep(0.1)
                    if placeholder.playlist_exists:
                        return placeholder
                # Still no playlist - maybe the owner died, try to take over
                logger.warning(f"HLS {channel_uuid}: Playlist not ready, attempting takeover")
                # Force cleanup of stale ownership - the owner key exists but no playlist
                # This indicates the previous owner died without proper cleanup
                self._force_cleanup_stale_session(channel_uuid)

            # Try to acquire ownership
            if not self._try_acquire_ownership(channel_uuid):
                # Another worker just beat us - wait for their playlist
                logger.info(f"HLS {channel_uuid}: Lost ownership race, waiting for playlist")
                placeholder = HLSChannelSession(
                    channel_uuid,
                    stream_url,
                    user_agent=user_agent,
                    channel=channel,
                    stream_metadata={}
                )
                for _ in range(50):  # Wait up to 5 seconds
                    time.sleep(0.1)
                    if placeholder.playlist_exists:
                        return placeholder
                logger.error(f"HLS {channel_uuid}: Failed to get playlist from owner")
                return None

            # We have ownership - start the session
            logger.info(f"HLS {channel_uuid}: Starting session as owner ({self._worker_id})")

            # Get stream metadata for Active Connections display
            stream_metadata = {}
            if channel:
                stream_metadata = get_stream_metadata(channel)

            # Create new session with direct stream URL and channel reference
            session = HLSChannelSession(
                channel_uuid,
                stream_url,
                user_agent=user_agent,
                channel=channel,
                stream_metadata=stream_metadata
            )
            if session.start():
                self._sessions[channel_uuid] = session
                return session

            # Failed to start - release ownership
            self._release_ownership(channel_uuid)
            return None

    def stop_session(self, channel_uuid: str):
        """Stop an HLS session for a channel."""
        with self._sessions_lock:
            if channel_uuid in self._sessions:
                session = self._sessions[channel_uuid]
                session.stop()
                del self._sessions[channel_uuid]
                # Release ownership when stopping
                self._release_ownership(channel_uuid)
                logger.info(f"HLS session stopped for {channel_uuid}")

    def change_stream_url(self, channel_uuid: str, new_url: str, user_agent: str = None,
                          stream_id: int = None, m3u_profile_id: int = None) -> dict:
        """
        Change the stream URL for an active HLS session.

        This stops the current FFmpeg process and restarts it with the new URL.
        Unlike TS Proxy which can seamlessly switch URLs, HLS requires restarting
        FFmpeg because it writes to disk.

        Args:
            channel_uuid: UUID of the channel
            new_url: New stream URL to switch to
            user_agent: Optional user agent for the new stream
            stream_id: Optional stream ID for metadata tracking
            m3u_profile_id: Optional M3U profile ID for metadata tracking

        Returns:
            dict: Result information including success status
        """
        from apps.channels.models import Channel

        logger.info(f"HLS change_stream_url called for channel {channel_uuid}")

        # Check if we own this channel
        if not self._am_i_owner(channel_uuid):
            # We don't own this channel - check if it exists in Redis
            redis_client = self._get_redis_client()
            if redis_client:
                owner_key = self._get_owner_key(channel_uuid)
                owner = redis_client.get(owner_key)
                if owner:
                    # Another worker owns this channel - publish event for them
                    logger.info(f"HLS channel {channel_uuid} owned by another worker, publishing stream change event")
                    self._publish_stream_change_event(channel_uuid, new_url, user_agent, stream_id, m3u_profile_id)
                    return {
                        'status': 'success',
                        'direct_update': False,
                        'event_published': True,
                        'message': 'Stream change event published'
                    }
                else:
                    # No owner - channel doesn't exist
                    return {
                        'status': 'error',
                        'message': 'Channel not found - no active HLS session'
                    }
            else:
                return {
                    'status': 'error',
                    'message': 'Redis not available'
                }

        # We own this channel - perform the switch
        with self._sessions_lock:
            session = self._sessions.get(channel_uuid)
            if not session:
                return {
                    'status': 'error',
                    'message': 'No active session for this channel'
                }

            # Get the channel object for metadata
            try:
                channel = Channel.objects.get(uuid=channel_uuid)
            except Channel.DoesNotExist:
                return {
                    'status': 'error',
                    'message': 'Channel not found in database'
                }

            # Build new stream metadata
            stream_metadata = {}
            if stream_id:
                stream_metadata['stream_id'] = str(stream_id)
                # Get stream name
                try:
                    from apps.channels.models import Stream
                    stream = Stream.objects.get(pk=stream_id)
                    stream_metadata['stream_name'] = stream.name
                except Exception:
                    pass

            if m3u_profile_id:
                stream_metadata['m3u_profile_id'] = str(m3u_profile_id)
                # Get profile name
                try:
                    from apps.m3u.models import M3UAccountProfile
                    profile = M3UAccountProfile.objects.get(pk=m3u_profile_id)
                    stream_metadata['m3u_profile_name'] = profile.name
                    if profile.m3u_account:
                        stream_metadata['m3u_account_name'] = profile.m3u_account.name
                except Exception:
                    pass

            # Get HLS stream profile info
            hls_profile = channel.get_hls_stream_profile()
            if hls_profile:
                stream_metadata['stream_profile'] = str(hls_profile.id)
                stream_metadata['stream_profile_name'] = hls_profile.name

            old_url = session.stream_url
            old_user_agent = session.user_agent_override
            old_stream_metadata = session.stream_metadata
            logger.info(f"HLS {channel_uuid}: Switching stream from {old_url[:50]}... to {new_url[:50]}...")

            # Stop the current session WITHOUT cleanup and WITHOUT releasing ownership
            # We want to keep the HLS directory intact for the new session
            session.stop(cleanup=False, release_ownership=False)

            # Create new session with new URL
            new_session = HLSChannelSession(
                channel_uuid=channel_uuid,
                stream_url=new_url,
                user_agent=user_agent or old_user_agent,
                channel=channel,
                stream_metadata=stream_metadata
            )

            # Start the new session
            # IMPORTANT: preserve_segments=True for seamless transition
            # This keeps existing segments so the player can continue fetching
            # them while FFmpeg starts writing new ones with append_list flag
            if new_session.start(preserve_segments=True):
                self._sessions[channel_uuid] = new_session

                # Update Redis metadata
                hls_client_manager.update_channel_metadata(
                    channel_uuid,
                    new_url,
                    stream_metadata
                )

                logger.info(f"HLS {channel_uuid}: Stream switch successful")
                return {
                    'status': 'success',
                    'direct_update': True,
                    'old_url': old_url,
                    'new_url': new_url,
                    'stream_id': stream_id
                }
            else:
                # Failed to start new session - try to restart with old URL
                logger.error(f"HLS {channel_uuid}: Failed to start new session, attempting recovery")
                recovery_session = HLSChannelSession(
                    channel_uuid=channel_uuid,
                    stream_url=old_url,
                    user_agent=old_user_agent,
                    channel=channel,
                    stream_metadata=old_stream_metadata
                )
                # Recovery also preserves segments for seamless fallback
                if recovery_session.start(preserve_segments=True):
                    self._sessions[channel_uuid] = recovery_session
                    return {
                        'status': 'error',
                        'message': 'Failed to switch stream, recovered to previous URL',
                        'recovered': True
                    }
                else:
                    # Complete failure - cleanup now
                    if channel_uuid in self._sessions:
                        del self._sessions[channel_uuid]
                    self._release_ownership(channel_uuid)
                    # Clean up the HLS directory since we're completely done
                    try:
                        import shutil
                        output_path = hls_config.get_channel_path(channel_uuid)
                        if os.path.exists(output_path):
                            shutil.rmtree(output_path)
                    except Exception as e:
                        logger.warning(f"HLS {channel_uuid}: Error cleaning up directory after failure: {e}")
                    return {
                        'status': 'error',
                        'message': 'Failed to switch stream and recovery failed',
                        'recovered': False
                    }

    def _publish_stream_change_event(self, channel_uuid: str, new_url: str, user_agent: str = None,
                                      stream_id: int = None, m3u_profile_id: int = None):
        """Publish a stream change event via Redis PubSub for other workers."""
        redis_client = self._get_redis_client()
        if not redis_client:
            logger.error("Cannot publish stream change event - Redis not available")
            return

        import json
        event_data = {
            'type': 'stream_change',
            'channel_uuid': channel_uuid,
            'new_url': new_url,
            'user_agent': user_agent,
            'stream_id': stream_id,
            'm3u_profile_id': m3u_profile_id
        }

        try:
            redis_client.publish('hls_output:events', json.dumps(event_data))
            logger.info(f"Published HLS stream change event for channel {channel_uuid}")
        except Exception as e:
            logger.error(f"Failed to publish stream change event: {e}")

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

