# Existing HLS Implementation: File-by-File Transformation Guide

This document details exactly what happens to every file in your current HLS implementation — what is kept, what is changed, what is removed, and why.

---

## BACKEND FILES

---

### `apps/output/hls/config.py` (274 lines)

**Current:** HLSConfig class with cached settings from CoreSettings, env-based output path, directory validation.

**Transformation → `apps/proxy/hls_output/config.py`**

| Section                                                             | Action     | Details                                                                                                                                                         |
| ------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `HLSConfig.__init__()` with cache                                   | **KEEP**   | Settings cache with TTL is a good pattern. Move to new location.                                                                                                |
| `_load_settings()` from `HLS_OUTPUT_SETTINGS_KEY`                   | **MODIFY** | Change to use `CoreSettings._get_group("hls_output_settings", defaults)` instead of `json.loads(settings_obj.value)`. This follows upstream's settings pattern. |
| `output_path` property (reads `HLS_OUTPUT_PATH` env)                | **MODIFY** | Rename env var from `HLS_OUTPUT_PATH` to `HLS_PATH`. Remove hardcoded `/data/hls` default. Return empty string if not set (HLS disabled).                       |
| `_ensure_directory()` with write test                               | **KEEP**   | Good defensive code. Port directly.                                                                                                                             |
| `segment_duration`, `playlist_size`, `retention_seconds` properties | **KEEP**   | Same settings, same access pattern.                                                                                                                             |
| `ll_hls_enabled`, `use_fmp4_segments` properties                    | **KEEP**   | Same.                                                                                                                                                           |
| `shutdown_delay` property                                           | **KEEP**   | Same.                                                                                                                                                           |
| `get_channel_path()`                                                | **KEEP**   | Same logic — `{hls_path}/{uuid}/`.                                                                                                                              |
| `initialize()`                                                      | **KEEP**   | Called during app startup.                                                                                                                                      |
| `to_dict()`                                                         | **KEEP**   | Useful for debugging.                                                                                                                                           |
| `hls_config` global instance                                        | **KEEP**   | Singleton pattern, same approach.                                                                                                                               |

**Net change:** ~15 lines modified out of 274. Mostly renaming env var and settings access pattern.

---

### `apps/output/hls/manager.py` (1,811 lines)

**Current:** `get_channel_or_stream()`, `get_direct_stream_url()`, `get_stream_metadata()`, `HLSChannelSession`, `HLSOutputManager`, `hls_manager` singleton.

**Transformation → `apps/proxy/hls_output/manager.py` + `apps/proxy/hls_output/session.py`**

#### Helper Functions (lines 1-197)

| Function                             | Action     | Details                                                                                                                                                                  |
| ------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `get_channel_or_stream()`            | **KEEP**   | Exact same logic — lookup by UUID or stream_hash.                                                                                                                        |
| `get_direct_stream_url_for_stream()` | **KEEP**   | Exact same — gets URL from Stream + M3U profile.                                                                                                                         |
| `get_direct_stream_url()`            | **KEEP**   | Exact same — gets URL from Channel's stream selection.                                                                                                                   |
| `get_stream_metadata()`              | **MODIFY** | Remove `channel.get_hls_stream_profile()` call — replace with generic stream profile info. This is the line that creates a dependency on the Channel model's HLS method. |

#### `HLSChannelSession` class (lines 200-1162) → `apps/proxy/hls_output/session.py`

