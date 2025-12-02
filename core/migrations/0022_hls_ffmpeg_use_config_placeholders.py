# Generated migration to update HLS FFmpeg profile to use config placeholders
# This allows HLS Proxy settings (segment_duration, playlist_size) from the UI to work

from django.db import migrations


def update_hls_ffmpeg_profile(apps, schema_editor):
    """Update HLS FFmpeg profile to use {segmentDuration} and {playlistSize} placeholders.
    
    This allows the profile to respect the HLS Proxy settings configured in the UI
    (Settings -> HLS Proxy -> Segment Duration and Playlist Size).
    """
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    # Update the HLS FFmpeg profile to use placeholders instead of hardcoded values
    # Old: -hls_time 4 -hls_list_size 5
    # New: -hls_time {segmentDuration} -hls_list_size {playlistSize}
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list -hls_segment_filename {hlsOutputPath}/segment_%05d.ts {hlsOutputPath}/stream.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to hardcoded values in HLS FFmpeg profile."""
    StreamProfile = apps.get_model("core", "StreamProfile")
    
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time 4 -hls_list_size 5 -hls_flags delete_segments+append_list -hls_segment_filename {hlsOutputPath}/segment_%05d.ts {hlsOutputPath}/stream.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_remove_hls_reconnect_flags'),
    ]

    operations = [
        migrations.RunPython(update_hls_ffmpeg_profile, reverse_code=reverse_migration),
    ]

