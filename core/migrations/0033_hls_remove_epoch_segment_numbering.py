# Generated migration to remove epoch-based segment numbering from HLS FFmpeg profile
# Epoch numbering (like index1768531941.ts) was causing unusual segment numbers
# Reverting to sequential numbering (index0.ts, index1.ts, etc.) for compatibility

from django.db import migrations


def remove_epoch_numbering(apps, schema_editor):
    """Remove -hls_start_number_source epoch from HLS FFmpeg profile.

    The epoch-based segment numbering was intended to prevent caching issues,
    but it creates segment filenames like index1768531941.ts (Unix timestamp).
    This is unnecessary when:
    - We already use -hls_allow_cache 0 to prevent client caching
    - The append_list flag handles playlist continuity

    Sequential numbering (index0.ts, index1.ts, etc.) is more standard and
    compatible with all HLS players.
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile to remove epoch-based segment numbering
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
            # Removed: -hls_start_number_source epoch
            # Now uses default sequential numbering (0, 1, 2, ...)
            "-hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} "
            "{hlsOutputPath}/index.m3u8"
        )
    )


def reverse_migration(apps, schema_editor):
    """Add back epoch-based segment numbering (revert to previous behavior)."""
    StreamProfile = apps.get_model("core", "StreamProfile")

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


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_change_coresettings_value_to_jsonfield'),
    ]

    operations = [
        migrations.RunPython(
            remove_epoch_numbering,
            reverse_code=reverse_migration
        ),
    ]

