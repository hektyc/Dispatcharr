# Plan: Sync Local Dispatcharr Dev with Upstream While Preserving HLS Features

## 1. Current Sync Status

### Fork: `hektyc/Dispatcharr` (dev branch)

- **Latest commit**: `e4a4f3f9` — "Fix: Only fetch HLS settings when video player is visible" (Jan 17, 2026)
- **Last upstream merge**: `5b6f800b` — "Merge upstream/dev: Settings refactor + Stats reorganization" (Jan 14, 2026)
- **Synced up to upstream v0.17.0** (Jan 13, 2026)

### Upstream: `Dispatcharr/Dispatcharr` (dev branch)

- **Latest commit**: `61d713fc` — "test: update validation for empty CIDR range" (Mar 21, 2026)
- **Latest release**: v0.21.1 (Mar 18, 2026)
- **Your fork is ~2 months and 4 major versions behind (v0.17.0 → v0.21.1+)**

### Your HLS-Specific Commits on Top of Upstream

1. `5b6f800b` — Merge upstream/dev: Settings refactor + Stats reorganization
2. `d65fb7eb` — Fix migration dependency conflict after upstream merge
3. `859d614e` — Add dynamic HLS.js player settings and remove epoch segment numbering
4. `e4a4f3f9` — Fix: Only fetch HLS settings when video player is visible
5. Additional local uncommitted changes in `apps/output/hls/client_manager.py`

---

## 2. Your HLS Feature Implementation — Comprehensive Inventory

### 2.1 New Files (Entirely Your Creation)

| File                                                         | Purpose                                                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `apps/output/hls/__init__.py`                                | HLS output module marker                                                                          |
| `apps/output/hls/config.py`                                  | HLS configuration with cache, env-based output path, settings from DB                             |
| `apps/output/hls/manager.py`                                 | FFmpeg process manager for HLS segment generation, session management, automatic stream switching |
| `apps/output/hls/client_manager.py`                          | Redis-backed client tracking, WebSocket updates, cleanup thread, multi-worker support             |
| `apps/output/hls/views.py`                                   | HTTP endpoints for master playlist, media playlist, segments, stream switching                    |
| `apps/output/hls/urls.py`                                    | URL routing for HLS output endpoints                                                              |
| `apps/hdhr/hls_urls.py`                                      | Dedicated HDHR-HLS URL patterns for Plex/Channels DVR                                             |
| `frontend/src/components/forms/settings/HlsSettingsForm.jsx` | Settings UI for all HLS configuration                                                             |

### 2.2 HLS-Specific Database Migrations (Your Custom)

| Migration                                                    | Purpose                                                                      |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `core/migrations/0019_add_hls_stream_profiles.py`            | Adds `profile_type` field + creates HLS Proxy and HLS FFmpeg stream profiles |
| `core/migrations/0020_update_hls_ffmpeg_profile.py`          | Add reconnection options to HLS FFmpeg                                       |
| `core/migrations/0021_remove_hls_reconnect_flags.py`         | Remove reconnect flags from HLS FFmpeg                                       |
| `core/migrations/0022_hls_ffmpeg_use_config_placeholders.py` | Use dynamic config placeholders                                              |
| `core/migrations/0023_hls_segment_naming_refactor.py`        | Change segment naming format                                                 |
| `core/migrations/0024_hls_increase_delete_threshold.py`      | Add `program_date_time` flag                                                 |
| `core/migrations/0025_hls_dynamic_segment_extension.py`      | Dynamic `.ts`/`.m4s` extension                                               |
| `core/migrations/0026_hls_dynamic_delete_threshold.py`       | Dynamic delete threshold                                                     |
| `core/migrations/0027_hls_remove_delete_segments.py`         | Remove `delete_segments` flag                                                |
| `core/migrations/0028_hls_ffmpeg_input_buffering.py`         | Add input buffering options                                                  |
| `core/migrations/0029_hls_enterprise_ffmpeg_flags.py`        | Enterprise-level FFmpeg flags                                                |
| `core/migrations/0030_hls_add_reconnect_flags.py`            | Re-add reconnect flags                                                       |
| `core/migrations/0033_hls_remove_epoch_segment_numbering.py` | Remove epoch-based segment numbering                                         |

### 2.3 Modified Existing Files (Upstream files with HLS additions)