| Method/Property                  | Action       | Details                                                                                                                                                                                                                    |
| -------------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `__init__()`                     | **MODIFY**   | Remove `channel` parameter (used for profile lookup). Add `storage: SegmentStore` parameter. Remove `_stream_profile` cache.                                                                                               |
| `output_path` property           | **KEEP**     | Same — reads from config.                                                                                                                                                                                                  |
| `_refresh_output_path()`         | **KEEP**     | Same.                                                                                                                                                                                                                      |
| `_ensure_output_directory()`     | **KEEP**     | Same write-test logic.                                                                                                                                                                                                     |
| `_get_stream_profile()`          | **REMOVE**   | No longer needed. FFmpeg command built internally, not via StreamProfile.                                                                                                                                                  |
| `_get_user_agent()`              | **SIMPLIFY** | Remove profile-based user agent lookup. Just use the override or default.                                                                                                                                                  |
| `start()`                        | **MODIFY**   | Instead of calling `self._build_command()` which went through StreamProfile, call `self._build_ffmpeg_command()` directly. For Redis mode, start the watcher thread after FFmpeg starts.                                   |
| `stop()`                         | **MODIFY**   | Add watcher stop. Add SegmentStore cleanup call.                                                                                                                                                                           |
| `_is_source_hls()`               | **KEEP**     | Same URL check.                                                                                                                                                                                                            |
| `_build_command()`               | **REPLACE**  | Currently routes through StreamProfile.build_command(). Replace with `_build_ffmpeg_command()` that builds the command internally using HLS settings. This is the **key decoupling change** — no StreamProfile dependency. |
| `_build_fallback_command()`      | **ABSORB**   | The fallback command logic becomes the primary command builder since we no longer have profile-based commands. The enterprise-level FFmpeg flags from your migration 0029 are preserved here.                              |
| `_monitor_process()`             | **KEEP**     | Same process monitoring, ownership refresh, auto-restart logic.                                                                                                                                                            |
| `_read_ffmpeg_stderr()`          | **KEEP**     | Same stderr reading and byte-by-byte parsing.                                                                                                                                                                              |
| `_parse_ffmpeg_line()`           | **KEEP**     | Same line classification (info, error, warning).                                                                                                                                                                           |
| `_extract_input_format()`        | **KEEP**     | Same.                                                                                                                                                                                                                      |
| `_parse_stream_info()`           | **KEEP**     | Same codec/resolution/fps/bitrate extraction.                                                                                                                                                                              |
| `_parse_progress_line()`         | **KEEP**     | Same speed/fps/bitrate parsing.                                                                                                                                                                                            |
| `_update_metadata_field()`       | **KEEP**     | Same Redis metadata storage.                                                                                                                                                                                               |
| `_cleanup_segments()`            | **MODIFY**   | For filesystem mode: same as current. For Redis mode: call `store.cleanup_channel()` instead of file deletion.                                                                                                             |
| `_cleanup_all()`                 | **MODIFY**   | Same pattern but through SegmentStore.                                                                                                                                                                                     |
| `_try_automatic_stream_switch()` | **KEEP**     | Same failover logic. Remove `channel.get_hls_stream_profile()` reference (line 1083).                                                                                                                                      |
| `playlist_path` property         | **KEEP**     | Same.                                                                                                                                                                                                                      |
| `playlist_exists` property       | **MODIFY**   | For filesystem mode: check file. For Redis mode: check `store.get_playlist()`.                                                                                                                                             |
| `get_stat()`                     | **KEEP**     | Same Redis metadata read.                                                                                                                                                                                                  |

#### `HLSOutputManager` class (lines 1165-1810) → stays in `manager.py`

| Method                            | Action     | Details                                                                                          |
| --------------------------------- | ---------- | ------------------------------------------------------------------------------------------------ |
| `__init__()`                      | **KEEP**   | Same session dict, lock, Redis client, worker ID.                                                |
| `redis_client` property           | **KEEP**   | Same lazy init.                                                                                  |
| `_get_owner_key()`                | **KEEP**   | Same.                                                                                            |
| `_is_session_active_in_redis()`   | **KEEP**   | Same ownership check.                                                                            |
| `_try_acquire_ownership()`        | **KEEP**   | Same Redis SETNX pattern.                                                                        |
| `_release_ownership()`            | **KEEP**   | Same.                                                                                            |
| `_refresh_ownership()`            | **KEEP**   | Same.                                                                                            |
| `_is_session_running_elsewhere()` | **KEEP**   | Same multi-worker check.                                                                         |
| `_force_cleanup_stale_session()`  | **KEEP**   | Same orphaned process cleanup.                                                                   |
| `get_or_start_session()`          | **MODIFY** | Remove `channel` parameter dependency for profile lookup. Add SegmentStore creation via factory. |
| `stop_session()`                  | **KEEP**   | Same.                                                                                            |
| `change_stream_url()`             | **MODIFY** | Remove `channel.get_hls_stream_profile()` reference. Rest of stream switch logic stays the same. |
| `_publish_stream_change_event()`  | **KEEP**   | Same Redis pub/sub.                                                                              |
| `get_session()`                   | **KEEP**   | Same.                                                                                            |
| `stop_all_sessions()`             | **KEEP**   | Same.                                                                                            |
| `get_active_channels()`           | **KEEP**   | Same.                                                                                            |

