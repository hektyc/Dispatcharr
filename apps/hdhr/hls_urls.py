"""
HLS HDHR URL patterns.

These URL patterns are used for the /hdhr-hls/ endpoint, which is a dedicated
HDHR endpoint that always returns HLS URLs instead of TS proxy URLs.

This allows HDHR clients (like Plex, Channels DVR) to receive HLS streams
without requiring query parameters or special configuration.
"""
from django.urls import path
from .api_views import (
    HLSDiscoverAPIView, HLSLineupAPIView, HLSHDHRDeviceXMLAPIView,
    LineupStatusAPIView
)

app_name = 'hdhr-hls'

urlpatterns = [
    path('<str:profile>/discover.json', HLSDiscoverAPIView.as_view(), name='discover_with_profile'),
    path('discover.json', HLSDiscoverAPIView.as_view(), name='discover_no_profile'),
    path('<str:profile>/lineup.json', HLSLineupAPIView.as_view(), name='lineup_with_profile'),
    path('lineup.json', HLSLineupAPIView.as_view(), name='lineup_no_profile'),
    path('<str:profile>/lineup_status.json', LineupStatusAPIView.as_view(), name='lineup_status_with_profile'),
    path('lineup_status.json', LineupStatusAPIView.as_view(), name='lineup_status_no_profile'),
    path('device.xml', HLSHDHRDeviceXMLAPIView.as_view(), name='device_xml'),
]

