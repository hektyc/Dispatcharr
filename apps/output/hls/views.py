# HLS Output Views
# Serves HLS playlists and segments to clients

import os
import glob
import time
import uuid
import json
import logging
from django.http import (
    HttpResponse,
    HttpResponseNotFound,
    FileResponse,
    StreamingHttpResponse,
    JsonResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view, permission_classes
from apps.channels.models import Channel, Stream
from apps.m3u.models import M3UAccountProfile
from apps.accounts.permissions import IsAdmin
from dispatcharr.utils import network_access_allowed
from .manager import hls_manager, get_direct_stream_url, get_direct_stream_url_for_stream, get_channel_or_stream
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
    Serve the HLS master playlist for a channel or stream.
    This is the entry point for HLS playback.
    URL: /output/hls/{channel_uuid}/playlist.m3u8

    Supports both:
    - Channel UUIDs (e.g., 378f9af6-d623-4384-8b2a-3a69d1746768)
    - Stream hashes (e.g., abc123def456)
    """
    if not network_access_allowed(request, "STREAMS"):
        return HttpResponse("Forbidden", status=403)

    # Get channel or stream by UUID or hash
    channel_or_stream, session_id = get_channel_or_stream(channel_uuid)

    if channel_or_stream is None:
        return HttpResponseNotFound("Channel or stream not found")

    # Get direct stream URL based on object type
    if isinstance(channel_or_stream, Channel):
        # It's a channel - use the channel's stream selection logic
        channel = channel_or_stream
        stream_url, user_agent = get_direct_stream_url(channel)
        if not stream_url:
            return HttpResponse("No stream available for this channel", status=503)

        # Get or start HLS session with direct stream URL
        session = hls_manager.get_or_start_session(
            session_id,
            stream_url,
            user_agent=user_agent,
            channel=channel
        )
    else:
        # It's a stream - preview directly from source
        stream = channel_or_stream
        stream_url, user_agent = get_direct_stream_url_for_stream(stream)
        if not stream_url:
            return HttpResponse("No stream URL available", status=503)

        # Get or start HLS session for stream preview (no channel object)
        session = hls_manager.get_or_start_session(
            session_id,
            stream_url,
            user_agent=user_agent,
            channel=None  # No channel for stream preview
        )

    if not session:
        return HttpResponse("Failed to start HLS output", status=500)

    # Track client connection IMMEDIATELY - before the wait loop
    # This is critical to prevent the session from being killed by the client manager
    # while we're waiting for FFmpeg to create segments
    client_id = _get_client_id(request)
    client_ip = _get_client_ip(request)
    client_user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown')
    hls_client_manager.add_client(session_id, client_id, client_ip, client_user_agent)

    # Wait for playlist, enough segments, AND FFmpeg speed to stabilize (up to 20 seconds)
    # With delete_segments enabled, FFmpeg may run faster than real-time and delete
    # early segments before clients can request them. We need to wait for:
    # 1. Playlist file to exist
    # 2. Enough segments to exist (at least 5 to have buffer room)
    # 3. FFmpeg speed to drop below 2.0x (so segments aren't created faster than consumed)
    MIN_SEGMENTS_BEFORE_READY = 5
    MAX_SPEED_BEFORE_READY = 2.0  # Wait until FFmpeg is running at most 2x real-time
    playlist_ready = False
    speed_stabilized = False

    for _ in range(200):  # 20 seconds (increased from 15 to allow speed stabilization)
        # Update client activity during the wait to keep session alive
        hls_client_manager.update_client_activity(session_id, client_id)

        if session.playlist_exists:
            # Segment format: index0.ts, index1.ts, etc.
            segment_pattern = os.path.join(session.output_path, "index*.ts")
            segments = glob.glob(segment_pattern)

            # Get current FFmpeg speed from session stats
            current_speed = session.get_stat('ffmpeg_speed', 0.0)
            try:
                current_speed = float(current_speed)
            except (ValueError, TypeError):
                current_speed = 0.0

            if len(segments) >= MIN_SEGMENTS_BEFORE_READY:
                # Have enough segments - now check if speed is stable
                if current_speed > 0 and current_speed <= MAX_SPEED_BEFORE_READY:
                    speed_stabilized = True
                    playlist_ready = True
                    logger.debug(
                        f"HLS {session_id}: Ready with {len(segments)} segments, "
                        f"speed={current_speed:.2f}x"
                    )
                    break
                elif current_speed == 0:
                    # Speed not reported yet, but have segments - accept after short delay
                    # This handles cases where speed stats aren't being updated
                    playlist_ready = True
                    break
                # else: speed too high, keep waiting for it to stabilize

        time.sleep(0.1)

    if not playlist_ready:
        return HttpResponse("HLS stream not ready yet, try again", status=503)

    if not speed_stabilized:
        # Log warning but continue - better to serve something than nothing
        logger.warning(
            f"HLS {session_id}: Serving playlist before speed stabilization "
            f"(may cause initial skipping)"
        )

    # Return redirect to the media playlist
    # For simplicity, we serve a master playlist that points to the stream playlist
    base_url = request.build_absolute_uri('/')[:-1]
    hls_url = f"{base_url}/output/hls/{session_id}/index.m3u8"

    master_content = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=5000000
{hls_url}
"""

    response = HttpResponse(master_content, content_type="application/vnd.apple.mpegurl")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Access-Control-Allow-Origin"] = "*"
    return response


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def hls_media_playlist(request, channel_uuid: str):
    """
    Serve the HLS media playlist (index.m3u8) for a channel.
    URL: /output/hls/{channel_uuid}/index.m3u8

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
        "index.m3u8"
    )

    # Update client activity BEFORE checking if playlist exists
    # This is critical to keep the session alive during startup when FFmpeg
    # is still creating the playlist. Without this, the session could be
    # killed after shutdown_delay seconds because no clients are registered.
    client_id = _get_client_id(request)
    hls_client_manager.update_client_activity(channel_uuid, client_id)

    # Check if playlist file exists on disk
    if not os.path.exists(playlist_path):
        # Playlist doesn't exist - might need to start session
        # Return 503 to tell client to retry
        return HttpResponse("Playlist not ready", status=503)

    # Read and modify playlist to use absolute URLs
    try:
        with open(playlist_path, 'r') as f:
            content = f.read()

        # Replace segment filenames with absolute URLs
        # Segment format: index0.ts, index1.ts, etc.
        base_url = request.build_absolute_uri('/')[:-1]
        segment_base = f"{base_url}/output/hls/{channel_uuid}/"

        modified_lines = []
        for line in content.splitlines():
            # Match segment files: index0.ts, index1.ts, etc. (or .m4s for fmp4)
            if line.startswith("index") and (line.endswith(".ts") or line.endswith(".m4s")):
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

    # Update client activity on segment requests BEFORE checking if segment exists
    # This is critical to keep the session alive during startup when FFmpeg
    # is still creating the first segment. Without this, the session would be
    # killed after shutdown_delay seconds because no clients are registered.
    client_id = _get_client_id(request)
    hls_client_manager.update_client_activity(channel_uuid, client_id)

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


def _get_stream_info_for_hls_switch(channel_uuid: str, target_stream_id: int) -> dict:
    """
    Get stream information for an HLS stream switch.

    Args:
        channel_uuid: UUID of the channel
        target_stream_id: Stream ID to switch to

    Returns:
        dict: Stream info including url, user_agent, stream_id, m3u_profile_id
              or dict with 'error' key on failure
    """
    try:
        from apps.proxy.ts_proxy.url_utils import transform_url
        from core.utils import RedisClient

        channel = Channel.objects.get(uuid=channel_uuid)
        redis_client = RedisClient.get_client()

        # Get the target stream
        stream = Stream.objects.get(pk=target_stream_id)

        # Find compatible profile for this stream
        m3u_account = stream.m3u_account
        if not m3u_account:
            return {'error': 'Stream has no M3U account'}

        m3u_profiles = m3u_account.profiles.filter(is_active=True)
        default_profile = next((obj for obj in m3u_profiles if obj.is_default), None)

        if not default_profile:
            return {'error': 'M3U account has no default profile'}

        # Check profiles in order: default first, then others
        profiles = [default_profile] + [obj for obj in m3u_profiles if not obj.is_default]

        selected_profile = None
        for profile in profiles:
            if redis_client:
                profile_connections_key = f"profile_connections:{profile.id}"
                current_connections = int(redis_client.get(profile_connections_key) or 0)

                # Check if this channel is already using this profile
                channel_using_profile = False
                existing_stream_id = redis_client.get(f"channel_stream:{channel.id}")
                if existing_stream_id:
                    existing_stream_id = existing_stream_id.decode('utf-8')
                    existing_profile_id = redis_client.get(f"stream_profile:{existing_stream_id}")
                    if existing_profile_id and int(existing_profile_id.decode('utf-8')) == profile.id:
                        channel_using_profile = True

                effective_connections = current_connections - (1 if channel_using_profile else 0)

                if profile.max_streams == 0 or effective_connections < profile.max_streams:
                    selected_profile = profile
                    break
            else:
                selected_profile = profile
                break

        if not selected_profile:
            return {'error': 'No profiles available with connection capacity'}

        # Get user agent from M3U account
        user_agent = m3u_account.get_user_agent().user_agent

        # Transform URL using M3U profile patterns
        stream_url = transform_url(
            stream.url,
            selected_profile.search_pattern,
            selected_profile.replace_pattern
        )

        return {
            'url': stream_url,
            'user_agent': user_agent,
            'stream_id': target_stream_id,
            'm3u_profile_id': selected_profile.id,
            'm3u_profile_name': selected_profile.name,
            'm3u_account_name': m3u_account.name
        }

    except Channel.DoesNotExist:
        return {'error': 'Channel not found'}
    except Stream.DoesNotExist:
        return {'error': 'Stream not found'}
    except Exception as e:
        logger.error(f"Error getting stream info for HLS switch: {e}", exc_info=True)
        return {'error': str(e)}


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAdmin])
def change_stream(request, channel_uuid: str):
    """
    Change stream URL for an existing HLS channel.

    This endpoint mirrors the TS Proxy's change_stream endpoint but for HLS output.
    It stops the current FFmpeg process and restarts with the new stream URL.

    URL: /output/hls/change_stream/{channel_uuid}
    """
    try:
        data = json.loads(request.body)
        new_url = data.get("url")
        user_agent = data.get("user_agent")
        stream_id = data.get("stream_id")

        # If stream_id is provided, get the URL and user_agent from it
        m3u_profile_id = None
        if stream_id:
            logger.info(f"HLS stream switch: Stream ID {stream_id} provided for channel {channel_uuid}")
            stream_info = _get_stream_info_for_hls_switch(channel_uuid, stream_id)

            if 'error' in stream_info:
                return JsonResponse(
                    {"error": stream_info["error"], "stream_id": stream_id},
                    status=404
                )

            new_url = stream_info['url']
            user_agent = stream_info['user_agent']
            m3u_profile_id = stream_info.get('m3u_profile_id')
        elif not new_url:
            return JsonResponse(
                {"error": "Either url or stream_id must be provided"},
                status=400
            )

        logger.info(f"HLS: Attempting to change stream for channel {channel_uuid}")

        # Use the HLS manager to change the stream
        result = hls_manager.change_stream_url(
            channel_uuid,
            new_url,
            user_agent,
            stream_id,
            m3u_profile_id
        )

        if result.get('status') == 'error':
            return JsonResponse(
                {
                    "error": result.get('message', 'Unknown error'),
                    "diagnostics": result
                },
                status=404
            )

        # Format response
        response_data = {
            "message": "Stream changed successfully",
            "channel": channel_uuid,
            "url": new_url,
            "direct_update": result.get('direct_update', False),
        }

        if stream_id:
            response_data["stream_id"] = stream_id
        if m3u_profile_id:
            response_data["m3u_profile_id"] = m3u_profile_id

        return JsonResponse(response_data)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"HLS: Failed to change stream: {e}", exc_info=True)
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAdmin])
def next_stream(request, channel_uuid: str):
    """
    Switch to the next available stream for an HLS channel.

    This endpoint mirrors the TS Proxy's next_stream endpoint but for HLS output.
    It finds the next available stream in the channel's stream list and switches to it.

    URL: /output/hls/next_stream/{channel_uuid}
    """
    try:
        logger.info(f"HLS next_stream called for channel {channel_uuid}")

        # Get the channel
        try:
            channel = Channel.objects.get(uuid=channel_uuid)
        except Channel.DoesNotExist:
            return JsonResponse({"error": "Channel not found"}, status=404)

        # Get current stream ID from Redis metadata
        from core.utils import RedisClient
        redis_client = RedisClient.get_client()

        current_stream_id = None
        if redis_client:
            metadata_key = f"hls_output:channel:{channel_uuid}:metadata"
            stream_id_bytes = redis_client.hget(metadata_key, "stream_id")
            if stream_id_bytes:
                current_stream_id = int(stream_id_bytes.decode('utf-8'))

        # Get all streams for this channel in order
        streams = channel.streams.all().order_by('channelstream__order')

        if not streams.exists():
            return JsonResponse({"error": "No streams assigned to channel"}, status=404)

        # Find the next stream
        stream_list = list(streams)
        next_stream = None

        if current_stream_id:
            # Find current stream index and get next one
            for i, stream in enumerate(stream_list):
                if stream.id == current_stream_id:
                    # Get next stream (wrap around to first if at end)
                    next_index = (i + 1) % len(stream_list)
                    next_stream = stream_list[next_index]
                    break

        if not next_stream:
            # Current stream not found or not set, use first stream
            next_stream = stream_list[0]

        # Get stream info for the next stream
        stream_info = _get_stream_info_for_hls_switch(channel_uuid, next_stream.id)

        if 'error' in stream_info:
            return JsonResponse(
                {"error": stream_info["error"], "stream_id": next_stream.id},
                status=404
            )

        # Perform the switch
        result = hls_manager.change_stream_url(
            channel_uuid,
            stream_info['url'],
            stream_info['user_agent'],
            next_stream.id,
            stream_info.get('m3u_profile_id')
        )

        if result.get('status') == 'error':
            return JsonResponse(
                {
                    "error": result.get('message', 'Unknown error'),
                    "diagnostics": result
                },
                status=404
            )

        return JsonResponse({
            "message": "Switched to next stream",
            "channel": channel_uuid,
            "stream_id": next_stream.id,
            "stream_name": next_stream.name,
            "previous_stream_id": current_stream_id
        })

    except Exception as e:
        logger.error(f"HLS: Failed to switch to next stream: {e}", exc_info=True)
        return JsonResponse({"error": str(e)}, status=500)
