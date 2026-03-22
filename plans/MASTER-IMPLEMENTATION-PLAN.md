# Master Implementation Plan: HLS Output for Dispatcharr

## Table of Contents

1. [Fork Reset and Upstream Sync](#1-fork-reset-and-upstream-sync)
2. [HLS Output Module Architecture](#2-hls-output-module-architecture)
3. [Backend Implementation Plan](#3-backend-implementation-plan)
4. [Frontend Implementation Plan](#4-frontend-implementation-plan)
5. [Migration Strategy](#5-migration-strategy)
6. [File-by-File Change List](#6-file-by-file-change-list)
7. [What Changes vs. Current Implementation](#7-what-changes-vs-current-implementation)
8. [Step-by-Step Execution Order](#8-step-by-step-execution-order)

---

## 1. Fork Reset and Upstream Sync

### Step 1.1: Create Backup of Current Work

```bash
# Create a backup branch with ALL your current HLS work
git checkout dev
git checkout -b hls-backup-2026-01-17
git push origin hls-backup-2026-01-17

# Also create a local tag for safety
git tag hls-work-backup
```

This preserves your entire HLS implementation as a reference branch.

### Step 1.2: Reset Fork to Upstream

```bash
# Add upstream remote if not already present
git remote add upstream https://github.com/Dispatcharr/Dispatcharr.git
git fetch upstream

# Reset your dev branch to match upstream dev exactly
git checkout dev
git reset --hard upstream/dev
git push origin dev --force
```

After this, your fork's `dev` branch is identical to upstream's `dev` branch (at v0.21.1+).

### Step 1.3: Create HLS Feature Branch

```bash
# Create a new feature branch from the clean upstream dev
git checkout -b feature/hls-output dev
```

All HLS work happens on this branch. When upstream updates, you can:

```bash
git checkout dev
git pull upstream dev
git checkout feature/hls-output
git rebase dev  # Minimal conflicts because HLS touches few upstream files
```

---

## 2. HLS Output Module Architecture

### Location: `apps/proxy/hls_output/`

Following the established upstream pattern where each proxy type is a sub-module of `apps/proxy/`:

- `apps/proxy/ts_proxy/` — existing TS proxy
- `apps/proxy/hls_proxy/` — existing HLS input proxy
- `apps/proxy/vod_proxy/` — existing VOD proxy
- `apps/proxy/hls_output/` — **NEW** HLS output module

### Module Structure

```
apps/proxy/hls_output/
  ├── __init__.py              # Module docstring
  ├── config.py                # Configuration: HLSOutputConfig
  ├── manager.py               # HLSOutputManager: FFmpeg process lifecycle
  ├── session.py               # HLSSession: Per-channel session management
  ├── storage/
  │   ├── __init__.py
  │   ├── base.py              # SegmentStore abstract base class
  │   ├── redis_store.py       # RedisSegmentStore implementation
  │   └── filesystem_store.py  # FilesystemSegmentStore implementation
  ├── watcher.py               # FileWatcher: Reads FFmpeg output, pushes to storage
  ├── client_manager.py        # Client tracking via Redis
  ├── views.py                 # HTTP endpoints: playlist, segments, stream control
  ├── urls.py                  # URL routing
  └── tests.py                 # Tests
```

### Configuration

#### Environment Variable: `HLS_PATH`

This is the **single user-configurable path** for where HLS segments are stored or staged. It is NOT hardcoded anywhere — the user defines it in their docker-compose.yml:

```yaml
environment:
  - HLS_PATH=/dev/shm # RAM disk (fast, recommended)
  - HLS_PATH=/mnt/ramdisk/hls # Custom RAM disk
  - HLS_PATH=/data/hls # Regular disk
  # If not set: HLS output is disabled (no default assumed)
```

How the module reads it:

```python
def get_hls_path() -> str:
    """Get the HLS output path from environment.

    The path is configured via HLS_PATH environment variable
    in docker-compose.yml. Supports any writable path:
    - /dev/shm (RAM disk - recommended)
    - Custom tmpfs mounts
    - Regular disk paths
    - Network mounts

    Returns empty string if not configured (HLS output disabled).
    """
    return os.environ.get("HLS_PATH", "")
```

**No default path is assumed.** If the user does not set `HLS_PATH`, HLS output functionality is simply not available. This prevents any accidental writes to container internal storage.

#### Settings via CoreSettings (UI-configurable)

These settings are stored in the database and configurable via the Settings UI:

```python
HLS_OUTPUT_SETTINGS_KEY = "hls_output_settings"

# Default settings dict
{
    "storage_backend": "filesystem",   # "filesystem" or "redis"
    "segment_duration": 6,             # Seconds per segment
    "playlist_size": 10,               # Segments in playlist
    "shutdown_delay": 30,              # Seconds after last client disconnects
    "ll_hls_enabled": False,           # Low-latency HLS
    "use_fmp4_segments": False,        # fMP4 instead of MPEG-TS segments
    "redis_segment_ttl": 120,          # TTL for segments in Redis (seconds)
}
```

#### How Storage Backend Works with HLS_PATH

| Backend      | HLS_PATH Usage                                                                                                          |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `filesystem` | FFmpeg writes directly to `HLS_PATH/{uuid}/`. Clients served from filesystem. Simplest setup.                           |
| `redis`      | FFmpeg writes to `HLS_PATH/{uuid}/` as staging. Watcher pushes to Redis, then deletes files. Clients served from Redis. |

In both cases, `HLS_PATH` is where FFmpeg writes. The difference is how clients are served.

---

## 3. Backend Implementation Plan

### 3.1 Storage Backends

#### `storage/base.py` — Abstract Interface

```python
from abc import ABC, abstractmethod
from typing import Optional

class SegmentStore(ABC):
    @abstractmethod
    def store_playlist(self, channel_uuid: str, content: str) -> bool: ...

    @abstractmethod
    def get_playlist(self, channel_uuid: str) -> Optional[str]: ...

    @abstractmethod
    def store_segment(self, channel_uuid: str, name: str, data: bytes) -> bool: ...

    @abstractmethod
    def get_segment(self, channel_uuid: str, name: str) -> Optional[bytes]: ...

    @abstractmethod
    def cleanup_channel(self, channel_uuid: str): ...
```

#### `storage/filesystem_store.py` — Direct Filesystem

- FFmpeg writes directly to `{HLS_PATH}/{uuid}/index.m3u8`
- Views serve files with Django `FileResponse`
- Cleanup: `shutil.rmtree` on session stop
- **No watcher needed** — FFmpeg writes, views read, simple

#### `storage/redis_store.py` — Redis with TTL

- FFmpeg writes to `{HLS_PATH}/{uuid}/` (staging)
- Watcher reads files, pushes to Redis with `SETEX`, deletes files
- Views read from Redis
- Cleanup: Redis TTL auto-expiry + staging dir cleanup
- Keys: `hls_output:{uuid}:playlist`, `hls_output:{uuid}:seg:{name}`

### 3.2 HLS Output Manager (`manager.py`)

Singleton that manages all HLS sessions. Patterns taken from your current `apps/output/hls/manager.py` but adapted:

**What is reused from your current implementation:**

- FFmpeg process lifecycle management (start, stop, monitor)
- FFmpeg stderr parsing for stream info (codec, resolution, fps, bitrate)
- Automatic stream switching on FFmpeg crash
- Redis-based multi-worker ownership coordination
- Redis metadata storage for stream stats

**What changes:**

- No `profile_type` or `build_command()` changes to StreamProfile
- FFmpeg command built internally by the manager (not via StreamProfile)
- Output path from `HLS_PATH` env var (not database/UI)
- Storage backend abstraction layer
- No modifications to Channel model (`get_hls_stream_profile()` removed)

### 3.3 FFmpeg Command Building

The manager builds FFmpeg commands internally using HLS settings from CoreSettings:

```python
def _build_ffmpeg_command(self, stream_url, user_agent, output_path, settings):
    segment_duration = settings.get("segment_duration", 6)
    playlist_size = settings.get("playlist_size", 10)
    use_fmp4 = settings.get("use_fmp4_segments", False)
    ll_hls = settings.get("ll_hls_enabled", False)

    segment_ext = "m4s" if use_fmp4 or ll_hls else "ts"

    hls_flags = "append_list+omit_endlist+program_date_time"
    if ll_hls:
        hls_flags += "+independent_segments"

    cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-user_agent", user_agent,
        "-i", stream_url,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", str(segment_duration),
        "-hls_list_size", str(playlist_size),
        "-hls_flags", hls_flags,
        "-hls_segment_filename", os.path.join(output_path, f"index%d.{segment_ext}"),
        os.path.join(output_path, "index.m3u8"),
    ]

    if use_fmp4 or ll_hls:
        # Insert fMP4 options before output
        fmp4_opts = ["-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4"]
        cmd = cmd[:-1] + fmp4_opts + cmd[-1:]

    return cmd
```

**This is all internal to the HLS output module** — no changes to StreamProfile's `build_command()`.

### 3.4 Client Manager (`client_manager.py`)

**Directly reused from your current implementation** (`apps/output/hls/client_manager.py`):

- Redis-backed client tracking
- Client TTL with automatic cleanup
- WebSocket updates on client connect/disconnect
- Cooldown mechanism for preventing rapid restarts

Changes: Key prefix aligned with proxy convention: `hls_output:channel:{uuid}:*`

### 3.5 Views (`views.py`)

**Reused logic from your current implementation:**

- Master playlist endpoint (`playlist.m3u8`)
- Media playlist endpoint (`index.m3u8`)
- Segment endpoint
- Stream switching endpoints (`change_stream`, `next_stream`)
- Client tracking on segment requests

**Changes:**

- Segment serving goes through the SegmentStore abstraction (not direct file reads)
- Network access check uses upstream's `network_access_allowed()`
- No dependency on Channel model's `get_hls_stream_profile()`

### 3.6 URL Routing (`urls.py`)

**Identical to your current implementation:**

```python
urlpatterns = [
    path("change_stream/<str:channel_uuid>", views.change_stream, name="change_stream"),
    path("next_stream/<str:channel_uuid>", views.next_stream, name="next_stream"),
    re_path(r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/playlist\.m3u8$", views.hls_master_playlist),
    re_path(r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/index\.m3u8$", views.hls_media_playlist),
    re_path(r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>index[0-9]+\.(ts|m4s))$", views.hls_segment),
    re_path(r"^(?P<channel_uuid>[0-9a-fA-F\-]+)/(?P<segment_name>init\.mp4)$", views.hls_segment),
]
```

---

## 4. Frontend Implementation Plan

### 4.1 Overview: Minimal Changes to Existing Components

The goal is to add HLS output support with **minimal modifications to existing upstream frontend files**. Most HLS frontend code lives in new, self-contained files.

### 4.2 New Frontend Files (No Upstream Conflicts)

| File                                                               | Purpose                            | Reuse from Current                          |
| ------------------------------------------------------------------ | ---------------------------------- | ------------------------------------------- |
| `frontend/src/components/forms/settings/HlsOutputSettingsForm.jsx` | Settings UI for HLS output         | ~90% from your `HlsSettingsForm.jsx`        |
| `frontend/src/components/HLSPlayer.jsx`                            | Standalone HLS.js player component | ~80% from your `FloatingVideo.jsx` HLS code |

### 4.3 Modified Existing Files (Minimal, Additive Changes)

#### `frontend/src/pages/Settings.jsx`

**Change:** Add one accordion section for HLS Output Settings

```jsx
// ADD: One import
const HlsOutputSettingsForm = React.lazy(
  () => import("../components/forms/settings/HlsOutputSettingsForm.jsx"),
);

// ADD: One accordion item (inside existing accordion)
<AccordionItem value="hls-output-settings">
  <AccordionControl>HLS Output</AccordionControl>
  <AccordionPanel>
    <Suspense fallback={<Loader />}>
      <HlsOutputSettingsForm
        active={accordianValue === "hls-output-settings"}
      />
    </Suspense>
  </AccordionPanel>
</AccordionItem>;
```

**Impact:** ~8 lines added. Zero existing lines changed.

#### `frontend/src/components/FloatingVideo.jsx`

**Change:** Add HLS.js support alongside existing mpegts.js player

```jsx
// ADD: Import HLS.js
import Hls from "hls.js";

// ADD: Detect HLS URL and use appropriate player
// In the useEffect that initializes the player:
if (streamUrl?.includes(".m3u8")) {
  // Use HLS.js player
  initializeHLSPlayer();
} else {
  // Use existing mpegts.js player (unchanged)
  initializeMpegTsPlayer();
}
```

**Impact:** ~50 lines added for HLS.js initialization. Existing mpegts.js code untouched. The HLS.js initialization code reuses ~80% of your current implementation.

#### `frontend/src/store/settings.jsx`

**Change:** Add stream format preference

```jsx
// ADD: Stream format state
streamFormat: 'ts',  // 'ts' or 'hls'
setStreamFormat: (format) => set({ streamFormat: format }),
getStreamUrl: (baseUrl, channelUuid) => {
  const { streamFormat } = get();
  if (streamFormat === 'hls') {
    return `${baseUrl}/proxy/hls_output/${channelUuid}/playlist.m3u8`;
  }
  return `${baseUrl}/proxy/ts/stream/${channelUuid}`;
},
```

**Impact:** ~10 lines added. No existing lines changed.

#### `frontend/src/api.js`

**Change:** Add HLS settings API methods

```jsx
// ADD: Two methods
static async getHLSOutputSettings() {
  const response = await request(`${host}/api/core/hls-output-settings/`);
  return response;
}

static async updateHLSOutputSettings(settings) {
  const response = await request(`${host}/api/core/hls-output-settings/`, {
    method: 'POST',
    body: JSON.stringify(settings),
  });
  return response;
}
```

**Impact:** ~15 lines added. No existing lines changed.

#### `frontend/src/components/tables/ChannelsTable.jsx`

**Change:** Add HLS format toggle in the channel URL section

```jsx
// ADD: Toggle for TS/HLS format switch
// In the channel popover or URL display area:
<SegmentedControl
  size="xs"
  data={[
    { value: "ts", label: "MPEG-TS" },
    { value: "hls", label: "HLS" },
  ]}
  value={streamFormat}
  onChange={setStreamFormat}
/>
```

**Impact:** ~15 lines added in the URL display section. No existing display logic changed.

### 4.4 Frontend Files NOT Changed (Compared to Current Implementation)

These files that your current implementation modifies will **NOT** be changed in the new approach:

| File                      | Current Changes             | New Approach                          |
| ------------------------- | --------------------------- | ------------------------------------- |
| `StreamProfilesTable.jsx` | Added HLS badge             | Not needed — no `profile_type` field  |
| `StreamProfile.jsx`       | Added profile_type dropdown | Not needed — no `profile_type` field  |
| `ChannelTableStreams.jsx` | Added HLS preview           | Preview works via URL format toggle   |
| `StreamsTable.jsx`        | Added HLS preview           | Preview works via URL format toggle   |
| `useVideoStore.jsx`       | Added streamFormat          | State lives in settings store instead |

### 4.5 HDHR-HLS Integration

**New file:** `apps/hdhr/hls_urls.py` — Reused directly from your current implementation (~90% identical)

**Modified file:** `dispatcharr/urls.py` — Add one line:

```python
path("hdhr-hls/", include("apps.hdhr.hls_urls")),
```

**Modified file:** `apps/hdhr/api_views.py` — Add the three HLS HDHR view classes (additive, ~100 lines)

---

## 5. Migration Strategy

### Single Migration

One migration file: `core/migrations/00XX_hls_output_settings.py`

This migration creates:

1. The `hls_output_settings` CoreSettings entry with default values
2. Nothing else — no model field changes, no new profiles

```python
def create_hls_output_settings(apps, schema_editor):
    CoreSettings = apps.get_model("core", "CoreSettings")
    CoreSettings.objects.get_or_create(
        key="hls_output_settings",
        defaults={
            "name": "HLS Output Settings",
            "value": {
                "storage_backend": "filesystem",
                "segment_duration": 6,
                "playlist_size": 10,
                "shutdown_delay": 30,
                "ll_hls_enabled": False,
                "use_fmp4_segments": False,
                "redis_segment_ttl": 120,
            }
        }
    )
```

**Migration number:** Will be `00XX` where XX is the next available number in upstream's sequence. Since the module is self-contained, this migration has zero conflict risk with upstream migrations.

---

## 6. File-by-File Change List

### New Files (Zero Upstream Conflict Risk)

| File                                                               | Lines | Purpose                     |
| ------------------------------------------------------------------ | ----- | --------------------------- |
| `apps/proxy/hls_output/__init__.py`                                | ~5    | Module marker               |
| `apps/proxy/hls_output/config.py`                                  | ~80   | Configuration class         |
| `apps/proxy/hls_output/manager.py`                                 | ~500  | FFmpeg process management   |
| `apps/proxy/hls_output/session.py`                                 | ~400  | Per-channel session         |
| `apps/proxy/hls_output/storage/__init__.py`                        | ~5    | Package marker              |
| `apps/proxy/hls_output/storage/base.py`                            | ~40   | Abstract SegmentStore       |
| `apps/proxy/hls_output/storage/redis_store.py`                     | ~80   | Redis backend               |
| `apps/proxy/hls_output/storage/filesystem_store.py`                | ~80   | Filesystem backend          |
| `apps/proxy/hls_output/watcher.py`                                 | ~100  | File watcher for Redis mode |
| `apps/proxy/hls_output/client_manager.py`                          | ~300  | Client tracking             |
| `apps/proxy/hls_output/views.py`                                   | ~300  | HTTP endpoints              |
| `apps/proxy/hls_output/urls.py`                                    | ~30   | URL routing                 |
| `apps/proxy/hls_output/tests.py`                                   | ~200  | Tests                       |
| `apps/hdhr/hls_urls.py`                                            | ~25   | HDHR-HLS URL patterns       |
| `frontend/src/components/forms/settings/HlsOutputSettingsForm.jsx` | ~200  | Settings form               |
| `frontend/src/components/HLSPlayer.jsx`                            | ~100  | HLS.js player component     |

**Total new files: 16, ~2,445 lines**

### Modified Upstream Files (Minimal, Additive Only)

| File                                               | Change Type                       | Lines Added | Lines Modified |
| -------------------------------------------------- | --------------------------------- | ----------- | -------------- |
| `core/models.py`                                   | Add settings key + helper methods | ~20         | 0              |
| `core/api_urls.py`                                 | Add HLS settings viewset route    | ~2          | 0              |
| `core/api_views.py`                                | Add HLS settings viewset class    | ~40         | 0              |
| `core/serializers.py`                              | Add HLS settings serializer       | ~25         | 0              |
| `core/migrations/00XX_*.py`                        | New migration file                | ~30         | 0              |
| `apps/proxy/urls.py`                               | Add HLS output URL include        | ~1          | 0              |
| `dispatcharr/urls.py`                              | Add HDHR-HLS URL pattern          | ~2          | 0              |
| `apps/hdhr/api_views.py`                           | Add 3 HLS HDHR view classes       | ~100        | 0              |
| `docker/docker-compose.yml`                        | Add HLS_PATH comment              | ~5          | 0              |
| `frontend/src/pages/Settings.jsx`                  | Add HLS accordion section         | ~8          | 0              |
| `frontend/src/components/FloatingVideo.jsx`        | Add HLS.js player support         | ~50         | 0              |
| `frontend/src/store/settings.jsx`                  | Add stream format state           | ~10         | 0              |
| `frontend/src/api.js`                              | Add HLS API methods               | ~15         | 0              |
| `frontend/src/components/tables/ChannelsTable.jsx` | Add format toggle                 | ~15         | 0              |
| `requirements.txt` or `package.json`               | Add hls.js dependency             | ~1          | 0              |

**Total modifications to upstream files: ~325 lines added, 0 lines modified**

### Files NOT Touched (Compared to Current Implementation)

| File                                                     | Why Not Touched                                       |
| -------------------------------------------------------- | ----------------------------------------------------- |
| `core/models.py` StreamProfile class                     | No `profile_type` field, no `build_command()` changes |
| `apps/channels/models.py`                                | No `get_hls_stream_profile()` method                  |
| `apps/output/apps.py`                                    | No HLS initialization in output app                   |
| `apps/output/views.py`                                   | No `format=hls` parameter                             |
| `apps/proxy/ts_proxy/views.py`                           | No HLS channel tracking                               |
| `apps/proxy/ts_proxy/client_manager.py`                  | No HLS channel tracking                               |
| `core/tasks.py`                                          | No HLS channel tracking                               |
| `frontend/src/components/forms/StreamProfile.jsx`        | No profile_type UI                                    |
| `frontend/src/components/tables/StreamProfilesTable.jsx` | No HLS badge                                          |
| `frontend/src/components/tables/StreamsTable.jsx`        | No HLS preview changes                                |
| `frontend/src/components/tables/ChannelTableStreams.jsx` | No HLS preview changes                                |
| `frontend/src/store/useVideoStore.jsx`                   | No streamFormat in video store                        |

---

## 7. What Changes vs. Current Implementation

### Removed Concepts

| Concept                                       | Why Removed                                                             |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| `profile_type` field on StreamProfile         | Upstream doesn't have it. FFmpeg command built internally by HLS module |
| `HLS Proxy` and `HLS FFmpeg` locked profiles  | Not needed. Module builds its own commands                              |
| `build_command()` HLS extensions              | Upstream signature untouched                                            |
| `get_hls_stream_profile()` on Channel         | Not needed with internal command building                               |
| `HLS_OUTPUT_SETTINGS_KEY` as separate concept | Integrated into CoreSettings group pattern                              |
| `HLS_OUTPUT_PATH` environment variable        | Renamed to `HLS_PATH` (clearer)                                         |
| 15 HLS core migrations                        | Collapsed to 1 migration                                                |
| `apps/output/hls/` directory                  | Moved to `apps/proxy/hls_output/`                                       |

### Preserved Concepts (Reused Code)

| Concept                                        | Reuse Level                          |
| ---------------------------------------------- | ------------------------------------ |
| FFmpeg process management and monitoring       | ~80%                                 |
| FFmpeg stderr parsing (codec, resolution, fps) | ~90%                                 |
| Redis-backed client tracking                   | ~85%                                 |
| Client cooldown mechanism                      | ~90%                                 |
| Automatic stream switching                     | ~70%                                 |
| Multi-worker ownership via Redis               | ~80%                                 |
| HLS.js web player integration                  | ~80%                                 |
| HLS Settings Form UI                           | ~90%                                 |
| HDHR-HLS endpoints                             | ~90%                                 |
| Master/media playlist serving                  | ~70%                                 |
| Segment serving                                | ~60% (now goes through SegmentStore) |
| fMP4/LL-HLS support                            | ~90%                                 |

### New Concepts

| Concept                  | Purpose                                              |
| ------------------------ | ---------------------------------------------------- |
| SegmentStore abstraction | Pluggable storage backends                           |
| RedisSegmentStore        | Redis-backed segment storage with TTL                |
| FilesystemSegmentStore   | Direct filesystem serving                            |
| FileWatcher              | Pushes segments from filesystem to Redis             |
| `HLS_PATH` env var       | User-configurable segment path (no defaults assumed) |
| Single migration         | Clean database setup                                 |

---

## 8. Step-by-Step Execution Order

### Phase 1: Fork Reset

- [ ] Create backup branch `hls-backup-2026-01-17` from current dev
- [ ] Tag current state as `hls-work-backup`
- [ ] Push backup branch to origin
- [ ] Add upstream remote
- [ ] Fetch upstream
- [ ] Reset dev branch to upstream/dev
- [ ] Force push to origin/dev
- [ ] Create `feature/hls-output` branch from clean dev

### Phase 2: Core Backend — Storage Backends

- [ ] Create `apps/proxy/hls_output/` directory structure
- [ ] Implement `storage/base.py` — SegmentStore interface
- [ ] Implement `storage/filesystem_store.py` — direct filesystem backend
- [ ] Implement `storage/redis_store.py` — Redis backend with TTL
- [ ] Implement `watcher.py` — file watcher for Redis mode
- [ ] Write tests for both storage backends

### Phase 3: Core Backend — Session Management

- [ ] Implement `config.py` — HLSOutputConfig with HLS_PATH env var support
- [ ] Implement `session.py` — HLSSession with FFmpeg process management
- [ ] Implement `manager.py` — HLSOutputManager singleton
- [ ] Port FFmpeg stderr parsing from current implementation
- [ ] Port automatic stream switching logic
- [ ] Port Redis-based multi-worker ownership
- [ ] Write tests for session management

### Phase 4: Core Backend — Client Tracking and Views

- [ ] Implement `client_manager.py` — port from current with key prefix alignment
- [ ] Implement `views.py` — playlist and segment serving via SegmentStore
- [ ] Implement `urls.py` — URL routing
- [ ] Add stream switching endpoints (change_stream, next_stream)
- [ ] Write tests for views

### Phase 5: Integration Points — Backend

- [ ] Add `HLS_OUTPUT_SETTINGS_KEY` and helper methods to `core/models.py`
- [ ] Add `HLSOutputSettingsSerializer` to `core/serializers.py`
- [ ] Add `HLSOutputSettingsViewSet` to `core/api_views.py`
- [ ] Register viewset in `core/api_urls.py`
- [ ] Add URL include in `apps/proxy/urls.py`
- [ ] Create migration `core/migrations/00XX_hls_output_settings.py`
- [ ] Add HLS_PATH documentation to `docker/docker-compose.yml`

### Phase 6: HDHR-HLS Integration

- [ ] Create `apps/hdhr/hls_urls.py`
- [ ] Add HLS HDHR view classes to `apps/hdhr/api_views.py`
- [ ] Add URL pattern to `dispatcharr/urls.py`

### Phase 7: Frontend

- [ ] Add `hls.js` dependency to `package.json`
- [ ] Create `HlsOutputSettingsForm.jsx` settings component
- [ ] Create `HLSPlayer.jsx` standalone player component
- [ ] Add HLS accordion section to `Settings.jsx`
- [ ] Add HLS.js support to `FloatingVideo.jsx`
- [ ] Add stream format state to `store/settings.jsx`
- [ ] Add HLS API methods to `api.js`
- [ ] Add format toggle to `ChannelsTable.jsx`

### Phase 8: Testing and Documentation

- [ ] Run all existing upstream tests (ensure nothing breaks)
- [ ] Run HLS-specific tests
- [ ] Test filesystem backend with various paths
- [ ] Test Redis backend with watcher
- [ ] Test fMP4 and LL-HLS modes
- [ ] Test HDHR-HLS integration
- [ ] Test frontend HLS playback
- [ ] Test stream switching
- [ ] Update README with HLS output documentation

### Phase 9: Upstream PR Preparation

- [ ] Review all changes against upstream coding standards
- [ ] Ensure commit messages follow upstream format
- [ ] Squash into logical commits
- [ ] Open issue referencing #1003 describing the implementation
- [ ] Submit PR with comprehensive description
