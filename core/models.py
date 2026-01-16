# core/models.py

from shlex import split as shlex_split

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.core.exceptions import ValidationError


class UserAgent(models.Model):
    name = models.CharField(
        max_length=512, unique=True, help_text="The User-Agent name."
    )
    user_agent = models.CharField(
        max_length=512,
        unique=True,
        help_text="The complete User-Agent string sent by the client.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        help_text="An optional description of the client or device type.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this user agent is currently allowed/recognized.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


PROXY_PROFILE_NAME = "Proxy"
REDIRECT_PROFILE_NAME = "Redirect"
HLS_PROXY_PROFILE_NAME = "HLS Proxy"
HLS_FFMPEG_PROFILE_NAME = "HLS FFmpeg"

# Profile type choices
PROFILE_TYPE_TS = "ts"
PROFILE_TYPE_HLS = "hls"
PROFILE_TYPE_CHOICES = [
    (PROFILE_TYPE_TS, "MPEG-TS Output"),
    (PROFILE_TYPE_HLS, "HLS Output"),
]


class StreamProfile(models.Model):
    name = models.CharField(max_length=255, help_text="Name of the stream profile")
    command = models.CharField(
        max_length=255,
        help_text="Command to execute (e.g., 'yt.sh', 'streamlink', or 'vlc')",
        blank=True,
    )
    parameters = models.TextField(
        help_text="Command-line parameters. Placeholders: {userAgent}, {streamUrl}, {hlsOutputPath}, {segmentDuration}, {playlistSize}, {segmentExtension}",
        blank=True,
    )
    profile_type = models.CharField(
        max_length=10,
        choices=PROFILE_TYPE_CHOICES,
        default=PROFILE_TYPE_TS,
        help_text="Output type: 'ts' for MPEG-TS (pipe:1), 'hls' for HLS file output",
    )
    locked = models.BooleanField(
        default=False, help_text="Protected - can't be deleted or modified"
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this profile is active"
    )
    user_agent = models.ForeignKey(
        "UserAgent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Optional user agent to use. If not set, you can fall back to a default.",
    )

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.pk:  # Only check existing records
            orig = StreamProfile.objects.get(pk=self.pk)
            if orig.locked:
                allowed_fields = {"user_agent_id"}  # Only allow this field to change
                for field in self._meta.fields:
                    field_name = field.name

                    # Convert user_agent to user_agent_id for comparison
                    orig_value = getattr(orig, field_name)
                    new_value = getattr(self, field_name)

                    # Ensure that ForeignKey fields compare their ID values
                    if isinstance(orig_value, models.Model):
                        orig_value = orig_value.pk
                    if isinstance(new_value, models.Model):
                        new_value = new_value.pk

                    if field_name not in allowed_fields and orig_value != new_value:
                        raise ValidationError(
                            f"Cannot modify {field_name} on a protected profile."
                        )

        super().save(*args, **kwargs)

    @classmethod
    def update(cls, pk, **kwargs):
        instance = cls.objects.get(pk=pk)

        if instance.locked:
            allowed_fields = {"user_agent_id"}  # Only allow updating this field

            for field_name, new_value in kwargs.items():
                if field_name not in allowed_fields:
                    raise ValidationError(
                        f"Cannot modify {field_name} on a protected profile."
                    )

                # Ensure user_agent ForeignKey updates correctly
                if field_name == "user_agent" and isinstance(
                    new_value, cls._meta.get_field("user_agent").related_model
                ):
                    new_value = new_value.pk  # Convert object to ID if needed

                setattr(instance, field_name, new_value)

        instance.save()
        return instance

    def is_proxy(self):
        if self.locked and self.name == PROXY_PROFILE_NAME:
            return True
        return False

    def is_redirect(self):
        if self.locked and self.name == REDIRECT_PROFILE_NAME:
            return True
        return False

    def is_hls_proxy(self):
        """Check if this is the locked HLS Proxy profile."""
        if self.locked and self.name == HLS_PROXY_PROFILE_NAME:
            return True
        return False

    def is_hls_ffmpeg(self):
        """Check if this is the locked HLS FFmpeg profile."""
        if self.locked and self.name == HLS_FFMPEG_PROFILE_NAME:
            return True
        return False

    def is_hls_profile(self):
        """Check if this profile outputs HLS format."""
        return self.profile_type == PROFILE_TYPE_HLS

    def is_ts_profile(self):
        """Check if this profile outputs MPEG-TS format."""
        return self.profile_type == PROFILE_TYPE_TS

    def build_command(self, stream_url, user_agent, hls_output_path=None, hls_config=None):
        """
        Build the command for this stream profile.

        Args:
            stream_url: The stream URL to process
            user_agent: The user agent string
            hls_output_path: Path to HLS output directory (required for HLS profiles)
            hls_config: Dict with HLS settings (segment_duration, playlist_size, use_fmp4_segments, etc.)

        Returns:
            List of command arguments, or empty list for proxy profiles
        """
        if self.is_proxy() or self.is_hls_proxy():
            return []

        replacements = {
            "{streamUrl}": stream_url,
            "{userAgent}": user_agent,
        }

        # Add HLS output path if provided (for HLS profiles)
        if hls_output_path:
            replacements["{hlsOutputPath}"] = hls_output_path

        # Add HLS config placeholders if provided
        use_fmp4 = False
        if hls_config:
            segment_duration = hls_config.get("segment_duration", 6)
            playlist_size = hls_config.get("playlist_size", 10)
            replacements["{segmentDuration}"] = str(segment_duration)
            replacements["{playlistSize}"] = str(playlist_size)
            # Calculate delete_threshold based on playlist_size for better buffering
            # Keep at least as many extra segments as are in the playlist
            delete_threshold = max(3, playlist_size)
            replacements["{deleteThreshold}"] = str(delete_threshold)
            # Determine segment extension based on fMP4 setting
            use_fmp4 = hls_config.get("use_fmp4_segments", False)
            segment_ext = "m4s" if use_fmp4 else "ts"
            replacements["{segmentExtension}"] = segment_ext

        # Split the command and iterate through each part to apply replacements
        cmd = [self.command] + [
            self._replace_in_part(part, replacements)
            for part in shlex_split(self.parameters) # use shlex to handle quoted strings
        ]

        # For HLS profiles using fMP4 segments, add required fMP4 options
        # These must be added dynamically because they're not just placeholders
        # IMPORTANT: These options must be inserted BEFORE the output playlist path,
        # which is the last argument in the command. If we append after the playlist
        # path, FFmpeg will fail with an invalid command syntax error.
        if self.is_hls_profile() and use_fmp4:
            # The playlist path (e.g., /path/index.m3u8) is the last argument
            # Insert fMP4 options before it
            fmp4_options = [
                "-bsf:a", "aac_adtstoasc",  # Convert AAC ADTS to ASC for MP4 container
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_type", "fmp4",
            ]
            # Insert before the last element (playlist path)
            cmd = cmd[:-1] + fmp4_options + [cmd[-1]]

        return cmd

    def _replace_in_part(self, part, replacements):
        # Iterate through the replacements and replace each part of the string
        for key, value in replacements.items():
            part = part.replace(key, value)
        return part


# Setting group keys
STREAM_SETTINGS_KEY = "stream_settings"
DVR_SETTINGS_KEY = "dvr_settings"
BACKUP_SETTINGS_KEY = "backup_settings"
PROXY_SETTINGS_KEY = "proxy_settings"
NETWORK_ACCESS_KEY = "network_access"
SYSTEM_SETTINGS_KEY = "system_settings"
HLS_OUTPUT_SETTINGS_KEY = slugify("HLS Output Settings")


class CoreSettings(models.Model):
    key = models.CharField(
        max_length=255,
        unique=True,
    )
    name = models.CharField(
        max_length=255,
    )
    value = models.JSONField(
        default=dict,
        blank=True,
    )

    def __str__(self):
        return "Core Settings"

    # Helper methods to get/set grouped settings
    @classmethod
    def _get_group(cls, key, defaults=None):
        """Get a settings group, returning defaults if not found."""
        try:
            return cls.objects.get(key=key).value or (defaults or {})
        except cls.DoesNotExist:
            return defaults or {}

    @classmethod
    def _update_group(cls, key, name, updates):
        """Update specific fields in a settings group."""
        obj, created = cls.objects.get_or_create(
            key=key,
            defaults={"name": name, "value": {}}
        )
        current = obj.value if isinstance(obj.value, dict) else {}
        current.update(updates)
        obj.value = current
        obj.save()
        return current

    # Stream Settings
    @classmethod
    def get_stream_settings(cls):
        """Get all stream-related settings."""
        return cls._get_group(STREAM_SETTINGS_KEY, {
            "default_user_agent": None,
            "default_stream_profile": None,
            "m3u_hash_key": "",
            "preferred_region": None,
            "auto_import_mapped_files": None,
        })

    @classmethod
    def get_default_user_agent_id(cls):
        return cls.get_stream_settings().get("default_user_agent")

    @classmethod
    def get_default_stream_profile_id(cls):
        return cls.get_stream_settings().get("default_stream_profile")

    @classmethod
    def get_m3u_hash_key(cls):
        return cls.get_stream_settings().get("m3u_hash_key", "")

    @classmethod
    def get_preferred_region(cls):
        return cls.get_stream_settings().get("preferred_region")

    @classmethod
    def get_auto_import_mapped_files(cls):
        return cls.get_stream_settings().get("auto_import_mapped_files")

    # DVR Settings
    @classmethod
    def get_dvr_settings(cls):
        """Get all DVR-related settings."""
        return cls._get_group(DVR_SETTINGS_KEY, {
            "tv_template": "TV_Shows/{show}/S{season:02d}E{episode:02d}.mkv",
            "movie_template": "Movies/{title} ({year}).mkv",
            "tv_fallback_dir": "TV_Shows",
            "tv_fallback_template": "TV_Shows/{show}/{start}.mkv",
            "movie_fallback_template": "Movies/{start}.mkv",
            "comskip_enabled": False,
            "comskip_custom_path": "",
            "pre_offset_minutes": 0,
            "post_offset_minutes": 0,
            "series_rules": [],
        })

    @classmethod
    def get_dvr_tv_template(cls):
        return cls.get_dvr_settings().get("tv_template", "TV_Shows/{show}/S{season:02d}E{episode:02d}.mkv")

    @classmethod
    def get_dvr_movie_template(cls):
        return cls.get_dvr_settings().get("movie_template", "Movies/{title} ({year}).mkv")

    @classmethod
    def get_dvr_tv_fallback_dir(cls):
        return cls.get_dvr_settings().get("tv_fallback_dir", "TV_Shows")

    @classmethod
    def get_dvr_tv_fallback_template(cls):
        return cls.get_dvr_settings().get("tv_fallback_template", "TV_Shows/{show}/{start}.mkv")

    @classmethod
    def get_dvr_movie_fallback_template(cls):
        return cls.get_dvr_settings().get("movie_fallback_template", "Movies/{start}.mkv")

    @classmethod
    def get_dvr_comskip_enabled(cls):
        return bool(cls.get_dvr_settings().get("comskip_enabled", False))

    @classmethod
    def get_dvr_comskip_custom_path(cls):
        return cls.get_dvr_settings().get("comskip_custom_path", "")

    @classmethod
    def set_dvr_comskip_custom_path(cls, path: str | None):
        value = (path or "").strip()
        cls._update_group(DVR_SETTINGS_KEY, "DVR Settings", {"comskip_custom_path": value})
        return value

    @classmethod
    def get_dvr_pre_offset_minutes(cls):
        return int(cls.get_dvr_settings().get("pre_offset_minutes", 0) or 0)

    @classmethod
    def get_dvr_post_offset_minutes(cls):
        return int(cls.get_dvr_settings().get("post_offset_minutes", 0) or 0)

    @classmethod
    def get_dvr_series_rules(cls):
        return cls.get_dvr_settings().get("series_rules", [])

    @classmethod
    def set_dvr_series_rules(cls, rules):
        cls._update_group(DVR_SETTINGS_KEY, "DVR Settings", {"series_rules": rules})
        return rules

    # Proxy Settings
    @classmethod
    def get_proxy_settings(cls):
        """Get proxy settings."""
        return cls._get_group(PROXY_SETTINGS_KEY, {
            "buffering_timeout": 15,
            "buffering_speed": 1.0,
            "redis_chunk_ttl": 60,
            "channel_shutdown_delay": 0,
            "channel_init_grace_period": 5,
        })

    # System Settings
    @classmethod
    def get_system_settings(cls):
        """Get all system-related settings."""
        return cls._get_group(SYSTEM_SETTINGS_KEY, {
            "time_zone": getattr(settings, "TIME_ZONE", "UTC") or "UTC",
            "max_system_events": 100,
        })

    @classmethod
    def get_system_time_zone(cls):
        return cls.get_system_settings().get("time_zone") or getattr(settings, "TIME_ZONE", "UTC") or "UTC"

    @classmethod
    def set_system_time_zone(cls, tz_name: str | None):
        value = (tz_name or "").strip() or getattr(settings, "TIME_ZONE", "UTC") or "UTC"
        cls._update_group(SYSTEM_SETTINGS_KEY, "System Settings", {"time_zone": value})
        return value

    @classmethod
    def get_hls_output_settings(cls):
        """Retrieve HLS output settings as dict (or return defaults if not found)."""
        import json
        # Default values for all HLS settings
        defaults = {
            # FFmpeg output settings
            "output_path": "/data/hls",
            "segment_duration": 6,
            "playlist_size": 10,
            "retention_seconds": 0,
            "ll_hls_enabled": False,
            "use_fmp4_segments": False,
            "shutdown_delay": 30,
            # HLS.js player settings - optimized for standard HLS (not LL-HLS)
            "player_enable_worker": True,
            "player_low_latency_mode": False,  # Disabled by default for stability
            "player_back_buffer_length": 30,
            "player_max_buffer_length": 30,
            "player_max_max_buffer_length": 60,
            "player_live_sync_duration_count": 4,  # Segments behind live edge
            "player_live_max_latency_duration_count": 15,  # Max latency before seeking
            "player_live_duration_infinity": True,
            "player_manifest_loading_max_retry": 3,
            "player_level_loading_max_retry": 3,
            "player_frag_loading_max_retry": 3,
        }
        try:
            settings_json = cls.objects.get(key=HLS_OUTPUT_SETTINGS_KEY).value
            settings = json.loads(settings_json)
            # Merge with defaults to ensure all keys are present
            merged = {**defaults, **settings}
            return merged
        except (cls.DoesNotExist, json.JSONDecodeError):
            # Return defaults if not found or invalid JSON
            return defaults

    @classmethod
    def set_hls_output_settings(cls, settings_dict):
        """Save HLS output settings."""
        import json
        value = json.dumps(settings_dict)
        obj, created = cls.objects.get_or_create(
            key=HLS_OUTPUT_SETTINGS_KEY,
            defaults={"name": "HLS Output Settings", "value": value},
        )
        if not created:
            obj.value = value
            obj.save(update_fields=["value"])
        return settings_dict


class SystemEvent(models.Model):
    """
    Tracks system events like channel start/stop, buffering, failover, client connections.
    Maintains a rolling history based on max_system_events setting.
    """
    EVENT_TYPES = [
        ('channel_start', 'Channel Started'),
        ('channel_stop', 'Channel Stopped'),
        ('channel_buffering', 'Channel Buffering'),
        ('channel_failover', 'Channel Failover'),
        ('channel_reconnect', 'Channel Reconnected'),
        ('channel_error', 'Channel Error'),
        ('client_connect', 'Client Connected'),
        ('client_disconnect', 'Client Disconnected'),
        ('recording_start', 'Recording Started'),
        ('recording_end', 'Recording Ended'),
        ('stream_switch', 'Stream Switched'),
        ('m3u_refresh', 'M3U Refreshed'),
        ('m3u_download', 'M3U Downloaded'),
        ('epg_refresh', 'EPG Refreshed'),
        ('epg_download', 'EPG Downloaded'),
        ('login_success', 'Login Successful'),
        ('login_failed', 'Login Failed'),
        ('logout', 'User Logged Out'),
        ('m3u_blocked', 'M3U Download Blocked'),
        ('epg_blocked', 'EPG Download Blocked'),
    ]

    event_type = models.CharField(max_length=50, choices=EVENT_TYPES, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    channel_id = models.UUIDField(null=True, blank=True, db_index=True)
    channel_name = models.CharField(max_length=255, null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp']),
            models.Index(fields=['event_type', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.channel_name or 'N/A'} @ {self.timestamp}"
