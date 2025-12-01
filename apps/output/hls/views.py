# HLS Output Views
# Serves HLS playlists and segments to clients

import os
import uuid
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
from .manager import hls_manager, get_direct_stream_url
from .config import hls_config
from .client_manager import hls_client_manager

logger = logging.getLogger(__name__)


def _get_client_id(request):
    """Generate or retrieve a client ID for tracking."""
    # Try to get from session or header
    client_id = request.headers.get('X-Client-ID')
    if not client_id:
        # Generate based on IP and user agent
        client_ip = _get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown')[:50]
        # Create a unique but deterministic ID for this client
        client_id = f"hls_{hash((client_ip, user_agent)) & 0xFFFFFFFF:08x}"
    return client_id


def _get_client_ip(request):
    """Get the client IP address from the request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'Unknown')


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

    # Get direct stream URL (bypasses TS proxy to avoid circular dependency)
    stream_url, user_agent = get_direct_stream_url(channel)
    if not stream_url:
        return HttpResponse("No stream available for this channel", status=503)

    # Get or start HLS session with direct stream URL
    session = hls_manager.get_or_start_session(
        channel_uuid,
        stream_url,
        user_agent=user_agent,
        channel=channel
    )

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

    # Track client connection
    client_id = _get_client_id(request)
    client_ip = _get_client_ip(request)
    client_user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown')
    hls_client_manager.add_client(channel_uuid, client_id, client_ip, client_user_agent)

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

    Note: In a multi-worker uwsgi environment, sessions are not shared between
    workers. Instead of checking session state, we check if the playlist file
    exists on disk. This allows any worker to serve the playlist regardless of
    which worker started the FFmpeg process.
    """
    if not network_access_allowed(request, "STREAMS"):
        return HttpResponse("Forbidden", status=403)

    # Get the playlist path from config (don't rely on session state)
    playlist_path = os.path.join(
        hls_config.get_channel_path(channel_uuid),
        "stream.m3u8"
    )

    # Check if playlist file exists on disk
    if not os.path.exists(playlist_path):
        # Playlist doesn't exist - might need to start session
        # Return 503 to tell client to retry
        return HttpResponse("Playlist not ready", status=503)

    # Update client activity (client was registered on master playlist request)
    client_id = _get_client_id(request)
    hls_client_manager.update_client_activity(channel_uuid, client_id)

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

    # Update client activity on segment requests
    # This is important to keep the session alive while client is actively streaming
    client_id = _get_client_id(request)
    hls_client_manager.update_client_activity(channel_uuid, client_id)

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

