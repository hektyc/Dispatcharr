# Generated migration to update HLS segment naming format
# Changes: segment_%05d.ts -> index%d.ts, stream.m3u8 -> index.m3u8
# This allows unlimited segment numbers (no 5-digit limit) for long-running streams

from django.db import migrations


def update_hls_segment_naming(apps, schema_editor):
    """Update HLS FFmpeg profile to use new segment naming format.
    
    Changes:
    - segment_%05d.ts -> index%d.ts (no leading zeros, unlimited segments)
    - stream.m3u8 -> index.m3u8 (consistent naming)
    
    This prevents streams from failing when segment count exceeds 99999 during
    very long streaming sessions (e.g., 55+ hours with 2-second segments).
    """
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    # Update the HLS FFmpeg profile with new segment naming
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to old segment naming format."""
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list -hls_segment_filename {hlsOutputPath}/segment_%05d.ts {hlsOutputPath}/stream.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_hls_ffmpeg_use_config_placeholders'),
    ]

    operations = [
        migrations.RunPython(update_hls_segment_naming, reverse_code=reverse_migration),
    ]

