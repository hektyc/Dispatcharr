import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Flex,
  NumberInput,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import API from '../../../api';

const HlsSettingsForm = ({ active }) => {
  const [hlsSettings, setHlsSettings] = useState({
    output_path: '/data/hls',
    segment_duration: 6,
    playlist_size: 10,
    retention_seconds: 0,
    ll_hls_enabled: false,
    use_fmp4_segments: false,
    shutdown_delay: 15,
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
          }))
        }
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

