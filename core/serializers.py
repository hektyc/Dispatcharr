# core/serializers.py
import json
import ipaddress

from rest_framework import serializers
from .models import CoreSettings, UserAgent, StreamProfile, NETWORK_ACCESS


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
            "is_active",
            "user_agent",
            "locked",
        ]


class CoreSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoreSettings
        fields = "__all__"

    def update(self, instance, validated_data):
        if instance.key == NETWORK_ACCESS:
            errors = False
            invalid = {}
            value = json.loads(validated_data.get("value"))
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
    """Serializer for HLS output settings stored as JSON in CoreSettings"""
    output_path = serializers.CharField(max_length=500, required=False, default="/data/hls")
    segment_duration = serializers.IntegerField(min_value=2, max_value=10, required=False, default=6)
    playlist_size = serializers.IntegerField(min_value=3, max_value=10, required=False, default=5)
    retention_seconds = serializers.IntegerField(min_value=0, max_value=3600, required=False, default=0)
    ll_hls_enabled = serializers.BooleanField(required=False, default=False)

    def validate_output_path(self, value):
        if not value or not value.startswith('/'):
            raise serializers.ValidationError("Output path must be an absolute path starting with /")
        return value

    def validate_segment_duration(self, value):
        if value < 2 or value > 10:
            raise serializers.ValidationError("Segment duration must be between 2 and 10 seconds")
        return value

    def validate_playlist_size(self, value):
        if value < 3 or value > 10:
            raise serializers.ValidationError("Playlist size must be between 3 and 10 segments")
        return value

    def validate_retention_seconds(self, value):
        if value < 0 or value > 3600:
            raise serializers.ValidationError("Retention must be between 0 and 3600 seconds (0 = immediate cleanup)")
        return value