**Summary of manager.py changes:** ~90% of the 1,811 lines are kept. The main changes are:

1. Remove all `get_hls_stream_profile()` calls (~5 occurrences)
2. Replace `_build_command()` with internal FFmpeg command builder (~50 lines rewritten)
3. Add SegmentStore creation in `get_or_start_session()` (~10 lines)
4. Add watcher start/stop for Redis mode (~20 lines)

---

### `apps/output/hls/client_manager.py` (55,260 chars / ~1,200 lines)

**Current:** HLSClientManager singleton with Redis backing, client tracking, cleanup thread, cooldown mechanism.

**Transformation → `apps/proxy/hls_output/client_manager.py`**

| Component                                         | Action   | Details                           |
| ------------------------------------------------- | -------- | --------------------------------- |
| `HLSClientManager` class                          | **KEEP** | Nearly identical.                 |
| Singleton pattern                                 | **KEEP** | Same `__new__` pattern.           |
| `redis_client` lazy property                      | **KEEP** | Same.                             |
| Key patterns: `hls_output:channel:{uuid}:clients` | **KEEP** | Already follows proxy convention. |
| `add_client()`, `remove_client()`                 | **KEEP** | Same.                             |
| `update_client_activity()`                        | **KEEP** | Same.                             |
| `get_client_count()`, `get_total_client_count()`  | **KEEP** | Same.                             |
| `is_channel_active()`                             | **KEEP** | Same.                             |
| `clear_cooldown()`                                | **KEEP** | Same.                             |
| `_start_cleanup_thread()`                         | **KEEP** | Same.                             |
| `_cleanup_loop()`                                 | **KEEP** | Same inactive channel detection.  |
| `_send_websocket_update()`                        | **KEEP** | Same.                             |

**Net change:** ~5 lines — change imports path from `apps.output.hls` to `apps.proxy.hls_output`. Functionally identical.

---

### `apps/output/hls/views.py` (639 lines)

**Current:** HTTP endpoints for playlist, segments, stream switching.

**Transformation → `apps/proxy/hls_output/views.py`**

| View Function                                      | Action     | Details                                                                                                                                                                                                                        |
| -------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `_get_client_id()`                                 | **KEEP**   | Same client ID generation from IP + user agent hash.                                                                                                                                                                           |
| `_get_client_ip()`                                 | **KEEP**   | Same X-Forwarded-For handling.                                                                                                                                                                                                 |
| `hls_master_playlist()`                            | **MODIFY** | Change URL path from `/output/hls/` to `/proxy/hls_output/`. Rest identical.                                                                                                                                                   |
| Readiness check loop (lines 121-167)               | **MODIFY** | Currently uses `glob.glob(os.path.join(session.output_path, "index*.ts"))` to count segments. For Redis mode: use `store.get_segment_count()`. For filesystem mode: keep current glob approach. Add storage backend detection. |
| Master playlist content generation (lines 180-193) | **MODIFY** | Change URL path from `/output/hls/` to `/proxy/hls_output/`.                                                                                                                                                                   |
| `hls_media_playlist()`                             | **MODIFY** | Currently reads playlist file with `open(session.playlist_path, 'r')`. For Redis mode: use `store.get_playlist()`. For filesystem mode: keep file read. Segment URL rewriting stays the same.                                  |
| `hls_segment()`                                    | **MODIFY** | Currently uses `FileResponse(open(segment_path, 'rb'))`. For Redis mode: use `store.get_segment()` → `HttpResponse(data)`. For filesystem mode: keep FileResponse.                                                             |
| `_get_stream_info_for_hls_switch()`                | **MODIFY** | Remove `channel.get_hls_stream_profile()` reference. Rest of stream info gathering stays.                                                                                                                                      |
| `change_stream()`                                  | **KEEP**   | Same logic — calls `hls_manager.change_stream_url()`.                                                                                                                                                                          |
| `next_stream()`                                    | **KEEP**   | Same logic — finds next stream and calls change.                                                                                                                                                                               |

**Summary of views.py changes:** ~80% kept. Main changes are:

1. Segment/playlist serving goes through SegmentStore abstraction (~30 lines changed)
2. URL paths updated from `/output/hls/` to `/proxy/hls_output/` (~5 lines)
3. Remove `get_hls_stream_profile()` reference (~3 lines)

