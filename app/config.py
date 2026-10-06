"""Runtime configuration for MediaSrv.

Values are resolved in this order (later wins):

1. built-in defaults
2. a ``config.toml`` file
3. environment variables (``MEDIASRV_*``)

Looked up config file locations, first match wins:

1. ``$MEDIASRV_CONFIG`` (explicit path)
2. ``<project>/config.toml`` (same dir as the code)
3. ``~/.config/MediaSrv/config.toml`` (or ``mediasrv``)
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path.home() / ".cache" / "MediaSrv" / "data"

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 9000,
        "data_dir": str(DEFAULT_DATA_DIR),
    },
    "media": {
        # Folder roots. Audio/video are combined for scanning; the split is
        # just a convenient way to describe your library.
        "audio": ["~/Music"],
        "video": ["~/Movies"],
        "paths": [],
        "follow_symlinks": False,
        "ignore_hidden": True,
        # Path fragments / globs to skip, e.g. ["/Volumes/olddrive", "*_backup*"].
        "exclude": [],
        # Skip a directory that does not respond within this many seconds
        # (frozen mount / TCC-protected app library). 0 disables the guard.
        "directory_timeout": 10,
        "audio_extensions": [".mp3"],
        "video_extensions": [".mp4", ".m4v", ".mov", ".mkv", ".webm"],
    },
    "player": {
        # Sent to the web UI as starting defaults (localStorage still wins).
        "volume": 0.8,
        "shuffle": False,
        "repeat": "off",  # off | all | one
        "skip_seconds": 10,
        "theme": "dark",
    },
    "transcode": {
        # Browsers (especially iPadOS Safari) cannot play every codec -- e.g.
        # VP9/AV1 in MP4. Non-web-safe videos are transcoded to H.264 + AAC.
        "enabled": True,
        "encoder": "auto",  # auto | videotoolbox | libx264
        "video_bitrate": "5M",  # used by the hardware encoder
        "crf": 23,  # used by libx264
        "preset": "veryfast",  # used by libx264
        "audio_bitrate": "160k",
        "segment_seconds": 30,  # resume granularity (shorter = finer resume)
    },
    "service": {
        # Used by deploy/install-launchd.sh to render the launchd plist.
        "label": "com.mediasrv",
        "user": "",  # empty -> the user running the installer
        "log_file": "",  # empty -> ~/Library/Logs/MediaSrv.log
        "path": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    explicit = os.environ.get("MEDIASRV_CONFIG")
    if explicit:
        files.append(Path(explicit).expanduser())
    files.append(BASE_DIR / "config.toml")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    files.append(config_home / "MediaSrv" / "config.toml")
    files.append(config_home / "mediasrv" / "config.toml")
    return files


def _load_file() -> tuple[dict, Path | None]:
    for path in _candidate_files():
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            print(f"[MediaSrv] invalid config {path}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if not isinstance(data, dict):
            continue
        return data, path
    return {}, None


_raw, CONFIG_PATH = _load_file()
CONFIG = _deep_merge(DEFAULTS, _raw)


def _config_base() -> Path:
    """Directory used to resolve relative media paths."""
    return CONFIG_PATH.parent if CONFIG_PATH else BASE_DIR


# ---------------------------------------------------------------- server
host = os.environ.get("MEDIASRV_HOST") or str(CONFIG["server"].get("host") or "0.0.0.0")
try:
    port = int(os.environ.get("MEDIASRV_PORT") or CONFIG["server"].get("port") or 9000)
except (TypeError, ValueError):
    port = 9000

HOST = host
PORT = port

_DATA_ENV = os.environ.get("MEDIASRV_DATA")
DATA_DIR = Path(_DATA_ENV).expanduser() if _DATA_ENV else Path(str(CONFIG["server"].get("data_dir") or (BASE_DIR / "data"))).expanduser()
COVER_DIR = DATA_DIR / "covers"
TRANSCODE_DIR = DATA_DIR / "transcoded"
LIBRARY_CACHE = DATA_DIR / "library.json"
FAVORITES_FILE = DATA_DIR / "favorites.json"


# ---------------------------------------------------------------- media
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _resolve_roots() -> list[Path]:
    media = CONFIG["media"]
    base = _config_base()

    env_media = os.environ.get("MEDIASRV_MEDIA")
    if env_media:
        raw = [p for p in env_media.replace(",", ":").split(":") if p.strip()]
    else:
        raw = _as_list(media.get("audio")) + _as_list(media.get("video")) + _as_list(media.get("paths"))

    if not raw:
        raw = [str(p) for p in (Path.home() / "Media", Path.home() / "Music", Path.home() / "Movies") if p.exists()]
        if not raw:
            raw = [str(Path.home() / "Media")]

    dirs: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        key = str(path)
        if key not in seen:
            seen.add(key)
            dirs.append(path)
    return dirs


_MEDIA_DIRS = _resolve_roots()

FOLLOW_SYMLINKS = bool(CONFIG["media"].get("follow_symlinks", False))
IGNORE_HIDDEN = bool(CONFIG["media"].get("ignore_hidden", True))
EXCLUDE = [str(x) for x in _as_list(CONFIG["media"].get("exclude"))]
DIRECTORY_TIMEOUT = float(CONFIG["media"].get("directory_timeout", 10) or 0)

AUDIO_EXTS = {e.lower() if e.startswith(".") else "." + e.lower() for e in _as_list(CONFIG["media"].get("audio_extensions"))}
VIDEO_EXTS = {e.lower() if e.startswith(".") else "." + e.lower() for e in _as_list(CONFIG["media"].get("video_extensions"))}
if not AUDIO_EXTS:
    AUDIO_EXTS = {".mp3"}
if not VIDEO_EXTS:
    VIDEO_EXTS = {".mp4", ".m4v", ".mov"}


def media_dirs() -> list[Path]:
    """Resolve the list of folders to scan for media."""
    return list(_MEDIA_DIRS)


PLAYER = {
    "volume": max(0.0, min(1.0, float(CONFIG["player"].get("volume", 0.8)))),
    "shuffle": bool(CONFIG["player"].get("shuffle", False)),
    "repeat": str(CONFIG["player"].get("repeat", "off")),
    "skip_seconds": max(1, int(CONFIG["player"].get("skip_seconds", 10))),
    "theme": str(CONFIG["player"].get("theme", "dark")),
}

TRANSCODE_ENABLED = bool(CONFIG["transcode"].get("enabled", True))
TRANSCODE_ENCODER = str(CONFIG["transcode"].get("encoder", "auto")).lower()
TRANSCODE_VIDEO_BITRATE = str(CONFIG["transcode"].get("video_bitrate", "5M"))
TRANSCODE_CRF = int(CONFIG["transcode"].get("crf", 23))
TRANSCODE_PRESET = str(CONFIG["transcode"].get("preset", "veryfast"))
TRANSCODE_AUDIO_BITRATE = str(CONFIG["transcode"].get("audio_bitrate", "160k"))
TRANSCODE_SEGMENT_SECONDS = max(1, int(CONFIG["transcode"].get("segment_seconds", 30)))

SERVICE = {
    "label": str(CONFIG["service"].get("label") or "com.mediasrv"),
    "user": str(CONFIG["service"].get("user") or ""),
    "log_file": str(CONFIG["service"].get("log_file") or ""),
    "path": str(
        CONFIG["service"].get("path")
        or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    ),
}


def public() -> dict:
    """Client-facing subset of the configuration."""
    return {
        "player": PLAYER,
        "library": {
            "roots": [str(p) for p in _MEDIA_DIRS],
            "audio_extensions": sorted(AUDIO_EXTS),
            "video_extensions": sorted(VIDEO_EXTS),
        },
        "config_path": str(CONFIG_PATH) if CONFIG_PATH else None,
        "transcode": {"enabled": TRANSCODE_ENABLED},
    }


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
