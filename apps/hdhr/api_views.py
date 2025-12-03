from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.accounts.permissions import Authenticated, permission_classes_by_action
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
import logging
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.shortcuts import get_object_or_404
from django.db import models
from apps.channels.models import Channel, ChannelProfile, Stream
from .models import HDHRDevice
from .serializers import HDHRDeviceSerializer
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views import View
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

# Configure logger
logger = logging.getLogger(__name__)


@login_required
def hdhr_dashboard_view(request):
    """Render the HDHR management page."""
    hdhr_devices = HDHRDevice.objects.all()
    return render(request, "hdhr/hdhr.html", {"hdhr_devices": hdhr_devices})


# 🔹 1) HDHomeRun Device API
class HDHRDeviceViewSet(viewsets.ModelViewSet):
    """Handles CRUD operations for HDHomeRun devices"""

    queryset = HDHRDevice.objects.all()
    serializer_class = HDHRDeviceSerializer

    def get_permissions(self):
        try:
            return [perm() for perm in permission_classes_by_action[self.action]]
        except KeyError:
            return [Authenticated()]


# 🔹 2) Discover API
class DiscoverAPIView(APIView):
    """Returns device discovery information"""

    @swagger_auto_schema(
        operation_description="Retrieve HDHomeRun device discovery information",
        responses={200: openapi.Response("HDHR Discovery JSON")},
    )
    def get(self, request, profile=None):
        uri_parts = ["hdhr"]
        if profile is not None:
            uri_parts.append(profile)

        base_url = request.build_absolute_uri(f'/{"/".join(uri_parts)}/').rstrip("/")
        device = HDHRDevice.objects.first()

        # Calculate tuner count using centralized function
        from apps.m3u.utils import calculate_tuner_count
        tuner_count = calculate_tuner_count(minimum=1, unlimited_default=10)

        # Create a unique DeviceID for the HDHomeRun device based on profile ID or a default value
        device_ID = "12345678"  # Default DeviceID
        friendly_name = "Dispatcharr HDHomeRun"
        if profile is not None:
            device_ID = f"dispatcharr-hdhr-{profile}"
            friendly_name = f"Dispatcharr HDHomeRun - {profile}"
        if not device:
            data = {
                "FriendlyName": friendly_name,
                "ModelNumber": "HDTC-2US",
                "FirmwareName": "hdhomerun3_atsc",
                "FirmwareVersion": "20200101",
                "DeviceID": device_ID,
                "DeviceAuth": "test_auth_token",
                "BaseURL": base_url,
                "LineupURL": f"{base_url}/lineup.json",
                "TunerCount": tuner_count,
            }
        else:
            data = {
                "FriendlyName": device.friendly_name,
                "ModelNumber": "HDTC-2US",
                "FirmwareName": "hdhomerun3_atsc",
                "FirmwareVersion": "20200101",
                "DeviceID": device.device_id,
                "DeviceAuth": "test_auth_token",
                "BaseURL": base_url,
                "LineupURL": f"{base_url}/lineup.json",
                "TunerCount": tuner_count,
            }
        return JsonResponse(data)


# 🔹 3) Lineup API
class LineupAPIView(APIView):
    """Returns available channel lineup.

    Supports the following query parameters:
    - format: 'ts' (default, MPEG-TS) or 'hls' (HLS output)
              This mirrors the M3U playlist generation behavior.
    """

    @swagger_auto_schema(
        operation_description="Retrieve the available channel lineup",
        manual_parameters=[
            openapi.Parameter(
                'format',
                openapi.IN_QUERY,
                description="Output format: 'ts' (MPEG-TS, default) or 'hls' (HLS)",
                type=openapi.TYPE_STRING,
                enum=['ts', 'hls'],
                default='ts'
            ),
        ],
        responses={200: openapi.Response("Channel Lineup JSON")},
    )
    def get(self, request, profile=None):
        if profile is not None:
            channel_profile = ChannelProfile.objects.get(name=profile)
            channels = Channel.objects.filter(
                channelprofilemembership__channel_profile=channel_profile,
                channelprofilemembership__enabled=True,
            ).order_by("channel_number")
        else:
            channels = Channel.objects.all().order_by("channel_number")

        # Get the output format: 'ts' (default MPEG-TS) or 'hls' (HLS output)
        # This mirrors the M3U playlist generation logic in apps/output/views.py
        output_format = request.GET.get('format', 'ts').lower()
        use_hls_output = output_format == 'hls'

        lineup = []
        for ch in channels:
            # Format channel number as integer if it has no decimal component
            if ch.channel_number is not None:
                if ch.channel_number == int(ch.channel_number):
                    formatted_channel_number = str(int(ch.channel_number))
                else:
                    formatted_channel_number = str(ch.channel_number)
            else:
                formatted_channel_number = ""

            # Determine the stream URL based on format
            # This mirrors the M3U playlist generation logic
            if use_hls_output:
                stream_url = request.build_absolute_uri(f"/output/hls/{ch.uuid}/playlist.m3u8")
            else:
                stream_url = request.build_absolute_uri(f"/proxy/ts/stream/{ch.uuid}")

            lineup.append(
                {
                    "GuideNumber": formatted_channel_number,
                    "GuideName": ch.name,
                    "URL": stream_url,
                    "Guide_ID": formatted_channel_number,
                    "Station": formatted_channel_number,
                }
            )
        return JsonResponse(lineup, safe=False)


# 🔹 4) Lineup Status API
class LineupStatusAPIView(APIView):
    """Returns the current status of the HDHR lineup"""

    @swagger_auto_schema(
        operation_description="Retrieve the HDHomeRun lineup status",
        responses={200: openapi.Response("Lineup Status JSON")},
    )
    def get(self, request, profile=None):
        data = {
            "ScanInProgress": 0,
            "ScanPossible": 0,
            "Source": "Cable",
            "SourceList": ["Cable"],
        }
        return JsonResponse(data)


# 🔹 5) Device XML API
class HDHRDeviceXMLAPIView(APIView):
    """Returns HDHomeRun device configuration in XML"""

    @swagger_auto_schema(
        operation_description="Retrieve the HDHomeRun device XML configuration",
        responses={200: openapi.Response("HDHR Device XML")},
    )
    def get(self, request):
        base_url = request.build_absolute_uri("/hdhr/").rstrip("/")

        xml_response = f"""<?xml version="1.0" encoding="utf-8"?>
        <root>
            <DeviceID>12345678</DeviceID>
            <FriendlyName>Dispatcharr HDHomeRun</FriendlyName>
            <ModelNumber>HDTC-2US</ModelNumber>
            <FirmwareName>hdhomerun3_atsc</FirmwareName>
            <FirmwareVersion>20200101</FirmwareVersion>
            <DeviceAuth>test_auth_token</DeviceAuth>
            <BaseURL>{base_url}</BaseURL>
            <LineupURL>{base_url}/lineup.json</LineupURL>
        </root>"""

        return HttpResponse(xml_response, content_type="application/xml")