| File                                                     | HLS Modifications                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core/models.py`                                         | Added `PROFILE_TYPE_HLS`, `HLS_PROXY_PROFILE_NAME`, `HLS_FFMPEG_PROFILE_NAME`, `profile_type` field on `StreamProfile`, `is_hls_proxy()`, `is_hls_ffmpeg()`, `is_hls_profile()`, `build_command()` with `hls_output_path`/`hls_config` params, `HLS_OUTPUT_SETTINGS_KEY`, `get_hls_output_settings()`, `set_hls_output_settings()` |
| `core/serializers.py`                                    | Added `HLSOutputSettingsSerializer` with all HLS.js player settings                                                                                                                                                                                                                                                                |
| `core/api_views.py`                                      | Added `HLSOutputSettingsViewSet` REST API endpoint                                                                                                                                                                                                                                                                                 |
| `core/api_urls.py`                                       | Added `hls-settings` router registration                                                                                                                                                                                                                                                                                           |
| `core/tasks.py`                                          | Added HLS output channel tracking                                                                                                                                                                                                                                                                                                  |
| `apps/output/apps.py`                                    | Added HLS session cleanup on shutdown, HLS initialization on startup                                                                                                                                                                                                                                                               |
| `apps/output/urls.py`                                    | Added HLS URL inclusion: `path("hls/", include(...))`                                                                                                                                                                                                                                                                              |
| `apps/output/views.py`                                   | Added `format=hls` query parameter support for HLS output                                                                                                                                                                                                                                                                          |
| `apps/hdhr/api_views.py`                                 | Added `HLSDiscoverAPIView`, `HLSLineupAPIView`, `HLSHDHRDeviceXMLAPIView`                                                                                                                                                                                                                                                          |
| `apps/channels/models.py`                                | Added `get_hls_stream_profile()` method to Channel model, HLS fallback logic in `get_active_stream_profile()`                                                                                                                                                                                                                      |
| `apps/proxy/ts_proxy/views.py`                           | Added HLS output channel status tracking                                                                                                                                                                                                                                                                                           |
| `apps/proxy/ts_proxy/client_manager.py`                  | Added HLS output channel tracking                                                                                                                                                                                                                                                                                                  |
| `dispatcharr/urls.py`                                    | Added `hdhr-hls` URL pattern                                                                                                                                                                                                                                                                                                       |
| `dispatcharr/settings.py`                                | Added HLS proxy settings                                                                                                                                                                                                                                                                                                           |
| `docker/docker-compose.yml`                              | Added HLS RAM disk options, `HLS_OUTPUT_PATH` env variable                                                                                                                                                                                                                                                                         |
| `frontend/src/api.js`                                    | Added `getHLSSettings()`, `updateHLSSettings()` API methods                                                                                                                                                                                                                                                                        |
| `frontend/src/pages/Settings.jsx`                        | Added HLS Settings accordion section                                                                                                                                                                                                                                                                                               |
| `frontend/src/components/FloatingVideo.jsx`              | Added HLS.js player support with dynamic settings from backend                                                                                                                                                                                                                                                                     |
| `frontend/src/store/useVideoStore.jsx`                   | Added `streamFormat` state for TS/HLS toggle                                                                                                                                                                                                                                                                                       |
| `frontend/src/store/settings.jsx`                        | Added `streamFormat` preference, `setStreamFormat()`, HLS URL builder                                                                                                                                                                                                                                                              |
| `frontend/src/components/tables/ChannelsTable.jsx`       | Added HLS format toggle, HDHR-HLS URL, HLS stream URLs                                                                                                                                                                                                                                                                             |
| `frontend/src/components/tables/ChannelTableStreams.jsx` | Added HLS stream preview support                                                                                                                                                                                                                                                                                                   |
| `frontend/src/components/tables/StreamsTable.jsx`        | Added HLS stream preview support                                                                                                                                                                                                                                                                                                   |
| `frontend/src/components/tables/StreamProfilesTable.jsx` | Added HLS badge display                                                                                                                                                                                                                                                                                                            |
| `frontend/src/components/forms/StreamProfile.jsx`        | Added `profile_type` field, HLS output type option                                                                                                                                                                                                                                                                                 |

---

## 3. Migration Conflict Analysis

This is the **most critical challenge**. The migration numbering is severely diverged:

### Upstream Migration Numbering (dev branch, current)

```
0001-0018: Shared with your fork
0019: add_vlc_stream_profile          ← Your fork has this as 0031
0020: change_coresettings_value_to_jsonfield  ← Your fork has this as 0032
0021: systemnotification_notificationdismissal  ← NEW - not in your fork at all
```

### Your Fork Migration Numbering

```
0001-0018: Shared with upstream
0019-0030: HLS-specific migrations    ← These don't exist upstream
0031: add_vlc_stream_profile           ← Upstream's 0019
0032: change_coresettings_value_to_jsonfield  ← Upstream's 0020
0033: hls_remove_epoch_segment_numbering  ← HLS-specific, not upstream
```

### Key Problem

- Upstream's `0019` → your `0031` (VLC profile)
- Upstream's `0020` → your `0032` (CoreSettings JSONField)
- Upstream's `0021` (SystemNotification) → completely missing from your fork
- Your HLS migrations `0019-0030` and `0033` don't exist upstream

---

## 4. Recommended Approach: Rebase HLS Migrations on Top of Upstream

### Strategy: "Squash HLS migrations, Rebase on Upstream"

Since all 15 HLS migrations (0019-0030, 0033) ultimately produce a single net result (one `profile_type` field + two stream profiles created via RunPython), the safest approach is:

1. **Squash all HLS migrations into a single migration** that runs AFTER upstream's latest migration
2. **Merge upstream dev into your local dev branch**
3. **Re-apply your HLS changes on top** of the merged result

### Detailed Steps

#### Phase 1: Preparation and Backup

- Create a backup branch of your current local state
- Document the exact current state of all HLS-modified files
- Export a list of all HLS-specific changes as patches

#### Phase 2: Sync GitHub Fork with Upstream

- Add upstream remote: `git remote add upstream https://github.com/Dispatcharr/Dispatcharr.git`
- Fetch upstream: `git fetch upstream`
- Create a working branch: `git checkout -b hls-rebase dev`

