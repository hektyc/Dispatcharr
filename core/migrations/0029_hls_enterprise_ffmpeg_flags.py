# Generated migration to add enterprise-level FFmpeg flags for smooth HLS streaming
# These flags are critical for seamless channel transitions and stable playback

from django.db import migrations


def update_hls_profile_enterprise_flags(apps, schema_editor):
    """Update HLS FFmpeg profile with enterprise-level flags for smooth streaming.

    Key additions for channel change stability:
    
    1. -copyts: Copy timestamps from input to output without modification.
       Critical for maintaining timing continuity during stream switches.
    
    2. -start_at_zero: When used with -copyts, starts output timestamps at zero.
       Prevents timestamp discontinuities that cause player buffering/crashes.
    
    3. -avoid_negative_ts make_zero: Shifts negative timestamps to zero.
       Handles edge cases where source streams have negative PTS values.
    
    4. -max_delay 0: Minimizes muxing delay for faster segment availability.
       Reduces time between FFmpeg receiving data and writing segments.
    
    5. -hls_allow_cache 0: Tells clients not to cache segments.
       Ensures clients always fetch fresh segments after channel changes.
    
    6. -hls_start_number_source epoch: Uses epoch-based segment numbering.
       Guarantees unique segment numbers across FFmpeg restarts, preventing
       clients from requesting stale segments with the same number.
    
    These flags together ensure:
    - Smooth timestamp handling during stream switches
    - Faster segment availability for reduced TTL
    - Unique segment numbering to prevent caching issues
    - Proper handling of source stream timing irregularities
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile with enterprise-level flags
    # Order: input flags -> -i -> output flags -> HLS options -> output file
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


def reverse_migration(apps, schema_editor):
    """Revert to previous profile without enterprise flags."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters=(
            "-user_agent {userAgent} "
            "-fflags +genpts+discardcorrupt "
            "-analyzeduration 5000000 "
            "-probesize 5000000 "
            "-i {streamUrl} "
            "-c copy "
            "-f hls "
            "-hls_time {segmentDuration} "
            "-hls_list_size {playlistSize} "
            "-hls_flags append_list+program_date_time "
            "-hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} "
            "{hlsOutputPath}/index.m3u8"
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_hls_ffmpeg_input_buffering'),
    ]

    operations = [
        migrations.RunPython(
            update_hls_profile_enterprise_flags,
            reverse_code=reverse_migration
        ),
    ]

