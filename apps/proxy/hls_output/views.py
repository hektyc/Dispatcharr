"""HLS Output HTTP Views.

Endpoints for serving HLS playlists and segments,
stream control (change/next stream), and session management.
"""

import os
import re
import time
import glob
import hashlib
import logging

from django.http import HttpResponse, JsonResponse, FileResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .config import hls_config
from .manager import hls_manager, get_channel_or_stream, get_direct_stream_url
from .client_manager import hls_client_manager

logger = logging.getLogger(__name__)

# Readiness polling settings
READINESS_POLL_INTERVAL = 0.5  # seconds
READINESS_TIMEOUT = 30  # seconds
MIN_SEGMENTS_REQUIRED = 2


def _get_client_id(request) -> str:
    """Generate a unique client ID from IP and user agent."""
    ip = _get_client_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "unknown")
    raw = f"{ip}:{ua}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _get_client_ip(request) -> str:
    """Get client IP, handling X-Forwarded-For."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _get_user_agent(request) -> str:
    """Get the client's user-agent string."""
    return request.META.get("HTTP_USER_AGENT", "unknown")


@require_GET
def hls_master_playlist(request, channel_uuid):
    """Serve the HLS master playlist for a channel.

    This is the entry point for HLS clients. It starts the session
    if not already running, waits for readiness, then returns a
    master playlist pointing to the media playlist.
    """
    if not hls_config.is_enabled:
        return HttpResponse(
            "HLS output is not enabled. Set HLS_PATH environment variable.",
            status=503,
        )

    # Get or start the session
    session = hls_manager.get_or_start_session(channel_uuid)
    if not session:
        return HttpResponse("Failed to start HLS session", status=503)

    # Register client with IP and user-agent for stats tracking
    client_id = _get_client_id(request)
    client_ip = _get_client_ip(request)
    client_ua = _get_user_agent(request)
    hls_client_manager.add_client(
        channel_uuid, client_id,
        ip_address=client_ip,
        user_agent=client_ua,
    )

    # Wait for HLS output to be ready
    storage = session.storage
    start_time = time.time()
    ready = False

    while (time.time() - start_time) < READINESS_TIMEOUT:
        # Check if we have enough segments
        if hls_config.storage_backend == "redis":
            seg_count = storage.get_segment_count(channel_uuid)
        else:
            # Filesystem mode - check files directly
            output_path = session.output_path
            if output_path and os.path.exists(output_path):
                ts_files = glob.glob(os.path.join(output_path, "index*.ts"))
                m4s_files = glob.glob(os.path.join(output_path, "index*.m4s"))
                seg_count = len(ts_files) + len(m4s_files)
            else:
                seg_count = 0

        if seg_count >= MIN_SEGMENTS_REQUIRED and session.playlist_exists:
            ready = True
            break

        time.sleep(READINESS_POLL_INTERVAL)

    if not ready:
        return HttpResponse("HLS stream not ready yet, try again", status=503)

    # Build master playlist
    base_url = f"/proxy/hls_output/{channel_uuid}"
    content = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-STREAM-INF:BANDWIDTH=5000000\n"
        f"{base_url}/index.m3u8\n"
    )

    return HttpResponse(
        content,
        content_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


@require_GET
def hls_media_playlist(request, channel_uuid):
    """Serve the HLS media playlist (index.m3u8) for a channel.

    Reads the playlist from the storage backend and rewrites
    segment URLs to route through this view layer.
    """
    session = hls_manager.get_session(channel_uuid)
    if not session:
        # Try to get or start
        session = hls_manager.get_or_start_session(channel_uuid)
        if not session:
            return HttpResponse("No active HLS session", status=404)

    # Update client activity
    client_id = _get_client_id(request)
    hls_client_manager.update_client_activity(channel_uuid, client_id)

    # Get playlist content
    if hls_config.storage_backend == "redis":
        content = session.storage.get_playlist(channel_uuid)
    else:
        playlist_path = session.playlist_path
        try:
            if not os.path.exists(playlist_path):
                return HttpResponse("Playlist not available", status=404)
            with open(playlist_path, "r") as f:
                content = f.read()
        except (OSError, IOError) as e:
            logger.error("Failed to read playlist for %s: %s", channel_uuid, e)
            return HttpResponse("Playlist read error", status=500)

    if not content:
        return HttpResponse("Playlist not available", status=404)

    # Rewrite segment URLs to route through our endpoint
    base_url = f"/proxy/hls_output/{channel_uuid}"
    rewritten_lines = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            rewritten_lines.append(line)
        else:
            # This is a segment filename - prepend our base URL
            segment_name = os.path.basename(line)
            rewritten_lines.append(f"{base_url}/{segment_name}")
    rewritten_content = "\n".join(rewritten_lines) + "\n"

    return HttpResponse(
        rewritten_content,
        content_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


@require_GET
def hls_segment(request, channel_uuid, segment_name):
    """Serve an HLS segment file.

    Reads the segment from the storage backend and returns it
    with the appropriate content type. Tracks bytes_sent per client.
    """
    session = hls_manager.get_session(channel_uuid)
    if not session:
        # On multi-worker setups, this worker may not have the session yet.
        # Try get_or_start_session() which will create a proxy session if
        # the session is running on another worker.
        session = hls_manager.get_or_start_session(channel_uuid)
        if not session:
            return HttpResponse("No active HLS session", status=404)

    # Determine content type
    if segment_name.endswith(".ts"):
        content_type = "video/mp2t"
    elif segment_name.endswith(".m4s"):
        content_type = "video/iso.segment"
    elif segment_name == "init.mp4":
        content_type = "video/mp4"
    else:
        content_type = "application/octet-stream"

    # Serve segment and track bytes
    client_id = _get_client_id(request)
    bytes_served = 0

    if hls_config.storage_backend == "redis":
        # Serve from Redis
        data = session.storage.get_segment(channel_uuid, segment_name)
        if data is None:
            return HttpResponse("Segment not found", status=404)

        bytes_served = len(data)
        response = HttpResponse(
            data,
            content_type=content_type,
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )
    else:
        # Serve from filesystem
        from .storage.filesystem_store import FilesystemSegmentStore

        if isinstance(session.storage, FilesystemSegmentStore):
            segment_path = session.storage.get_segment_path(channel_uuid, segment_name)
        else:
            segment_path = os.path.join(session.output_path, segment_name)

        if not segment_path or not os.path.exists(segment_path):
            return HttpResponse("Segment not found", status=404)

        try:
            bytes_served = os.path.getsize(segment_path)
        except OSError:
            bytes_served = 0

        response = FileResponse(
            open(segment_path, "rb"),
            content_type=content_type,
        )
        response["Cache-Control"] = "no-cache"
        response["Access-Control-Allow-Origin"] = "*"

    # Update client activity with bytes_sent
    hls_client_manager.update_client_activity(
        channel_uuid, client_id, bytes_sent=bytes_served
    )

    # Update session total bytes counter
    if bytes_served > 0 and session.is_running:
        session.add_bytes(bytes_served)

    return response


@csrf_exempt
@require_POST
def change_stream(request, channel_uuid):
    """Change the stream URL for an active HLS session.

    Expects JSON body: {"stream_url": "...", "user_agent": "..."}
    """
    try:
        import json
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    new_url = body.get("stream_url")
    user_agent = body.get("user_agent")

    if not new_url:
        return JsonResponse({"error": "stream_url required"}, status=400)

    success = hls_manager.change_stream_url(channel_uuid, new_url, user_agent)
    if success:
        return JsonResponse({"status": "ok", "message": "Stream changed"})
    else:
        return JsonResponse({"error": "Failed to change stream"}, status=500)


@csrf_exempt
@require_POST
def next_stream(request, channel_uuid):
    """Switch to the next available stream for a channel.

    Finds the next stream in the channel's stream list and switches to it.
    """
    channel, _ = get_channel_or_stream(channel_uuid)
    if not channel:
        return JsonResponse({"error": "Channel not found"}, status=404)

    try:
        from apps.channels.models import ChannelStream

        # Get current and next streams
        channel_streams = list(
            ChannelStream.objects.filter(channel=channel)
            .select_related("stream", "stream__m3u_account")
            .order_by("order")
        )

        if not channel_streams:
            return JsonResponse({"error": "No streams available"}, status=404)

        # Find the current stream
        session = hls_manager.get_session(channel_uuid)
        current_url = session._stream_url if session else None

        # Find the next stream
        next_idx = 0
        if current_url:
            for i, cs in enumerate(channel_streams):
                if cs.stream.url == current_url:
                    next_idx = (i + 1) % len(channel_streams)
                    break

        next_stream = channel_streams[next_idx].stream
        url = next_stream.url
        if not url:
            return JsonResponse({"error": "Next stream has no URL"}, status=404)

        # Get user agent
        user_agent = "VLC/3.0.20 LibVLC/3.0.20"
        profile = channel.get_stream_profile()
        if profile and profile.user_agent:
            user_agent = profile.user_agent.user_agent

        success = hls_manager.change_stream_url(channel_uuid, url, user_agent)
        if success:
            return JsonResponse({
                "status": "ok",
                "message": "Switched to next stream",
                "stream_name": str(next_stream),
            })
        else:
            return JsonResponse({"error": "Failed to switch stream"}, status=500)

    except Exception as e:
        logger.error("Error switching to next stream for %s: %s", channel_uuid, e)
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
def hls_status(request, channel_uuid):
    """Get the status of an HLS output session."""
    session = hls_manager.get_session(channel_uuid)
    if not session:
        return JsonResponse({
            "active": False,
            "channel_uuid": channel_uuid,
        })

    return JsonResponse({
        "active": session.is_running,
        "channel_uuid": channel_uuid,
        "stream_url": session._stream_url,
        "started_at": session._started_at,
        "client_count": hls_client_manager.get_client_count(channel_uuid),
        "video_codec": session.get_stat("video_codec"),
        "audio_codec": session.get_stat("audio_codec"),
        "resolution": session.get_stat("resolution"),
        "fps": session.get_stat("fps"),
        "speed": session.get_stat("speed"),
    })
