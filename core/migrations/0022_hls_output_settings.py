"""Create HLS Output Settings entry in CoreSettings."""

from django.db import migrations


def create_hls_output_settings(apps, schema_editor):
    CoreSettings = apps.get_model("core", "CoreSettings")
    CoreSettings.objects.get_or_create(
        key="hls_output_settings",
        defaults={
            "name": "HLS Output Settings",
            "value": {
                "storage_backend": "filesystem",
                "segment_duration": 6,
                "playlist_size": 10,
                "shutdown_delay": 30,
                "ll_hls_enabled": False,
                "use_fmp4_segments": False,
                "redis_segment_ttl": 120,
            },
        },
    )


def remove_hls_output_settings(apps, schema_editor):
    CoreSettings = apps.get_model("core", "CoreSettings")
    CoreSettings.objects.filter(key="hls_output_settings").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_systemnotification_notificationdismissal"),
    ]

    operations = [
        migrations.RunPython(
            create_hls_output_settings,
            remove_hls_output_settings,
        ),
    ]
