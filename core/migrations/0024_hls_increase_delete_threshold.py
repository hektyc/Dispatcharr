# Generated migration to increase HLS delete threshold
# Changes: hls_delete_threshold 3 -> hls_delete_threshold 10
# This provides more buffer during startup when FFmpeg runs faster than real-time

from django.db import migrations


def update_hls_delete_threshold(apps, schema_editor):
    """Update HLS FFmpeg profile to use higher delete threshold.
    
    Changes:
    - hls_delete_threshold 3 -> hls_delete_threshold 10
    
    This prevents segment deletion during startup when FFmpeg runs faster than
    real-time (e.g., 3x speed), which causes clients to request segments that
    have already been deleted, resulting in playback skipping.
    
    With playlist_size=10 and delete_threshold=10, segments are only deleted
    when there are 20+ segments total, giving clients plenty of buffer room.
    """
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    # Update the HLS FFmpeg profile with increased delete threshold
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 10 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to previous delete threshold."""
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_hls_segment_naming_refactor'),
    ]

    operations = [
        migrations.RunPython(update_hls_delete_threshold, reverse_code=reverse_migration),
    ]

