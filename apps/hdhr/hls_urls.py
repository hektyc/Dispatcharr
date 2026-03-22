"""URL patterns for HDHR-HLS integration.

Provides HDHR device discovery and lineup endpoints that serve
HLS stream URLs instead of MPEG-TS URLs.
"""

from django.urls import path
from .api_views import (
    HLSDiscoverAPIView,
    HLSLineupAPIView,
    HLSHDHRDeviceXMLAPIView,
)

urlpatterns = [
    path("discover.json", HLSDiscoverAPIView.as_view(), name="hdhr_hls_discover"),
    path("lineup.json", HLSLineupAPIView.as_view(), name="hdhr_hls_lineup"),
    path("device.xml", HLSHDHRDeviceXMLAPIView.as_view(), name="hdhr_hls_device_xml"),
    path("lineup_status.json", HLSDiscoverAPIView.as_view(), name="hdhr_hls_lineup_status"),
]
