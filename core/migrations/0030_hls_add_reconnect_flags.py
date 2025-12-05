# Generated migration to add reconnect flags back to HLS FFmpeg profile
# These flags help FFmpeg handle brief upstream connection issues without crashing

from django.db import migrations


def add_reconnect_flags(apps, schema_editor):
    """Add reconnect flags to HLS FFmpeg profile.

    The upstream IPTV source often closes connections briefly during channel changes
    or due to network hiccups. Adding reconnect flags allows FFmpeg to handle these
    gracefully instead of immediately crashing:

    1. -reconnect 1: Enable reconnection on connection failure
    2. -reconnect_streamed 1: Also reconnect on streamed (non-seekable) content
    3. -reconnect_delay_max 2: Maximum 2 seconds delay between reconnect attempts
       (short enough that automatic stream switch can take over if reconnection fails)

    These flags work WITH Dispatcharr's automatic stream switch:
    - Brief hiccups (< 2 seconds): FFmpeg handles via reconnect
    - Permanent failures (> 2 seconds): Automatic stream switch takes over
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile to add reconnect options BEFORE -i
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters=(
            # Input options (before -i)
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 2 "
            "-user_agent {userAgent} "
            "-fflags +genpts+discardcorrupt+igndts "
            "-analyzeduration 5000000 "
            "-probesize 5000000 "
            "-i {streamUrl} "
            # Output options
            "-c copy "
            "-copyts "
            "-start_at_zero "
            "-avoid_negative_ts make_zero "
            "-max_delay 0 "
            # HLS muxer options
            "-f hls "
            "-hls_time {segmentDuration} "
            "-hls_list_size {playlistSize} "
            "-hls_flags append_list+program_date_time "
            "-hls_allow_cache 0 "
            "-hls_start_number_source epoch "
            "-hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} "
            "{hlsOutputPath}/index.m3u8"
        )
    )


def reverse_migration(apps, schema_editor):
    """Remove reconnect flags from HLS FFmpeg profile."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters=(
            # Input options (before -i)
            "-user_agent {userAgent} "
            "-fflags +genpts+discardcorrupt+igndts "
            "-analyzeduration 5000000 "
            "-probesize 5000000 "
            "-i {streamUrl} "
            # Output options
            "-c copy "
            "-copyts "
            "-start_at_zero "
            "-avoid_negative_ts make_zero "
            "-max_delay 0 "
            # HLS muxer options
            "-f hls "
            "-hls_time {segmentDuration} "
            "-hls_list_size {playlistSize} "
            "-hls_flags append_list+program_date_time "
            "-hls_allow_cache 0 "
            "-hls_start_number_source epoch "
            "-hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} "
            "{hlsOutputPath}/index.m3u8"
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_hls_enterprise_ffmpeg_flags'),
    ]

    operations = [
        migrations.RunPython(
            add_reconnect_flags,
            reverse_code=reverse_migration
        ),
    ]