---

### `apps/output/hls/urls.py` (52 lines)

**Transformation → `apps/proxy/hls_output/urls.py`**

**KEEP entirely.** Only change: `app_name = "hls_output"` instead of `"hls"`.

---

### `apps/output/hls/__init__.py` (3 lines)

**KEEP.** Move to new location.

---

### `apps/output/apps.py` (57 lines — you modified)

**Current:** Added HLS session cleanup on shutdown and HLS initialization on startup.

**Transformation:** The upstream version of this file (6 lines, just the AppConfig class) will be restored. HLS initialization/cleanup moves to a new location.

| Code                                        | Action   | Details                                                                                                                            |
| ------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `cleanup_all_hls_sessions()` function       | **MOVE** | Move to `apps/proxy/hls_output/manager.py` as a module-level function.                                                             |
| `OutputConfig.ready()` HLS init             | **MOVE** | Move to `apps/proxy/hls_output/__init__.py` or add a Django signal handler. Alternatively, initialize lazily on first HLS request. |
| `atexit.register(cleanup_all_hls_sessions)` | **MOVE** | Register in the HLS output module's initialization.                                                                                |

**Result:** `apps/output/apps.py` reverts to upstream's clean 6-line version.

---

### `apps/output/urls.py` (16 lines — you modified)

**Current:** Added `path("hls/", include("apps.output.hls.urls", namespace="hls"))`.

**Transformation:** Revert to upstream's version (no HLS include). The HLS output URLs will be registered via `apps/proxy/urls.py` instead.

---

### `apps/hdhr/hls_urls.py` (27 lines — your creation)

**KEEP entirely.** This is a new file that doesn't conflict with anything.

---

### `apps/hdhr/api_views.py` (your HLS additions: ~120 lines)

**Current:** Added `HLSDiscoverAPIView`, `HLSLineupAPIView`, `HLSHDHRDeviceXMLAPIView`.

**Transformation:**

| View Class                | Action   | Details                                                                                                |
| ------------------------- | -------- | ------------------------------------------------------------------------------------------------------ |
| `HLSDiscoverAPIView`      | **KEEP** | Same discovery JSON with HLS device ID. Change stream URL from `/output/hls/` to `/proxy/hls_output/`. |
| `HLSLineupAPIView`        | **KEEP** | Same channel lineup. Change stream URL from `/output/hls/` to `/proxy/hls_output/`.                    |
| `HLSHDHRDeviceXMLAPIView` | **KEEP** | Same device XML.                                                                                       |

**Net change:** ~3 URL string updates.

---

### `core/models.py` (your additions: ~180 lines)

**Current modifications to upstream's `core/models.py`:**

| Addition                                                      | Action      | Details                                                                                                 |
| ------------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------- |
| `HLS_PROXY_PROFILE_NAME = "HLS Proxy"`                        | **REMOVE**  | No longer needed — no locked HLS profiles.                                                              |
| `HLS_FFMPEG_PROFILE_NAME = "HLS FFmpeg"`                      | **REMOVE**  | Same reason.                                                                                            |
| `PROFILE_TYPE_TS = "ts"` / `PROFILE_TYPE_HLS = "hls"`         | **REMOVE**  | No `profile_type` concept.                                                                              |
| `PROFILE_TYPE_CHOICES`                                        | **REMOVE**  | Same.                                                                                                   |
| `profile_type` field on StreamProfile                         | **REMOVE**  | Schema change reverted — upstream's model structure preserved.                                          |
| `is_hls_proxy()`, `is_hls_ffmpeg()`, `is_hls_profile()`       | **REMOVE**  | No longer needed.                                                                                       |
| `build_command()` with `hls_output_path`, `hls_config` params | **REVERT**  | Restore upstream's simple `build_command(stream_url, user_agent)` signature.                            |
| `HLS_OUTPUT_SETTINGS_KEY`                                     | **REPLACE** | Add `HLS_OUTPUT_SETTINGS_KEY = "hls_output_settings"` as a simple constant. Use `_get_group()` pattern. |
| `get_hls_output_settings()` classmethod                       | **REPLACE** | Rewrite to use `cls._get_group(HLS_OUTPUT_SETTINGS_KEY, defaults)` pattern.                             |
| `set_hls_output_settings()` classmethod                       | **REPLACE** | Rewrite to use `cls._update_group()` pattern.                                                           |

