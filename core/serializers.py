# core/serializers.py
import json
import ipaddress

from rest_framework import serializers
from .models import CoreSettings, UserAgent, StreamProfile, NETWORK_ACCESS_KEY


class UserAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAgent
        fields = [
            "id",
            "name",
            "user_agent",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ]


class StreamProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = StreamProfile
        fields = [
            "id",
            "name",
            "command",
            "parameters",
            "profile_type",
            "is_active",
            "user_agent",
            "locked",
        ]


class CoreSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoreSettings
        fields = "__all__"

    def update(self, instance, validated_data):
        if instance.key == NETWORK_ACCESS_KEY:
            errors = False
            invalid = {}
            value = validated_data.get("value")
            for key, val in value.items():
                cidrs = val.split(",")
                for cidr in cidrs:
                    try:
                        ipaddress.ip_network(cidr)
                    except:
                        errors = True
                        if key not in invalid:
                            invalid[key] = []
                        invalid[key].append(cidr)

            if errors:
                # Perform CIDR validation
                raise serializers.ValidationError(
                    {
                        "message": "Invalid CIDRs",
                        "value": invalid,
                    }
                )

        return super().update(instance, validated_data)

class ProxySettingsSerializer(serializers.Serializer):
    """Serializer for proxy settings stored as JSON in CoreSettings"""
    buffering_timeout = serializers.IntegerField(min_value=0, max_value=300)
    buffering_speed = serializers.FloatField(min_value=0.1, max_value=10.0)
    redis_chunk_ttl = serializers.IntegerField(min_value=10, max_value=3600)
    channel_shutdown_delay = serializers.IntegerField(min_value=0, max_value=300)
    channel_init_grace_period = serializers.IntegerField(min_value=0, max_value=60)

    def validate_buffering_timeout(self, value):
        if value < 0 or value > 300:
            raise serializers.ValidationError("Buffering timeout must be between 0 and 300 seconds")
        return value

    def validate_buffering_speed(self, value):
        if value < 0.1 or value > 10.0:
            raise serializers.ValidationError("Buffering speed must be between 0.1 and 10.0")
        return value

    def validate_redis_chunk_ttl(self, value):
        if value < 10 or value > 3600:
            raise serializers.ValidationError("Redis chunk TTL must be between 10 and 3600 seconds")
        return value

    def validate_channel_shutdown_delay(self, value):
        if value < 0 or value > 300:
            raise serializers.ValidationError("Channel shutdown delay must be between 0 and 300 seconds")
        return value

    def validate_channel_init_grace_period(self, value):
        if value < 0 or value > 60:
            raise serializers.ValidationError("Channel init grace period must be between 0 and 60 seconds")
        return value


class HLSOutputSettingsSerializer(serializers.Serializer):
    """Serializer for HLS output settings stored as JSON in CoreSettings.

    Note: output_path is NOT included here - it's configured via the
    HLS_OUTPUT_PATH environment variable in docker-compose.yml or .env file.

    FFmpeg Output Settings:
    - segment_duration: 2-60 seconds (higher values for slow connections)
    - playlist_size: 3-100 segments (higher values for longer buffer)
    - retention_seconds: 0-86400 (up to 24 hours for DVR-like use cases)
    - shutdown_delay: 0-300 seconds (default 15, higher recommended for HDHR clients)

    HLS.js Player Settings (used by web player):
    - player_enable_worker: Enable Web Worker for better performance
    - player_low_latency_mode: Enable low-latency mode (for LL-HLS streams)
    - player_back_buffer_length: Max back buffer length in seconds
    - player_max_buffer_length: Max buffer length in seconds
    - player_max_max_buffer_length: Absolute max buffer length
    - player_live_sync_duration_count: Segments behind live edge
    - player_live_max_latency_duration_count: Max latency before seeking
    - player_live_duration_infinity: Treat live stream as infinite
    - player_manifest_loading_max_retry: Manifest load retry count
    - player_level_loading_max_retry: Level load retry count
    - player_frag_loading_max_retry: Fragment load retry count
    """
    # FFmpeg output settings
    segment_duration = serializers.IntegerField(min_value=2, max_value=60, required=False, default=6)
    playlist_size = serializers.IntegerField(min_value=3, max_value=100, required=False, default=10)
    retention_seconds = serializers.IntegerField(min_value=0, max_value=86400, required=False, default=0)
    ll_hls_enabled = serializers.BooleanField(required=False, default=False)
    use_fmp4_segments = serializers.BooleanField(required=False, default=False)
    shutdown_delay = serializers.IntegerField(min_value=0, max_value=300, required=False, default=15)

    # HLS.js player settings
    player_enable_worker = serializers.BooleanField(required=False, default=True)
    player_low_latency_mode = serializers.BooleanField(required=False, default=False)
    player_back_buffer_length = serializers.IntegerField(min_value=0, max_value=300, required=False, default=30)
    player_max_buffer_length = serializers.IntegerField(min_value=10, max_value=300, required=False, default=30)
    player_max_max_buffer_length = serializers.IntegerField(min_value=30, max_value=600, required=False, default=60)
    player_live_sync_duration_count = serializers.IntegerField(min_value=1, max_value=20, required=False, default=4)
    player_live_max_latency_duration_count = serializers.IntegerField(min_value=3, max_value=30, required=False, default=15)
    player_live_duration_infinity = serializers.BooleanField(required=False, default=True)
    player_manifest_loading_max_retry = serializers.IntegerField(min_value=0, max_value=10, required=False, default=3)
    player_level_loading_max_retry = serializers.IntegerField(min_value=0, max_value=10, required=False, default=3)
    player_frag_loading_max_retry = serializers.IntegerField(min_value=0, max_value=10, required=False, default=3)

    def validate_segment_duration(self, value):
        if value < 2:
            raise serializers.ValidationError("Segment duration must be at least 2 seconds")
        if value > 60:
            raise serializers.ValidationError("Segment duration cannot exceed 60 seconds")
        return value

    def validate_playlist_size(self, value):
        if value < 3:
            raise serializers.ValidationError("Playlist size must be at least 3 segments")
        if value > 100:
            raise serializers.ValidationError("Playlist size cannot exceed 100 segments")
        return value

    def validate_retention_seconds(self, value):
        if value < 0:
            raise serializers.ValidationError("Retention cannot be negative")
        if value > 86400:
            raise serializers.ValidationError("Retention cannot exceed 86400 seconds (24 hours)")
        return value

    def validate_shutdown_delay(self, value):
        if value < 0:
            raise serializers.ValidationError("Shutdown delay cannot be negative")
        if value > 300:
            raise serializers.ValidationError("Shutdown delay cannot exceed 300 seconds")
        return value
