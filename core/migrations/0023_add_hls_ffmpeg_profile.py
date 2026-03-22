"""Create a locked 'HLS FFmpeg' stream profile for HLS output."""

from django.db import migrations


HLS_PROFILE_NAME = "HLS FFmpeg"
HLS_COMMAND = "ffmpeg"
HLS_PARAMETERS = (
    "-user_agent {userAgent} -i {streamUrl} -c copy -f hls "
    "-hls_time 6 -hls_list_size 10 "
    "-hls_flags append_list+omit_endlist+program_date_time "
    "-hls_segment_filename {hlsOutputPath}/index%d.ts "
    "{hlsOutputPath}/index.m3u8"
)


def add_hls_ffmpeg_profile(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    UserAgent = apps.get_model("core", "UserAgent")

    if not StreamProfile.objects.filter(name=HLS_PROFILE_NAME).exists():
        # Reuse the first available user agent (same approach as VLC migration)
        user_agent = UserAgent.objects.first()

        StreamProfile.objects.create(
            name=HLS_PROFILE_NAME,
            command=HLS_COMMAND,
            parameters=HLS_PARAMETERS,
            is_active=True,
            user_agent=user_agent,
            locked=True,
        )


def remove_hls_ffmpeg_profile(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    StreamProfile.objects.filter(name=HLS_PROFILE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_hls_output_settings"),
    ]

    operations = [
        migrations.RunPython(add_hls_ffmpeg_profile, remove_hls_ffmpeg_profile),
    ]
