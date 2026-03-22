import React, { useState, useEffect, useCallback } from 'react';
import {
  TextInput,
  NumberInput,
  Switch,
  Button,
  Group,
  Stack,
  Select,
  Text,
  Divider,
  Loader,
  Alert,
} from '@mantine/core';
import API from '../../../api';

const HlsOutputSettingsForm = ({ active }) => {
  const [hlsSettings, setHlsSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const loadHlsSettings = useCallback(async () => {
    try {
      setLoading(true);
      const response = await API.getHLSOutputSettings();
      setHlsSettings(response);
      setError(null);
    } catch (err) {
      setError('Failed to load HLS output settings');
      console.error('Failed to load HLS settings:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) {
      loadHlsSettings();
    }
  }, [active, loadHlsSettings]);

  const saveHlsSettings = async () => {
    if (!hlsSettings) return;
    try {
      setSaving(true);
      await API.updateHLSOutputSettings(hlsSettings);
      setError(null);
    } catch (err) {
      setError('Failed to save HLS output settings');
      console.error('Failed to save HLS settings:', err);
    } finally {
      setSaving(false);
    }
  };

  const updateSetting = (key, value) => {
    setHlsSettings((prev) => ({ ...prev, [key]: value }));
  };

  if (loading) {
    return <Loader size="sm" />;
  }

  if (!hlsSettings) {
    return (
      <Alert color="red" title="Error">
        Failed to load HLS output settings. Make sure the backend is running.
      </Alert>
    );
  }

  return (
    <Stack gap="md">
      {error && (
        <Alert color="red" title="Error" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Text size="sm" c="dimmed">
        HLS output converts channel streams into HLS format using FFmpeg. Configure the output
        path via the <code>HLS_PATH</code> environment variable in docker-compose.yml.
      </Text>

      {/* Output Path (read-only from env) */}
      <TextInput
        label="HLS Output Path"
        description="Configured via HLS_PATH environment variable. Supports RAM disk (/dev/shm), tmpfs, or regular disk."
        value={hlsSettings.output_path || '(not set - HLS output disabled)'}
        readOnly
        styles={{
          input: {
            backgroundColor: 'var(--mantine-color-dark-6)',
            color: hlsSettings.output_path ? 'inherit' : 'var(--mantine-color-yellow-5)',
          },
        }}
      />

      {/* Storage Backend */}
      <Select
        label="Storage Backend"
        description="Where HLS segments are served from. Filesystem is simplest. Redis enables multi-worker serving."
        data={[
          { value: 'filesystem', label: 'Filesystem (Direct)' },
          { value: 'redis', label: 'Redis (with TTL)' },
        ]}
        value={hlsSettings.storage_backend || 'filesystem'}
        onChange={(val) => updateSetting('storage_backend', val)}
      />

      <Divider label="HLS Output Settings" labelPosition="center" />

      <Group grow>
        <NumberInput
          label="Segment Duration"
          description="Seconds per HLS segment"
          min={1}
          max={30}
          value={hlsSettings.segment_duration ?? 6}
          onChange={(val) => updateSetting('segment_duration', val)}
        />
        <NumberInput
          label="Playlist Size"
          description="Number of segments in playlist"
          min={3}
          max={50}
          value={hlsSettings.playlist_size ?? 10}
          onChange={(val) => updateSetting('playlist_size', val)}
        />
      </Group>

      <NumberInput
        label="Shutdown Delay"
        description="Seconds to wait after last client disconnects before stopping FFmpeg"
        min={0}
        max={300}
        value={hlsSettings.shutdown_delay ?? 30}
        onChange={(val) => updateSetting('shutdown_delay', val)}
      />

      {hlsSettings.storage_backend === 'redis' && (
        <NumberInput
          label="Redis Segment TTL"
          description="Time-to-live for segments in Redis (seconds)"
          min={30}
          max={600}
          value={hlsSettings.redis_segment_ttl ?? 120}
          onChange={(val) => updateSetting('redis_segment_ttl', val)}
        />
      )}

      <Group>
        <Switch
          label="fMP4 Segments"
          description="Use fragmented MP4 instead of MPEG-TS segments"
          checked={hlsSettings.use_fmp4_segments ?? false}
          onChange={(e) => updateSetting('use_fmp4_segments', e.currentTarget.checked)}
        />
        <Switch
          label="Low-Latency HLS"
          description="Enable LL-HLS mode (requires fMP4)"
          checked={hlsSettings.ll_hls_enabled ?? false}
          onChange={(e) => updateSetting('ll_hls_enabled', e.currentTarget.checked)}
        />
      </Group>

      <Divider label="HLS.js Player Settings" labelPosition="center" />

      <Text size="xs" c="dimmed">
        These settings control the HLS.js player used in the browser for stream preview.
      </Text>

      <Group grow>
        <Switch
          label="Enable Worker"
          checked={hlsSettings.enable_worker ?? true}
          onChange={(e) => updateSetting('enable_worker', e.currentTarget.checked)}
        />
        <Switch
          label="Low Latency Mode"
          checked={hlsSettings.low_latency_mode ?? false}
          onChange={(e) => updateSetting('low_latency_mode', e.currentTarget.checked)}
        />
      </Group>

      <Group grow>
        <NumberInput
          label="Back Buffer Length"
          description="Seconds of back buffer"
          min={0}
          max={300}
          value={hlsSettings.back_buffer_length ?? 30}
          onChange={(val) => updateSetting('back_buffer_length', val)}
        />
        <NumberInput
          label="Max Buffer Length"
          description="Max seconds of forward buffer"
          min={5}
          max={120}
          value={hlsSettings.max_buffer_length ?? 30}
          onChange={(val) => updateSetting('max_buffer_length', val)}
        />
      </Group>

      <Group grow>
        <NumberInput
          label="Max Max Buffer Length"
          description="Absolute max buffer"
          min={10}
          max={300}
          value={hlsSettings.max_max_buffer_length ?? 60}
          onChange={(val) => updateSetting('max_max_buffer_length', val)}
        />
        <NumberInput
          label="Max Buffer Hole"
          description="Max gap to skip in buffer"
          min={0}
          max={5}
          step={0.1}
          decimalScale={1}
          value={hlsSettings.max_buffer_hole ?? 0.5}
          onChange={(val) => updateSetting('max_buffer_hole', val)}
        />
      </Group>

      <Group grow>
        <NumberInput
          label="Level Loading Max Retry"
          min={0}
          max={20}
          value={hlsSettings.level_loading_max_retry ?? 4}
          onChange={(val) => updateSetting('level_loading_max_retry', val)}
        />
        <NumberInput
          label="Fragment Loading Max Retry"
          min={0}
          max={20}
          value={hlsSettings.frag_loading_max_retry ?? 6}
          onChange={(val) => updateSetting('frag_loading_max_retry', val)}
        />
        <NumberInput
          label="Manifest Loading Max Retry"
          min={0}
          max={20}
          value={hlsSettings.manifest_loading_max_retry ?? 4}
          onChange={(val) => updateSetting('manifest_loading_max_retry', val)}
        />
      </Group>

      <Group justify="flex-end">
        <Button onClick={saveHlsSettings} loading={saving}>
          Save HLS Settings
        </Button>
      </Group>
    </Stack>
  );
};

export default HlsOutputSettingsForm;
