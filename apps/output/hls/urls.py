# HLS Output URL Configuration
from django.urls import path, re_path
from . import views

app_name = "hls"

urlpatterns = [
    # Stream switching endpoints (must be before the UUID patterns to avoid conflicts)
    path(
        "change_stream/<str:channel_uuid>",
        views.change_stream,
        name="change_stream"
    ),
    path(
        "next_stream/<str:channel_uuid>",
        views.next_stream,
        name="next_stream"
    ),
    # Master playlist: /output/hls/{channel_uuid}/playlist.m3u8
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/playlist\.m3u8$",
        views.hls_master_playlist,
        name="master_playlist"
    ),
    # Media playlist: /output/hls/{channel_uuid}/index.m3u8
    # Also accepts stream.m3u8 for backwards compatibility
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/index\.m3u8$",
        views.hls_media_playlist,
        name="media_playlist"
    ),
    # Backwards compatibility: /output/hls/{channel_uuid}/stream.m3u8
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/stream\.m3u8$",
        views.hls_media_playlist,
        name="media_playlist_legacy"
    ),
    # Segments: /output/hls/{channel_uuid}/{segment_name}
    # Format: index0.ts, index1.ts, etc. (no leading zeros, supports unlimited segments)
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>index[0-9]+\.(ts|m4s))$",
        views.hls_segment,
        name="segment"
    ),
    # Init segment for fMP4/CMAF: /output/hls/{channel_uuid}/init.mp4
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>init\.mp4)$",
        views.hls_segment,
        name="init_segment"
    ),
]

