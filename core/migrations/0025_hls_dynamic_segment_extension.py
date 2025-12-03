# Generated migration to use dynamic segment extension in HLS FFmpeg profile
# Supports both fMP4 (.m4s) and MPEG-TS (.ts) segments based on settings

from django.db import migrations


def update_hls_profile_segment_extension(apps, schema_editor):
    """Update HLS FFmpeg profile to use {segmentExtension} placeholder.

    Changes:
    - Replaces hardcoded .ts with {segmentExtension} placeholder
    - The placeholder is resolved at runtime based on use_fmp4_segments setting
    - .ts for MPEG-TS segments (default)
    - .m4s for fMP4 segments (when enabled)

    Note: The fMP4-specific options (-bsf:a aac_adtstoasc, -hls_fmp4_init_filename,
    -hls_segment_type fmp4) are added dynamically by build_command() when fMP4 is enabled.
    """
    StreamProfile = apps.get_model("core", "StreamProfile")

    # Update the HLS FFmpeg profile with dynamic segment extension
    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.{segmentExtension} {hlsOutputPath}/index.m3u8"
    )


def reverse_migration(apps, schema_editor):
    """Revert to hardcoded .ts extension."""
    StreamProfile = apps.get_model("core", "StreamProfile")

    StreamProfile.objects.filter(
        name="HLS FFmpeg",
        locked=True
    ).update(
        parameters="-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time {segmentDuration} -hls_list_size {playlistSize} -hls_flags delete_segments+append_list+program_date_time -hls_delete_threshold 3 -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_hls_increase_delete_threshold'),
    ]

    operations = [
        migrations.RunPython(update_hls_profile_segment_extension, reverse_code=reverse_migration),
    ]

