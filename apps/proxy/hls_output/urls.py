"""URL routing for HLS output module."""

from django.urls import path, re_path
from . import views

app_name = "hls_output"

urlpatterns = [
    # Stream control
    path(
        "change_stream/<str:channel_uuid>",
        views.change_stream,
        name="change_stream",
    ),
    path(
        "next_stream/<str:channel_uuid>",
        views.next_stream,
        name="next_stream",
    ),
    path(
        "status/<str:channel_uuid>",
        views.hls_status,
        name="hls_status",
    ),
    # HLS playlists and segments
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/playlist\.m3u8$",
        views.hls_master_playlist,
        name="hls_master_playlist",
    ),
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/index\.m3u8$",
        views.hls_media_playlist,
        name="hls_media_playlist",
    ),
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>index[0-9]+\.(ts|m4s))$",
        views.hls_segment,
        name="hls_segment",
    ),
    re_path(
        r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>init\.mp4)$",
        views.hls_segment,
        name="hls_init_segment",
    ),
]
