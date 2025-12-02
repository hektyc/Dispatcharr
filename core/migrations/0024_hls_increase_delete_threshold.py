# Generated migration to add program_date_time flag to HLS FFmpeg profile
# This improves player seeking and timeline display

from django.db import migrations


def update_hls_profile_flags(apps, schema_editor):
    """Update HLS FFmpeg profile to add program_date_time flag.

    Changes:
    - Adds +program_date_time to hls_flags for better player compatibility

    This improves:
    - Player seeking behavior (knows exact timestamps)
    - Timeline display in player UI
    - Synchronization with EPG/program guide
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile with program_date_time flag
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to profile without program_date_time."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_hls_segment_naming_refactor'),
    ]

    operations = [
        migrations.RunPython(update_hls_profile_flags, reverse_code=reverse_migration),
    ]

