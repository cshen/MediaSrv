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

iPadOS Safari can only decode a limited set of video codecs inside MP4
(H.264/HEVC); **VP9, AV1, etc. will not play**, even though desktop Safari may
play them. MediaSrv probes each video with `ffprobe` at scan time and, for any
non-web-safe codec, transcodes it on demand to **H.264 + AAC (faststart)** in
`data/transcoded/`. The original file is never modified.

- Transcoding starts automatically when the page loads (it warms the library)
  and when you tap such a video; the player shows an "Optimizing video… %"
  overlay until it's ready.
- Only one video is transcoded at a time. On Apple Silicon it uses the
  hardware encoder (`h264_videotoolbox`) and is usually much faster than
  real time.
- The result is cached, so it only happens once per file.
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

The library is cached in `data/library.json` and refreshed automatically when a
file changes.

## API (for reference)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Resolved player defaults + library roots |
| `GET` | `/api/tracks` | All tracks with metadata + favorite flag |
| `GET` | `/api/favorites` | Favorite track ids |
| `POST` | `/api/favorites/{id}` | Toggle a favorite |
| `POST` | `/api/rescan` | Re-scan the media folders |
| `GET` | `/api/cover/{id}` | Embedded album art |
| `GET` | `/media/{id}` | Stream a track (supports `Range`) |
