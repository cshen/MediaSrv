# MediaSrv

A tiny home media server for the Mac mini. Point it at a folder of music/videos
and play them in Safari on your iPad — audio, video, playlists, favorites and
shuffle, all from one clean web page.

## Features

- Plays **MP3** and **MP4/M4V/MOV** directly in the browser.
- Now-playing art with a blurred ambient backdrop.
- **Rewind / forward 10s**, previous / next, play/pause.
- **Draggable progress bar** (real HTTP Range streaming, so video seeking works in Safari).
- **Playlist** generated from the library, grouped/searchable.
- **Favorites** (persisted on the server) and a Favorites-only view.
- **Shuffle** and repeat modes (off / all / one).
- **Fullscreen** and **Picture-in-Picture** for video (the PiP window is freely resizable).
- Media Session support: lock-screen / Control Center controls on iPad.
- Automatic **transcoding** of non-web-safe videos (see below).
- Responsive touch UI.

## Requirements

- macOS with [uv](https://docs.astral.sh/uv/) installed.
- [ffmpeg](https://ffmpeg.org/) (provides `ffmpeg`/`ffprobe`) — needed only to
  transcode videos that browsers can't play natively. `brew install ffmpeg`.

## Setup

```sh
uv sync
```

## Run

```sh
# Uses config.toml if present, else scans ~/Music and ~/Movies (whichever exist)
uv run mediasrv

# or point at a folder just for this run
MEDIASRV_MEDIA="$HOME/Media" uv run mediasrv
```

`uv run mediasrv` is a shortcut for:

```sh
uv run uvicorn app.main:app --host 0.0.0.0 --port 9000
```

### Configuration

Configuration is resolved in this order (later wins):

1. built-in defaults
2. a `config.toml` file (see below)
3. `MEDIASRV_*` environment variables

`config.toml` is looked up in these locations, first match wins:

1. the path in `$MEDIASRV_CONFIG`
2. `./config.toml` (same dir as the code)
3. `~/.config/MediaSrv/config.toml` (also accepts `~/.config/mediasrv/`)

Copy the annotated [`config.example.toml`](config.example.toml) to get started:

```sh
cp config.example.toml config.toml
```

```toml
[server]
host = "0.0.0.0"
port = 9000
# data_dir = "~/.cache/MediaSrv/data"

[media]
audio = ["~/Music"]           # where your MP3s live
video = ["~/Movies"]          # where your MP4s live
paths = []                    # extra mixed folders
follow_symlinks = false
ignore_hidden = true
audio_extensions = [".mp3"]
video_extensions = [".mp4", ".m4v", ".mov"]

[player]
volume = 0.8
shuffle = false
repeat = "off"                # off | all | one
skip_seconds = 10             # rewind / forward jump
theme = "dark"                # dark | light
```

Relative media paths are resolved against the config file. Multiple roots in
`audio` + `video` + `paths` are combined for scanning.

#### Environment variables (override the file)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEDIASRV_CONFIG` | – | Explicit path to `config.toml`. |
| `MEDIASRV_MEDIA` | from config | Folder(s) to scan. Separate with `:` or `,`. |
| `MEDIASRV_HOST` | `0.0.0.0` | Bind address (all interfaces = reachable from iPad). |
| `MEDIASRV_PORT` | `9000` | Port. |
| `MEDIASRV_DATA` | `~/.cache/MediaSrv/data` | Where the library cache, covers, favorites and transcoded files live. |

## Video compatibility & transcoding

Browsers are picky about both the **container** and the **codecs**. Safari can
play a file as-is only when it is:

> container ∈ { **.mp4, .m4v, .mov** } **and** video = **H.264** **and**
> audio ∈ { **AAC, MP3** } (or no audio).

Everything else is converted to an MP4 with **H.264 + AAC (faststart)** in
`~/.cache/MediaSrv/data/transcoded/`. That includes:

- unsupported containers — **`.mkv`, `.webm`**, etc. (even if the codecs inside
  are H.264/AAC — Safari can't open Matroska at all),
- unsupported video — **VP9, AV1, HEVC, …**,
- unsupported audio — **DTS, AC-3, Opus, …**.

`ffprobe` inspects each file at scan time and picks the cheapest conversion:

| Situation | Action |
| --- | --- |
| MP4/MOV + H.264 + AAC/MP3 | play as-is (no work) |
| H.264 + AAC/MP3 in another container (e.g. `.mkv`) | **remux** — `-c copy`, instant & lossless |
| H.264 + other audio (DTS/AC-3/…) | copy video, **re-encode audio only** |
| other video codec (VP9/AV1/HEVC/…) | full **re-encode** (segmented, resumable) |

Notes:

- Transcoding starts when the page loads (warm-up) and when you tap such a
  video; the player shows "Optimizing video… %" until it's ready, then plays.
- Only one file is converted at a time; full re-encodes use the hardware encoder
  (`h264_videotoolbox`) on Apple Silicon and are usually faster than real time.
- Results are cached, so it happens once per file. Full re-encodes are
  **resumable** (segmented; tune with `segment_seconds`); if a file's duration
  can't be determined, it falls back to a single-pass encode.
- Default `video_extensions` is `[".mp4", ".m4v", ".mov", ".mkv", ".webm"]`.
- Check the cache size any time with **`./doctor.sh`** (reports the configured
  and default `transcoded/` folders and warns if any exceeds 20 GB).
- Disable/tune it in `config.toml` under `[transcode]`.

## Connect from the iPad

1. Make sure the iPad and Mac mini are on the same Wi-Fi network.
2. Find the Mac mini's local IP:

   ```sh
   ipconfig getifaddr en0      # Wi-Fi
   ipconfig getifaddr en1      # Ethernet (sometimes)
   ```

3. On the iPad open Safari to `http://<IP>:9000` (e.g. `http://192.168.1.20:9000`).
4. Tap share → **Add to Home Screen** for a full-screen app-like experience.

If macOS blocks incoming connections, allow it under **System Settings →
Network → Firewall**, or add the Python/uv binary to the allowed list.

## Adding / removing media

Drop new files into the media folder, then tap the browser reload — or hit the
rescan endpoint:

```sh
curl -X POST http://<IP>:9000/api/rescan
```

The library is cached in `~/.cache/MediaSrv/data/library.json` and refreshed
automatically when a file changes. Scanning always runs in the background, so
the server stays responsive; the web UI shows "Scanning your library… x/N" and
fills in automatically when it finishes.

## Troubleshooting

### "Scanning your library…" forever / the scan hangs

A directory that never answers a `readdir` (a stalled network/external volume,
or a folder macOS protects for background daemons) used to hang the whole scan,
so nothing was ever found.

MediaSrv now guards against this:

- **Per-directory timeout** — if listing a folder doesn't respond within
  `directory_timeout` seconds it is logged and skipped, and the scan continues:
  ```
  WARNING mediasrv.scanner: scan: skipping frozen directory /Users/cs/Music/Music (no response in 10s)
  ```
  Because a blocked `readdir` can't be cancelled, each frozen folder costs up to
  one timeout per scan (not forever).
- **`exclude` patterns** — skip known-bad folders instantly. Each entry is a
  glob matched against the full path and the folder/file name.

```toml
[media]
# narrow to what you actually want ...
audio = ["~/Music"]
video = ["~/Movies/Infuse", "~/Movies/Living"]
# ... and/or skip the problematic folders
exclude = ["*/Music/Music", "*/Movies/TV"]
directory_timeout = 5
```

Both options are also part of the cache signature, so changing them triggers a
rescan.

### Why some folders hang

On macOS, folders like `~/Music/Music` (Music app library) and `~/Movies/TV`
(TV app library) are protected. A system **LaunchDaemon** cannot be granted that
access interactively, so opening them can block indefinitely. Options:

- Exclude/narrow the roots (recommended for a daemon), or
- Grant **Full Disk Access** to
  `<project>/.venv/bin/python` in System Settings → Privacy & Security, or
- Run as a **LaunchAgent** instead (it inherits your user's permissions).

### Garbled / gibberish Chinese (or other CJK) tags

Old MP3 tags often store UTF-8 or GBK bytes but label them Latin-1, so titles
show up like `ÐíÃÀ¾²` or `å¤©ç¢`. The scanner repairs these (recovers the raw
bytes and re-decodes as UTF-8/GBK/Big5) and falls back to the ID3v1 tag when
the ID3v2 field is lossy. Characters that were already replaced with `?` inside
the file's tag cannot be recovered. Changing/upgrading this automatically
invalidates the library cache, so just restart and it re-reads the tags.

### Logs

The service logs go to `~/Library/Logs/MediaSrv.log`. Raise the level with
`MEDIASRV_LOG_LEVEL=DEBUG` to see each directory the scanner visits.

## Run automatically at boot (launchd)

A LaunchDaemon starts MediaSrv at boot (no login needed) and **restarts it
automatically if it is killed** (`RunAtLoad` + `KeepAlive`).

The plist is *generated* from `config.toml` — nothing user-specific is
hard-coded. Edit the `[service]` section (in `./config.toml` or
`~/.config/MediaSrv/config.toml`):

```toml
[service]
label = "com.mediasrv"        # launchd job label
user = ""                     # run as this user; empty = current user
log_file = ""                 # empty = ~/Library/Logs/MediaSrv.log
path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
```

Then render/install with the helper (it reads the same config search paths and
`MEDIASRV_*` env vars the app does):

```sh
./deploy/install-launchd.sh render      # preview the rendered plist
./deploy/install-launchd.sh install     # install + load it (asks for sudo)
./deploy/install-launchd.sh status      # launchd status
tail -f ~/Library/Logs/MediaSrv.log     # logs
```

Confirm it recovers from a kill, or restart it after pulling new code:

```sh
sudo pkill -f mediasrv                                    # it should come back
./deploy/install-launchd.sh install                       # re-render + reload
```

Uninstall:

```sh
./deploy/install-launchd.sh uninstall
```

> **No `sudo`? Use a LaunchAgent instead** — starts at login rather than boot.
> Render with `user = ""` and remove the `UserName` key from
> `deploy/com.mediasrv.plist.template`, then:
> ```sh
> ./deploy/install-launchd.sh write
> cp deploy/generated/*.plist ~/Library/LaunchAgents/
> launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mediasrv.plist
> ```

## API (for reference)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Resolved player defaults + library roots |
| `GET` | `/api/tracks` | All tracks with metadata + favorite flag |
| `GET` | `/api/favorites` | Favorite track ids |
| `POST` | `/api/favorites/{id}` | Toggle a favorite |
| `POST` | `/api/rescan` | Re-scan the media folders |
| `GET` | `/api/cover/{id}` | Embedded album art |
| `GET` | `/api/transcode/{id}` | Video transcode status / start |
| `GET` | `/media/{id}` | Stream a track (supports `Range`) |