#### Phase 3: Migration Resolution

- Remove all custom HLS migrations (0019-0030, 0033)
- Remove the renumbered upstream migrations (0031, 0032 which are upstream 0019, 0020)
- Merge upstream dev (which has proper 0019, 0020, 0021)
- Create a NEW single HLS migration numbered after upstream's latest (e.g., 0022) that:
  - Adds `profile_type` field to `StreamProfile`
  - Creates `HLS Proxy` and `HLS FFmpeg` stream profiles with the latest ffmpeg parameters
  - All the intermediate tweaks (reconnect flags, naming, enterprise flags, etc.) are collapsed into the final state

#### Phase 4: File-by-File Merge

For each modified file, carefully merge upstream changes with HLS additions:

**High-conflict files (upstream likely changed significantly):**

- `core/models.py` — Keep your HLS additions, accept upstream structural changes
- `core/api_views.py` — Keep HLS ViewSet, accept upstream changes
- `core/serializers.py` — Keep HLS serializer, accept upstream changes
- `apps/output/views.py` — Significantly changed upstream (145KB now), must merge carefully
- `apps/channels/models.py` — Keep `get_hls_stream_profile()`, accept upstream changes
- `frontend/src/components/FloatingVideo.jsx` — HLS.js player may conflict with upstream player changes
- `frontend/src/pages/Settings.jsx` — Keep HLS accordion, accept upstream settings changes
- `frontend/src/components/tables/ChannelsTable.jsx` — Significant upstream changes likely
- `frontend/src/components/forms/StreamProfile.jsx` — Upstream refactored to react-hook-form

**Lower-conflict files (likely additive only):**

- `apps/output/apps.py` — Your file is already customized; upstream is simple
- `apps/output/urls.py` — Your HLS inclusion is additive
- `apps/hdhr/api_views.py` — HLS endpoints are additive
- `core/api_urls.py` — HLS route is additive
- `core/tasks.py` — HLS channel tracking is additive
- `dispatcharr/urls.py` — HLS URL pattern is additive
- `docker/docker-compose.yml` — HLS config is additive

**Files entirely yours (no upstream conflict):**

- `apps/output/hls/*` — Entire directory is yours
- `apps/hdhr/hls_urls.py` — Entirely yours
- `frontend/src/components/forms/settings/HlsSettingsForm.jsx` — Entirely yours

#### Phase 5: Frontend Package Check

- Verify `hls.js` is still in `package.json` dependencies
- Check if upstream changed any frontend build config that affects HLS.js

#### Phase 6: Testing

- Run Django migrations to verify no conflicts
- Test HLS streaming endpoints manually
- Test TS proxy still works normally
- Test HDHR-HLS discovery
- Test frontend HLS toggle and video player
- Test HLS settings page

---

## 5. Risk Assessment

| Risk                                                                       | Severity | Mitigation                                                                      |
| -------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------- |
| Migration conflicts crash Django startup                                   | HIGH     | Squash all HLS migrations into single post-upstream migration                   |
| Upstream `apps/output/views.py` changes break HLS format parameter         | MEDIUM   | Carefully diff and merge the `format=hls` additions                             |
| Upstream frontend refactoring breaks HLS.js integration                    | MEDIUM   | Check upstream FloatingVideo.jsx changes, may need significant re-integration   |
| StreamProfile model upstream changes conflict with `profile_type`          | HIGH     | The upstream has no `profile_type` but may have changed the model significantly |
| Settings system refactor (JSONField) already merged but may have evolved   | MEDIUM   | Your fork already handled this merge once in Jan 2026                           |
| New upstream features depend on code that conflicts with HLS modifications | LOW      | HLS is mostly additive; main risk is in shared files                            |

---

## 6. Alternative Approach: Fresh Rebase

If the merge approach proves too complex, an alternative is:

1. Start from a fresh clone of upstream dev
2. Apply ONLY your HLS files (the `apps/output/hls/` directory)
3. Re-implement the integration points (model changes, URL routing, frontend) against the current upstream codebase
4. Create a single clean migration for all HLS schema changes

This is cleaner but requires more manual work to re-integrate all the touchpoints.
