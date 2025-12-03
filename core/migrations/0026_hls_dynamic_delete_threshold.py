# Generated migration to use dynamic delete_threshold in HLS FFmpeg profile
# Improves buffering by keeping more segments available for clients

from django.db import migrations


def update_hls_profile_delete_threshold(apps, schema_editor):
    """Update HLS FFmpeg profile to use {deleteThreshold} placeholder.

    Changes:
    - Replaces hardcoded -hls_delete_threshold 3 with {deleteThreshold} placeholder
    - The placeholder is resolved at runtime based on playlist_size setting
    - delete_threshold = max(3, playlist_size) for better buffering
    - This prevents clients from losing their buffer position during playback
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile with dynamic delete threshold
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold {deleteThreshold} -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to hardcoded delete_threshold."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_hls_dynamic_segment_extension'),
    ]

    operations = [
        migrations.RunPython(update_hls_profile_delete_threshold, reverse_code=reverse_migration),
    ]

