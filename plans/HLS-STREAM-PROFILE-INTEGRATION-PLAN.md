# HLS Output — Stream Profile Integration Plan

## Problem Statement

The current HLS output module builds FFmpeg commands internally in [`session.py:_build_ffmpeg_command()`](apps/proxy/hls_output/session.py:229), completely bypassing the Stream Profile system that Dispatcharr uses for TS output. This means:

1. Users cannot create custom FFmpeg profiles for HLS output
2. The HLS output ignores the channel's assigned stream profile
3. The HLS output doesn't participate in the profile-based connection management

The user expects HLS output to work **exactly like TS output** with respect to Stream Profiles — same profile creation UI, same parameter system, same `{streamUrl}`, `{userAgent}` placeholders.

## How TS Output Uses Stream Profiles Today

### The TS Profile Flow

1. **Profile Definition**: StreamProfile has `command` (e.g., `ffmpeg`) and `parameters` (e.g., `-user_agent {userAgent} -i {streamUrl} -c copy -f mpegts pipe:1`)
2. **`build_command()`**: Replaces `{streamUrl}` and `{userAgent}` placeholders, returns a command array
3. **TS Stream Manager**: Calls `stream_profile.build_command(url, user_agent)` to get the FFmpeg command
4. **Process Creation**: Runs the command with `subprocess.Popen`, reads from `stdout` (i.e., `pipe:1` output)

### Key TS Profiles (locked/built-in)

| Profile        | Command      | Parameters                                                                          |
| -------------- | ------------ | ----------------------------------------------------------------------------------- |
| **ffmpeg**     | `ffmpeg`     | `-user_agent {userAgent} -i {streamUrl} -c copy -f mpegts pipe:1`                   |
| **Proxy**      | _(empty)_    | _(empty)_ — direct proxy, no transcoding                                            |
| **Redirect**   | _(empty)_    | _(empty)_ — HTTP redirect                                                           |
| **streamlink** | `streamlink` | `{streamUrl} --http-header User-Agent={userAgent} best --stdout`                    |
| **VLC**        | `cvlc`       | `{streamUrl} --sout '#std{access=file,mux=ts,dst=-}' --http-user-agent={userAgent}` |

### The TS Pipe Pattern

TS output uses `pipe:1` (stdout) because the TS proxy reads the transcoded stream from stdout and pushes it to clients via HTTP. This is how FFmpeg outputs to stdout:

```
ffmpeg -i {streamUrl} -c copy -f mpegts pipe:1
```

## How HLS Output Differs From TS Output

HLS output writes to **files on disk** (segments + playlist), NOT to stdout. The FFmpeg output is an HLS muxer that creates:

- `index.m3u8` (playlist file)
- `index0.ts`, `index1.ts`, ... (segment files)

This means HLS output CANNOT use `pipe:1`. It needs an output path:

```
ffmpeg -i {streamUrl} -c copy -f hls -hls_time 6 -hls_list_size 10 ... /hls/{uuid}/index.m3u8
```

## Integration Approach: Add `{hlsOutputPath}` Placeholder

### Extend `build_command()` with HLS Support

The `StreamProfile.build_command()` method currently accepts `(stream_url, user_agent)`. We need to add an optional `hls_output_path` parameter:

```python
def build_command(self, stream_url, user_agent, hls_output_path=None):
    replacements = {
        "{streamUrl}": stream_url,
        "{userAgent}": user_agent,
    }
    if hls_output_path:
        replacements["{hlsOutputPath}"] = hls_output_path

    cmd = [self.command] + [
        self._replace_in_part(part, replacements)
        for part in shlex_split(self.parameters)
    ]
    return cmd
```

This is **fully backward-compatible** — existing TS profiles don't use `{hlsOutputPath}`, so they work unchanged.

### Create Default HLS FFmpeg Profile

Add a new locked profile via migration:

| Field          | Value                                                                                                                                                                                                                   |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **name**       | `HLS FFmpeg`                                                                                                                                                                                                            |
| **command**    | `ffmpeg`                                                                                                                                                                                                                |
| **parameters** | `-user_agent {userAgent} -i {streamUrl} -c copy -f hls -hls_time 6 -hls_list_size 10 -hls_flags append_list+omit_endlist+program_date_time -hls_segment_filename {hlsOutputPath}/index%d.ts {hlsOutputPath}/index.m3u8` |
| **locked**     | `True`                                                                                                                                                                                                                  |

Users can also create their own custom HLS profiles with different FFmpeg flags, transcoding options, etc.

### Modify HLS Session to Use StreamProfile

Instead of building commands internally, the HLS session should:

1. Get the channel's stream profile (or a default HLS profile)
2. Call `profile.build_command(stream_url, user_agent, hls_output_path=output_path)`
3. Run the resulting command

If the profile doesn't contain `{hlsOutputPath}` (i.e., it's a TS profile), fall back to the internal command builder.

### Modify HLS Manager to Get Stream Profile

The `get_direct_stream_url()` function needs to correctly use the Channel model's `get_stream_profile()` method and pass it to the session.

## Files to Modify

### Backend

| File                                                                       | Change                                                                                           |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [`core/models.py`](core/models.py:127)                                     | Add `hls_output_path` parameter to `build_command()` with `{hlsOutputPath}` replacement          |
| [`core/migrations/0023_add_hls_ffmpeg_profile.py`](core/migrations/)       | New migration: Create locked "HLS FFmpeg" profile                                                |
| [`apps/proxy/hls_output/manager.py`](apps/proxy/hls_output/manager.py:54)  | Fix `get_direct_stream_url()`: fix Stream attribute error, pass stream profile to session        |
| [`apps/proxy/hls_output/session.py`](apps/proxy/hls_output/session.py:100) | Accept StreamProfile, use `build_command()` with `{hlsOutputPath}`, fallback to internal builder |

### Frontend (minimal)

| File                                                                                | Change                                                   |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------- |
| [`StreamProfilesTable.jsx`](frontend/src/components/tables/StreamProfilesTable.jsx) | Show HLS badge for profiles containing `{hlsOutputPath}` |
| [`StreamProfile.jsx` or form](frontend/src/components/forms/StreamProfile.jsx)      | Add `{hlsOutputPath}` placeholder documentation hint     |

## Execution Steps

1. **Fix immediate crash**: Fix `get_direct_stream_url()` — `channel.streams.first()` returns `Stream` directly, not `ChannelStream`
2. **Extend `build_command()`**: Add optional `hls_output_path` parameter with `{hlsOutputPath}` placeholder
3. **Create HLS FFmpeg migration**: Add locked "HLS FFmpeg" profile with HLS-specific FFmpeg parameters
4. **Modify HLS manager**: Get the channel's stream profile and pass it to the session
5. **Modify HLS session**: Use stream profile's `build_command()` to generate the FFmpeg command, with fallback to internal builder for non-HLS profiles
6. **Frontend updates**: Show HLS profile hints, add `{hlsOutputPath}` to placeholder documentation
