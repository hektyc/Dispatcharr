import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Divider,
  Flex,
  NumberInput,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import API from '../../../api';

const HlsSettingsForm = ({ active }) => {
  const [hlsSettings, setHlsSettings] = useState({
    // FFmpeg output settings
    output_path: '/data/hls',
    segment_duration: 6,
    playlist_size: 10,
    retention_seconds: 0,
    ll_hls_enabled: false,
    use_fmp4_segments: false,
    shutdown_delay: 15,
    // HLS.js player settings
    player_enable_worker: true,
    player_low_latency_mode: false,
    player_back_buffer_length: 30,
    player_max_buffer_length: 30,
    player_max_max_buffer_length: 60,
    player_live_sync_duration_count: 4,
    player_live_max_latency_duration_count: 15,
    player_live_duration_infinity: true,
    player_manifest_loading_max_retry: 3,
    player_level_loading_max_retry: 3,
    player_frag_loading_max_retry: 3,
  });
  const [hlsSettingsSaved, setHlsSettingsSaved] = useState(false);
  const [hlsSettingsLoading, setHlsSettingsLoading] = useState(false);

  // Load HLS settings when form becomes active
  useEffect(() => {
    if (active) {
      const loadHlsSettings = async () => {
        try {
          const response = await API.getHLSSettings();
          if (response) {
            setHlsSettings(response);
          }
        } catch (error) {
          console.error('Failed to load HLS settings', error);
        }
      };
      loadHlsSettings();
      setHlsSettingsSaved(false);
    }
  }, [active]);

  const saveHlsSettings = async () => {
    setHlsSettingsLoading(true);
    setHlsSettingsSaved(false);
    try {
      // Exclude output_path from save - it's read-only from environment variable
      const { output_path, ...settingsToSave } = hlsSettings;
      const response = await API.updateHLSSettings(settingsToSave);
      if (response) {
        setHlsSettings(response);
      }
      setHlsSettingsSaved(true);
      notifications.show({
        title: 'HLS Settings Saved',
        message: 'HLS output settings have been updated successfully.',
        color: 'green',
      });
    } catch (error) {
      notifications.show({
        title: 'Error',
        message: 'Failed to save HLS settings.',
        color: 'red',
      });
    } finally {
      setHlsSettingsLoading(false);
    }
  };

  return (
    <Stack gap="md">
      {hlsSettingsSaved && (
        <Alert variant="light" color="green" title="Saved Successfully" />
      )}
      <Text size="sm" c="dimmed">
        Configure HLS (HTTP Live Streaming) proxy settings. HLS output can be
        enabled per M3U playlist using the format toggle in the M3U popover on
        the Channels page.
      </Text>
      <TextInput
        label="Output Path"
        description="Configured via HLS_OUTPUT_PATH environment variable in docker-compose.yml or .env file. Restart container after changing."
        value={hlsSettings.output_path}
        readOnly
        disabled
        styles={{
          input: {
            backgroundColor: 'var(--mantine-color-dark-6)',
            cursor: 'not-allowed',
          },
        }}
      />
      <NumberInput
        label="Shutdown Delay (seconds)"
        description="Time to wait after last client disconnects before stopping the HLS stream. Default: 15 seconds. Higher values recommended for HDHR clients like Plex."
        value={hlsSettings.shutdown_delay}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, shutdown_delay: value }))
        }
        min={0}
        max={300}
      />
      <NumberInput
        label="Segment Duration (seconds)"
        description="Duration of each HLS segment in seconds. Default: 6. Higher values (10-30) can help with unstable connections."
        value={hlsSettings.segment_duration}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, segment_duration: value }))
        }
        min={2}
        max={60}
      />
      <NumberInput
        label="Playlist Size (segments)"
        description="Number of segments to keep in the playlist. Default: 10 (60s buffer at 6s segments). Higher values provide longer buffer for clients."
        value={hlsSettings.playlist_size}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, playlist_size: value }))
        }
        min={3}
        max={100}
      />
      <NumberInput
        label="Retention (seconds)"
        description="How long to keep segments after removal from playlist. 0 = immediate cleanup. Max: 86400 (24 hours)."
        value={hlsSettings.retention_seconds}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, retention_seconds: value }))
        }
        min={0}
        max={86400}
      />
      <Switch
        label="Use fMP4 Segments"
        description="Use fMP4 (fragmented MP4) instead of MPEG-TS (.ts) segments. fMP4 supports more codecs (H.265, VP9, AV1), is more efficient, and is the modern standard. Automatically enabled when LL-HLS is enabled."
        checked={hlsSettings.use_fmp4_segments || hlsSettings.ll_hls_enabled}
        disabled={hlsSettings.ll_hls_enabled}
        onChange={(e) =>
          setHlsSettings((prev) => ({
            ...prev,
            use_fmp4_segments: e.target.checked,
          }))
        }
      />
      <Switch
        label="Enable Low-Latency HLS (LL-HLS)"
        description="Adds LL-HLS flags for reduced latency. Automatically enables fMP4 segments. Note: Full LL-HLS benefits require HTTP/2 (via reverse proxy)."
        checked={hlsSettings.ll_hls_enabled}
        onChange={(e) =>
          setHlsSettings((prev) => ({
            ...prev,
            ll_hls_enabled: e.target.checked,
            // Auto-enable low latency mode in player when LL-HLS is enabled
            player_low_latency_mode: e.target.checked ? true : prev.player_low_latency_mode,
          }))
        }
      />

      {/* HLS.js Player Settings Section */}
      <Divider my="md" label="Web Player Settings (HLS.js)" labelPosition="center" />

      <Text size="sm" c="dimmed">
        Configure HLS.js player settings for the web video player. These settings
        affect how the browser player handles live streams. Recommended defaults
        work well for most setups.
      </Text>

      <Switch
        label="Enable Web Worker"
        description="Use a Web Worker for parsing. Improves performance by offloading work from the main thread. Recommended: ON."
        checked={hlsSettings.player_enable_worker}
        onChange={(e) =>
          setHlsSettings((prev) => ({
            ...prev,
            player_enable_worker: e.target.checked,
          }))
        }
      />

      <Switch
        label="Low Latency Mode"
        description="Enable low-latency playback mode. Only enable if using LL-HLS output above. When disabled, provides more stable playback for standard HLS. Recommended: OFF for standard HLS, ON for LL-HLS."
        checked={hlsSettings.player_low_latency_mode}
        onChange={(e) =>
          setHlsSettings((prev) => ({
            ...prev,
            player_low_latency_mode: e.target.checked,
          }))
        }
      />

      <Switch
        label="Live Duration Infinity"
        description="Treat live streams as having infinite duration. Prevents the player from showing an end time. Recommended: ON."
        checked={hlsSettings.player_live_duration_infinity}
        onChange={(e) =>
          setHlsSettings((prev) => ({
            ...prev,
            player_live_duration_infinity: e.target.checked,
          }))
        }
      />

      <NumberInput
        label="Back Buffer Length (seconds)"
        description="Maximum length of content that can be seeked back to. Higher values use more memory. Recommended: 30."
        value={hlsSettings.player_back_buffer_length}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_back_buffer_length: value }))
        }
        min={0}
        max={300}
      />

      <NumberInput
        label="Max Buffer Length (seconds)"
        description="Maximum buffer length in seconds. Higher values provide smoother playback but use more memory. Recommended: 30."
        value={hlsSettings.player_max_buffer_length}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_max_buffer_length: value }))
        }
        min={10}
        max={300}
      />

      <NumberInput
        label="Max Max Buffer Length (seconds)"
        description="Absolute maximum buffer length. Player won't buffer beyond this even if bandwidth allows. Recommended: 60."
        value={hlsSettings.player_max_max_buffer_length}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_max_max_buffer_length: value }))
        }
        min={30}
        max={600}
      />

      <Tooltip
        label="Standard HLS: 4-6 (more stable). LL-HLS: 2-3 (lower latency)."
        position="top-start"
        withArrow
      >
        <NumberInput
          label="Live Sync Duration (segments)"
          description="Number of segments to stay behind live edge. Lower = closer to live but less buffer. Standard HLS: 4-6. LL-HLS: 2-3."
          value={hlsSettings.player_live_sync_duration_count}
          onChange={(value) =>
            setHlsSettings((prev) => ({ ...prev, player_live_sync_duration_count: value }))
          }
          min={1}
          max={20}
        />
      </Tooltip>

      <Tooltip
        label="Standard HLS: 12-20 (more tolerance). LL-HLS: 5-8 (stricter)."
        position="top-start"
        withArrow
      >
        <NumberInput
          label="Max Latency Duration (segments)"
          description="Maximum latency in segments before player seeks forward to live edge. Standard HLS: 12-20. LL-HLS: 5-8."
          value={hlsSettings.player_live_max_latency_duration_count}
          onChange={(value) =>
            setHlsSettings((prev) => ({ ...prev, player_live_max_latency_duration_count: value }))
          }
          min={3}
          max={30}
        />
      </Tooltip>

      <Divider my="sm" label="Retry Settings" labelPosition="left" />

      <NumberInput
        label="Manifest Loading Retries"
        description="Number of times to retry loading the playlist manifest. Recommended: 3."
        value={hlsSettings.player_manifest_loading_max_retry}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_manifest_loading_max_retry: value }))
        }
        min={0}
        max={10}
      />

      <NumberInput
        label="Level Loading Retries"
        description="Number of times to retry loading quality levels. Recommended: 3."
        value={hlsSettings.player_level_loading_max_retry}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_level_loading_max_retry: value }))
        }
        min={0}
        max={10}
      />

      <NumberInput
        label="Fragment Loading Retries"
        description="Number of times to retry loading video segments. Higher values help with unstable connections. Recommended: 3."
        value={hlsSettings.player_frag_loading_max_retry}
        onChange={(value) =>
          setHlsSettings((prev) => ({ ...prev, player_frag_loading_max_retry: value }))
        }
        min={0}
        max={10}
      />

      <Flex mih={50} gap="xs" justify="flex-end" align="flex-end">
        <Button
          onClick={saveHlsSettings}
          loading={hlsSettingsLoading}
          variant="default"
        >
          Save
        </Button>
      </Flex>
    </Stack>
  );
};

export default HlsSettingsForm;