**Result:** The only additions to `core/models.py` are:

- 1 constant: `HLS_OUTPUT_SETTINGS_KEY = "hls_output_settings"`
- 2 helper methods that follow the exact same pattern as `get_proxy_settings()`, `get_dvr_settings()`, etc.

---

### `apps/channels/models.py` (your additions: ~50 lines)

**Current:** Added `get_hls_stream_profile()` method to Channel model.

| Addition                                         | Action     | Details                                                                                                            |
| ------------------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------ |
| `get_hls_stream_profile()` method                | **REMOVE** | The HLS output module builds FFmpeg commands internally. No need for the Channel model to know about HLS profiles. |
| `get_active_stream_profile()` HLS fallback logic | **REVERT** | Restore upstream's original method without HLS fallback.                                                           |

**Result:** `apps/channels/models.py` reverts to upstream's version completely.

---

### Core Migrations (15 files)

**Current:** `core/migrations/0019_add_hls_stream_profiles.py` through `0033_hls_remove_epoch_segment_numbering.py`

| Migration                                        | Action     | Details                                                                                                             |
| ------------------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------- |
| `0019_add_hls_stream_profiles.py`                | **REMOVE** | No `profile_type` field, no HLS locked profiles.                                                                    |
| `0020_update_hls_ffmpeg_profile.py`              | **REMOVE** | No HLS FFmpeg profile to update.                                                                                    |
| `0021_remove_hls_reconnect_flags.py`             | **REMOVE** | Same.                                                                                                               |
| `0022_hls_ffmpeg_use_config_placeholders.py`     | **REMOVE** | Same.                                                                                                               |
| `0023_hls_segment_naming_refactor.py`            | **REMOVE** | Same.                                                                                                               |
| `0024_hls_increase_delete_threshold.py`          | **REMOVE** | Same.                                                                                                               |
| `0025_hls_dynamic_segment_extension.py`          | **REMOVE** | Same.                                                                                                               |
| `0026_hls_dynamic_delete_threshold.py`           | **REMOVE** | Same.                                                                                                               |
| `0027_hls_remove_delete_segments.py`             | **REMOVE** | Same.                                                                                                               |
| `0028_hls_ffmpeg_input_buffering.py`             | **REMOVE** | Same.                                                                                                               |
| `0029_hls_enterprise_ffmpeg_flags.py`            | **REMOVE** | The enterprise FFmpeg flags are preserved in the new module's `_build_ffmpeg_command()` method, not in a migration. |
| `0030_hls_add_reconnect_flags.py`                | **REMOVE** | Same — reconnect flags live in the new module code.                                                                 |
| `0031_add_vlc_stream_profile.py`                 | **REMOVE** | This is upstream's `0019` — upstream already has it.                                                                |
| `0032_change_coresettings_value_to_jsonfield.py` | **REMOVE** | This is upstream's `0020` — upstream already has it.                                                                |
| `0033_hls_remove_epoch_segment_numbering.py`     | **REMOVE** | Segment naming logic lives in the new module code.                                                                  |

**Replaced with:** 1 new migration `core/migrations/00XX_hls_output_settings.py` that creates the settings entry.

---

### Other Backend Files

| File                                                   | Current Changes | Action                                                                                                                                                       |
| ------------------------------------------------------ | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `core/serializers.py` — `HLSOutputSettingsSerializer`  | **REWRITE**     | Keep the serializer but simplify. Remove `profile_type`-related fields. Keep all player settings fields. Align validation with upstream serializer patterns. |
| `core/api_views.py` — `HLSOutputSettingsViewSet`       | **REWRITE**     | Simplify to use `CoreSettings._get_group()` / `_update_group()` pattern. Remove `json.loads(settings_obj.value)` pattern.                                    |
| `core/api_urls.py` — HLS settings route                | **KEEP**        | Change basename from `hls-settings` to `hls-output-settings`.                                                                                                |
| `core/tasks.py` — HLS channel tracking                 | **REMOVE**      | HLS output manages its own channel tracking internally.                                                                                                      |
| `apps/proxy/ts_proxy/views.py` — HLS status tracking   | **REMOVE**      | Status tracking is internal to HLS output module.                                                                                                            |
| `apps/proxy/ts_proxy/client_manager.py` — HLS tracking | **REMOVE**      | Same.                                                                                                                                                        |
| `dispatcharr/urls.py` — hdhr-hls URL                   | **KEEP**        | Same additive change.                                                                                                                                        |
| `dispatcharr/settings.py` — HLS proxy settings         | **REMOVE**      | HLS settings are in CoreSettings, not Django settings.                                                                                                       |
| `docker/docker-compose.yml` — HLS comments             | **REWRITE**     | Update comments to reference `HLS_PATH` instead of `HLS_OUTPUT_PATH`. Simplify the options documentation.                                                    |

