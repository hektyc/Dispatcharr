"""Create a locked 'HLS Proxy' stream profile for minimal HLS remux output.

This is the HLS equivalent of the TS 'Proxy' profile — the simplest
possible HLS output using FFmpeg with codec copy (no transcoding).
"""

from django.db import migrations


HLS_PROXY_PROFILE_NAME = "HLS Proxy"
HLS_PROXY_COMMAND = "ffmpeg"
HLS_PROXY_PARAMETERS = (
    "-i {streamUrl} -c copy -f hls "
    "-hls_time 6 -hls_list_size 10 "
    "-hls_flags delete_segments+append_list+omit_endlist "
    "-hls_segment_filename {hlsOutputPath}/index%d.ts "
    "{hlsOutputPath}/index.m3u8"
)


def add_hls_proxy_profile(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")

    if not StreamProfile.objects.filter(name=HLS_PROXY_PROFILE_NAME).exists():
        StreamProfile.objects.create(
            name=HLS_PROXY_PROFILE_NAME,
            command=HLS_PROXY_COMMAND,
            parameters=HLS_PROXY_PARAMETERS,
            is_active=True,
            user_agent=None,  # No custom user agent — uses default
            locked=True,
        )


def remove_hls_proxy_profile(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    StreamProfile.objects.filter(name=HLS_PROXY_PROFILE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_add_hls_ffmpeg_profile"),
    ]

    operations = [
        migrations.RunPython(add_hls_proxy_profile, remove_hls_proxy_profile),
    ]
