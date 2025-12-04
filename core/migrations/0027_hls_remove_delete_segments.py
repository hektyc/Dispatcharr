# Generated migration to remove delete_segments flag from HLS FFmpeg profile
# This fixes the issue where FFmpeg deletes segments faster than clients can request them

from django.db import migrations


def update_hls_profile_remove_delete_segments(apps, schema_editor):
    """Update HLS FFmpeg profile to remove delete_segments flag.

    The delete_segments flag causes FFmpeg to delete old segments from disk.
    When FFmpeg runs faster than real-time (which it does initially), segments
    are deleted before clients can request them, causing playback failures.

    Changes:
    - Removes 'delete_segments+' from -hls_flags
    - Removes -hls_delete_threshold (no longer needed without delete_segments)
    - Keeps append_list and program_date_time flags for proper playlist behavior

    Segments will now accumulate on disk and be cleaned up when the session ends
    (handled by HLSChannelSession._cleanup_all()).
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile without delete_segments
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags append_list+program_date_time -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to using delete_segments flag."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold {deleteThreshold} -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_hls_dynamic_delete_threshold'),
    ]

    operations = [
        migrations.RunPython(update_hls_profile_remove_delete_segments, reverse_code=reverse_migration),
    ]