---

## FRONTEND FILES

---

### `frontend/src/components/FloatingVideo.jsx`

**Current modifications (your additions: ~130 lines):**

| Code Section                                       | Action     | Details                                                                        |
| -------------------------------------------------- | ---------- | ------------------------------------------------------------------------------ |
| `import Hls from 'hls.js'` (line 6)                | **KEEP**   | Same import.                                                                   |
| `DEFAULT_HLS_PLAYER_SETTINGS` object (lines 10-23) | **KEEP**   | Same defaults.                                                                 |
| `hlsRef` ref (line 34)                             | **KEEP**   | Same ref for HLS.js instance.                                                  |
| `hlsSettingsRef` ref (line 49)                     | **KEEP**   | Same ref for settings.                                                         |
| HLS.js destroy in cleanup (lines 133-141)          | **KEEP**   | Same cleanup logic.                                                            |
| `fetchHlsSettings` useEffect (lines 404-425)       | **MODIFY** | Change `API.getHLSSettings()` to `API.getHLSOutputSettings()`. Rest identical. |
| `initializeHLSPlayer()` function (lines 428-515)   | **KEEP**   | Same HLS.js initialization, config building, error handling.                   |
| `isHLSUrl()` helper (lines 518-522)                | **KEEP**   | Same URL detection.                                                            |
| Player selection logic (lines 536-538)             | **KEEP**   | Same conditional — if HLS URL, use HLS player; else mpegts.                    |

**Net change to FloatingVideo.jsx:** 1 method name change (`getHLSSettings` → `getHLSOutputSettings`). Everything else is identical to your current code. The upstream version of FloatingVideo.jsx will need the same HLS additions re-applied — but since these are purely **additive** (no existing code is changed), this is a clean merge.

---

### `frontend/src/components/forms/settings/HlsSettingsForm.jsx` (338 lines)

**Transformation → `frontend/src/components/forms/settings/HlsOutputSettingsForm.jsx`**

| Code Section                                      | Action     | Details                                                                                                                                   |
| ------------------------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Component name `HlsSettingsForm`                  | **RENAME** | → `HlsOutputSettingsForm` (clearer naming).                                                                                               |
| State: `hlsSettings`                              | **KEEP**   | Same state object with all settings.                                                                                                      |
| `loadHlsSettings` calls `API.getHLSSettings()`    | **MODIFY** | → `API.getHLSOutputSettings()`                                                                                                            |
| `saveHlsSettings` calls `API.updateHLSSettings()` | **MODIFY** | → `API.updateHLSOutputSettings()`                                                                                                         |
| **ADD:** Storage backend selector                 | **ADD**    | New dropdown: "Storage Backend" with options "filesystem" and "redis". ~15 lines.                                                         |
| Output Path field (read-only, from env)           | **MODIFY** | Label: "HLS Path" with description: "Configured via HLS_PATH environment variable. Supports RAM disk (/dev/shm), tmpfs, or regular disk." |
| Shutdown Delay field                              | **KEEP**   | Same.                                                                                                                                     |
| Segment Duration field                            | **KEEP**   | Same.                                                                                                                                     |
| Playlist Size field                               | **KEEP**   | Same.                                                                                                                                     |
| Retention Seconds field                           | **KEEP**   | Same.                                                                                                                                     |
| fMP4 Segments switch                              | **KEEP**   | Same.                                                                                                                                     |
| LL-HLS switch                                     | **KEEP**   | Same.                                                                                                                                     |
| HLS.js Player Settings section                    | **KEEP**   | Same — all 11 player settings.                                                                                                            |
| Save button                                       | **KEEP**   | Same.                                                                                                                                     |

**Net change:** ~20 lines modified (API method names, component name, path label). ~15 lines added (storage backend selector). All form fields and logic preserved.

