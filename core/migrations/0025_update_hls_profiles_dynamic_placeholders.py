"""Update locked HLS profiles to use dynamic {hlsSegmentDuration} and
{hlsPlaylistSize} placeholders instead of hardcoded values.

This ensures the profiles respect the user's HLS Output Settings for
segment duration and playlist size.
"""

from django.db import migrations


# New parameters with dynamic placeholders
HLS_FFMPEG_PARAMETERS = (
    "-user_agent {userAgent} -i {streamUrl} -c copy -f hls "
    "-hls_time {hlsSegmentDuration} -hls_list_size {hlsPlaylistSize} "
    "-hls_flags append_list+omit_endlist+program_date_time "
    "-hls_segment_filename {hlsOutputPath}/index%d.ts "
    "{hlsOutputPath}/index.m3u8"
)

HLS_PROXY_PARAMETERS = (
    "-i {streamUrl} -c copy -f hls "
    "-hls_time {hlsSegmentDuration} -hls_list_size {hlsPlaylistSize} "
    "-hls_flags delete_segments+append_list+omit_endlist "
    "-hls_segment_filename {hlsOutputPath}/index%d.ts "
    "{hlsOutputPath}/index.m3u8"
)


def update_profiles(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update HLS FFmpeg profile
    StreamProfile.objects.filter(name="HLS FFmpeg", locked=True).update(
        parameters=HLS_FFMPEG_PARAMETERS,
    )

    # Update HLS Proxy profile
    StreamProfile.objects.filter(name="HLS Proxy", locked=True).update(
        parameters=HLS_PROXY_PARAMETERS,
    )


def revert_profiles(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Revert to hardcoded values
    StreamProfile.objects.filter(name="HLS FFmpeg", locked=True).update(
        parameters=(
            "-user_agent {userAgent} -i {streamUrl} -c copy -f hls "
            "-hls_time 6 -hls_list_size 10 "
            "-hls_flags append_list+omit_endlist+program_date_time "
            "-hls_segment_filename {hlsOutputPath}/index%d.ts "
            "{hlsOutputPath}/index.m3u8"
        ),
    )
    StreamProfile.objects.filter(name="HLS Proxy", locked=True).update(
        parameters=(
            "-i {streamUrl} -c copy -f hls "
            "-hls_time 6 -hls_list_size 10 "
            "-hls_flags delete_segments+append_list+omit_endlist "
            "-hls_segment_filename {hlsOutputPath}/index%d.ts "
            "{hlsOutputPath}/index.m3u8"
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_add_hls_proxy_profile"),
    ]

    operations = [
        migrations.RunPython(update_profiles, revert_profiles),
    ]
