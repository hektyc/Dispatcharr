# core/api_urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_views import (
    UserAgentViewSet,
    StreamProfileViewSet,
    CoreSettingsViewSet,
    SystemNotificationViewSet,
    HLSOutputSettingsViewSet,
    environment,
    version,
    rehash_streams_endpoint,
    TimezoneListView,
    get_system_events
)

router = DefaultRouter()
router.register(r'useragents', UserAgentViewSet, basename='useragent')
router.register(r'streamprofiles', StreamProfileViewSet, basename='streamprofile')
router.register(r'settings', CoreSettingsViewSet, basename='coresettings')
router.register(r'notifications', SystemNotificationViewSet, basename='systemnotification')
router.register(r'hls-output-settings', HLSOutputSettingsViewSet, basename='hls-output-settings')
urlpatterns = [
    path('settings/env/', environment, name='token_refresh'),
    path('version/', version, name='version'),
    path('rehash-streams/', rehash_streams_endpoint, name='rehash_streams'),
    path('timezones/', TimezoneListView.as_view(), name='timezones'),
    path('system-events/', get_system_events, name='system_events'),
    path('', include(router.urls)),
]