---

### `frontend/src/pages/Settings.jsx`

**Current modification (your addition: ~8 lines):**

```jsx
const HlsSettingsForm = React.lazy(
  () => import("../components/forms/settings/HlsSettingsForm.jsx"),
);

<AccordionItem value="hls-settings">
  <AccordionControl>HLS Settings</AccordionControl>
  <AccordionPanel>
    <Suspense fallback={<Loader />}>
      <HlsSettingsForm active={accordianValue === "hls-settings"} />
    </Suspense>
  </AccordionPanel>
</AccordionItem>;
```

**Transformation:**

| Line                  | Action     | Details                                              |
| --------------------- | ---------- | ---------------------------------------------------- |
| Import                | **MODIFY** | Change to `HlsOutputSettingsForm` from renamed file. |
| AccordionItem value   | **MODIFY** | Change to `"hls-output-settings"`.                   |
| AccordionControl text | **MODIFY** | Change to `"HLS Output"` (clearer).                  |
| Component reference   | **MODIFY** | Change to `<HlsOutputSettingsForm>`.                 |

**Net change:** 4 string changes. Structure identical.

---

### `frontend/src/store/settings.jsx`

**Current modifications (your additions: ~15 lines):**

```jsx
streamFormat: 'ts',
setStreamFormat: (format) => set({ streamFormat: format }),
getChannelUrl: (channelUuid, baseUrl) => { ... }
```

**Transformation:**

| Code                 | Action     | Details                                                                                                                   |
| -------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------- |
| `streamFormat: 'ts'` | **KEEP**   | Same default.                                                                                                             |
| `setStreamFormat()`  | **KEEP**   | Same setter.                                                                                                              |
| `getChannelUrl()`    | **MODIFY** | Change HLS URL from `/output/hls/{uuid}/playlist.m3u8` to `/proxy/hls_output/{uuid}/playlist.m3u8`. Same logic otherwise. |

**Net change:** 1 URL string change.

---

### `frontend/src/store/useVideoStore.jsx`

**Current modification (your addition: ~2 lines):**

```jsx
streamFormat: 'ts',
```

**Transformation:** **KEEP** — same addition.

---

### `frontend/src/api.js`

**Current additions (~20 lines):**

```jsx
static async getHLSSettings() { ... }
static async updateHLSSettings(settings) { ... }
```

**Transformation:**

| Method                | Action     | Details                                                                                                         |
| --------------------- | ---------- | --------------------------------------------------------------------------------------------------------------- |
| `getHLSSettings()`    | **RENAME** | → `getHLSOutputSettings()`. Change endpoint from `/api/core/hls-settings/` to `/api/core/hls-output-settings/`. |
| `updateHLSSettings()` | **RENAME** | → `updateHLSOutputSettings()`. Same endpoint change.                                                            |

**Net change:** 2 method names and 2 URL strings.

---

### `frontend/src/components/tables/ChannelsTable.jsx`

**Current modifications (~50 lines across the file):**

| Code Section                                   | Action     | Details                                             |
| ---------------------------------------------- | ---------- | --------------------------------------------------- |
| `hdhrHlsUrlBase` constant (line 75)            | **KEEP**   | Same.                                               |
| HLS URL generation (lines 604-608)             | **MODIFY** | Change from `/output/hls/` to `/proxy/hls_output/`. |
| M3U format parameter (line 683)                | **KEEP**   | Same `format=hls` parameter.                        |
| HDHR HLS URL copy notification (lines 735-740) | **KEEP**   | Same.                                               |
| HDHR HLS input field (line 1241)               | **KEEP**   | Same.                                               |
| Format toggle button styling (lines 1269-1278) | **KEEP**   | Same styling logic.                                 |
| SegmentedControl for TS/HLS (lines 1358-1360)  | **KEEP**   | Same.                                               |

**Net change:** 1 URL path change. All UI elements preserved.

---

### `frontend/src/components/tables/ChannelTableStreams.jsx`

**Current modification (~8 lines):**

```jsx
if (streamFormat === "hls") {
  vidUrl = `/output/hls/${streamHash}/playlist.m3u8`;
}
```

**Transformation:** Change URL to `/proxy/hls_output/${streamHash}/playlist.m3u8`. Same logic.

---

### `frontend/src/components/tables/StreamsTable.jsx`

