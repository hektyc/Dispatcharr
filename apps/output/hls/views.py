# HLS Output Views
# Serves HLS playlists and segments to clients

import os
import logging
from django.http import (
    HttpResponse,
    HttpResponseNotFound,
    FileResponse,
    StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from apps.channels.models import Channel
from dispatcharr.utils import network_access_allowed
from .manager import hls_manager
from .config import hls_config

logger = logging.getLogger(__name__)


def get_ts_proxy_url(request, channel_uuid: str) -> str:
    """Build the TS proxy URL for a channel."""
    base_url = request.build_absolute_uri('/')[:-1]
    return f"{base_url}/proxy/ts/stream/{channel_uuid}"


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def hls_master_playlist(request, channel_uuid: str):
    """
    Serve the HLS master playlist for a channel.
    This is the entry point for HLS playback.
    URL: /output/hls/{channel_uuid}/playlist.m3u8
    """
    if not network_access_allowed(request, "STREAMS"):
        return HttpResponse("Forbidden", status=403)

    # Verify channel exists
    try:
        channel = Channel.objects.get(uuid=channel_uuid)
    except Channel.DoesNotExist:
        return HttpResponseNotFound("Channel not found")

    # Get or start HLS session
    ts_url = get_ts_proxy_url(request, channel_uuid)
    session = hls_manager.get_or_start_session(channel_uuid, ts_url)

    if not session:
        return HttpResponse("Failed to start HLS output", status=500)

    # Wait briefly for playlist to be created (up to 5 seconds)
    import time
    for _ in range(50):
        if session.playlist_exists:
            break
        time.sleep(0.1)

    if not session.playlist_exists:
        return HttpResponse("HLS playlist not ready yet, try again", status=503)

    # Return redirect to the media playlist
    # For simplicity, we serve a master playlist that points to the stream playlist
    base_url = request.build_absolute_uri('/')[:-1]
    stream_url = f"{base_url}/output/hls/{channel_uuid}/stream.m3u8"

    master_content = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=5000000
{stream_url}
"""

    response = HttpResponse(master_content, content_type="application/vnd.apple.mpegurl")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Access-Control-Allow-Origin"] = "*"
    return response


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def hls_media_playlist(request, channel_uuid: str):
    """
    Serve the HLS media playlist (stream.m3u8) for a channel.
    URL: /output/hls/{channel_uuid}/stream.m3u8
    """
    if not network_access_allowed(request, "STREAMS"):
        return HttpResponse("Forbidden", status=403)

    # Check if session is active
    session = hls_manager.get_session(channel_uuid)
    if not session or not session.is_running:
        return HttpResponseNotFound("HLS session not active")

    playlist_path = session.playlist_path
    if not os.path.exists(playlist_path):
        return HttpResponse("Playlist not ready", status=503)

    # Read and modify playlist to use absolute URLs
    try:
        with open(playlist_path, 'r') as f:
            content = f.read()

        # Replace segment filenames with absolute URLs
        base_url = request.build_absolute_uri('/')[:-1]
        segment_base = f"{base_url}/output/hls/{channel_uuid}/"

        modified_lines = []
        for line in content.splitlines():
            if line.startswith("segment_"):
                modified_lines.append(segment_base + line)
            else:
                modified_lines.append(line)

        modified_content = "\n".join(modified_lines)

        response = HttpResponse(modified_content, content_type="application/vnd.apple.mpegurl")
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Access-Control-Allow-Origin"] = "*"
        return response
    except Exception as e:
        logger.error(f"Error reading playlist for {channel_uuid}: {e}")
        return HttpResponse("Error reading playlist", status=500)


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def hls_segment(request, channel_uuid: str, segment_name: str):
    """
    Serve an HLS segment file.
    URL: /output/hls/{channel_uuid}/{segment_name}
    """
    if not network_access_allowed(request, "STREAMS"):
        return HttpResponse("Forbidden", status=403)

    # Validate segment name (prevent directory traversal)
    if ".." in segment_name or "/" in segment_name or "\\" in segment_name:
        return HttpResponse("Invalid segment name", status=400)

    segment_path = os.path.join(hls_config.get_channel_path(channel_uuid), segment_name)

    if not os.path.exists(segment_path):
        return HttpResponseNotFound("Segment not found")

    # Determine content type
    if segment_name.endswith(".ts"):
        content_type = "video/mp2t"
    elif segment_name.endswith(".m4s"):
        content_type = "video/iso.segment"
    elif segment_name.endswith(".mp4"):
        content_type = "video/mp4"
    else:
        content_type = "application/octet-stream"

    response = FileResponse(open(segment_path, 'rb'), content_type=content_type)
    response["Cache-Control"] = "max-age=3600"
    response["Access-Control-Allow-Origin"] = "*"
    return response

