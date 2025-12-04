# Generated migration to add input buffering options to HLS FFmpeg profile
# This fixes issues with live stream handling where FFmpeg needs proper input buffering

from django.db import migrations


def update_hls_profile_add_input_buffering(apps, schema_editor):
    """Update HLS FFmpeg profile to add input buffering options.

    Live streams require proper input buffering to handle:
    - Network jitter and delays
    - Discontinuities in the source stream
    - Timestamp issues in the source

    Changes:
    - Adds -fflags +genpts+discardcorrupt before -i (generate PTS, discard corrupt packets)
    - Adds -analyzeduration 5000000 (5 seconds of analysis for stream detection)
    - Adds -probesize 5000000 (5MB probe size for format detection)

    These options match what the HLS Proxy fallback command uses and are essential
    for reliable live stream handling.
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile with input buffering options
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -fflags +genpts+discardcorrupt -analyzeduration 5000000 -probesize 5000000 -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags append_list+program_date_time -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to profile without input buffering options."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags append_list+program_date_time -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_hls_remove_delete_segments'),
    ]

    operations = [
        migrations.RunPython(update_hls_profile_add_input_buffering, reverse_code=reverse_migration),
    ]