**Current modification (~8 lines):** Same as ChannelTableStreams.

**Transformation:** Same URL change.

---

### `frontend/src/components/tables/StreamProfilesTable.jsx`

**Current modification (~6 lines):** Added HLS badge for profile_type.

**Transformation:** **REMOVE** — no `profile_type` field exists in the new approach. Revert to upstream's version.

---

### `frontend/src/components/forms/StreamProfile.jsx`

**Current modifications (~15 lines):** Added profile_type dropdown and HLS Output option.

**Transformation:** **REMOVE** — no `profile_type` field. Revert to upstream's version (which uses react-hook-form — note the upstream refactored this since your last sync).

---

### `frontend/package.json`

**Current addition:** `hls.js` dependency.

**Transformation:** **KEEP** — same dependency, verify version compatibility with upstream's package.json.

---

## SUMMARY TABLE

### Backend Files: What Happens to Each

| Current File                         | Lines  | Fate                                              | Reuse % |
| ------------------------------------ | ------ | ------------------------------------------------- | ------- |
| `apps/output/hls/__init__.py`        | 3      | Move to `apps/proxy/hls_output/__init__.py`       | 100%    |
| `apps/output/hls/config.py`          | 274    | Port to `apps/proxy/hls_output/config.py`         | 95%     |
| `apps/output/hls/manager.py`         | 1,811  | Split to `manager.py` + `session.py`              | 85%     |
| `apps/output/hls/client_manager.py`  | ~1,200 | Port to `apps/proxy/hls_output/client_manager.py` | 99%     |
| `apps/output/hls/views.py`           | 639    | Port to `apps/proxy/hls_output/views.py`          | 80%     |
| `apps/output/hls/urls.py`            | 52     | Port to `apps/proxy/hls_output/urls.py`           | 95%     |
| `apps/hdhr/hls_urls.py`              | 27     | Keep in place                                     | 100%    |
| `apps/hdhr/api_views.py` (HLS parts) | ~120   | Keep, update URLs                                 | 97%     |
| `core/models.py` (HLS additions)     | ~180   | Replace with ~25 lines using upstream pattern     | 15%     |
| `core/serializers.py` (HLS parts)    | ~100   | Simplify, keep player settings                    | 70%     |
| `core/api_views.py` (HLS parts)      | ~80    | Rewrite to use `_get_group()` pattern             | 50%     |
| 15 core migrations                   | ~2,500 | Replace with 1 migration (~30 lines)              | 0%      |

**NEW files added:**
| New File | Lines | Purpose |
|----------|-------|---------|
| `apps/proxy/hls_output/storage/base.py` | ~40 | SegmentStore interface |
| `apps/proxy/hls_output/storage/redis_store.py` | ~80 | Redis backend |
| `apps/proxy/hls_output/storage/filesystem_store.py` | ~80 | Filesystem backend |
| `apps/proxy/hls_output/watcher.py` | ~100 | File watcher for Redis mode |
| `apps/proxy/hls_output/tests.py` | ~200 | Tests |

### Frontend Files: What Happens to Each

| Current File                          | Fate                                                        | Reuse % |
| ------------------------------------- | ----------------------------------------------------------- | ------- |
| `HlsSettingsForm.jsx`                 | Rename to `HlsOutputSettingsForm.jsx`, add backend selector | 90%     |
| `FloatingVideo.jsx` (HLS additions)   | Re-apply same additions to upstream version                 | 99%     |
| `Settings.jsx` (HLS accordion)        | Same pattern, updated names                                 | 95%     |
| `settings.jsx` store (HLS parts)      | Same, update URL path                                       | 98%     |
| `useVideoStore.jsx` (streamFormat)    | Same                                                        | 100%    |
| `api.js` (HLS methods)                | Rename methods, update endpoints                            | 90%     |
| `ChannelsTable.jsx` (HLS parts)       | Same, update URL path                                       | 97%     |
| `ChannelTableStreams.jsx` (HLS parts) | Same, update URL path                                       | 97%     |
| `StreamsTable.jsx` (HLS parts)        | Same, update URL path                                       | 97%     |
| `StreamProfilesTable.jsx` (HLS badge) | REMOVE — revert to upstream                                 | 0%      |
| `StreamProfile.jsx` (profile_type)    | REMOVE — revert to upstream                                 | 0%      |
