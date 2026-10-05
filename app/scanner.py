"""Scan a folder tree for playable audio/video and read their metadata.

The scan result is cached to ``data/library.json`` so the server starts fast.
Embedded cover art is extracted once into ``data/covers/<id>.<ext>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from . import config, transcode

try:  # mutagen is optional at import time; degrade gracefully if missing.
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC  # noqa: F401
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
except Exception:  # pragma: no cover
    MutagenFile = None  # type: ignore
    ID3 = None  # type: ignore
    MP4 = None  # type: ignore


def track_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def _first(value) -> str:
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    return str(value) if value else ""


def _read_tags(path: Path) -> dict:
    """Return title/artist/album/duration for a file, with filename fallbacks."""
    info = {
        "title": path.stem,
        "artist": "",
        "album": "",
        "duration": 0.0,
    }
    if MutagenFile is None:
        return info

    try:
        audio = MutagenFile(path)
    except Exception:
        return info

    if audio is None:
        return info

    if getattr(audio, "info", None) is not None:
        info["duration"] = round(float(getattr(audio.info, "length", 0.0) or 0.0), 2)

    tags = getattr(audio, "tags", None)
    if tags:
        # Easy/ID3 and MP4 all expose a dict-like interface.
        for key in ("title", "TIT2", "\xa9nam"):
            if key in tags:
                info["title"] = _first(tags[key]) or info["title"]
                break
        for key in ("artist", "TPE1", "\xa9ART", "aART"):
            if key in tags:
                info["artist"] = _first(tags[key])
                break
        for key in ("album", "TALB", "\xa9alb"):
            if key in tags:
                info["album"] = _first(tags[key])
                break
    return info


def _extract_cover(path: Path, tid: str) -> str | None:
    """Extract embedded artwork to the cover cache; return the filename."""
    if MutagenFile is None:
        return None
    try:
        audio = MutagenFile(path)
    except Exception:
        return None
    if audio is None:
        return None

    data: bytes | None = None
    mime = "image/jpeg"
    tags = getattr(audio, "tags", None)
    try:
        if ID3 is not None and isinstance(tags, ID3):
            apic = tags.getall("APIC")
            if apic:
                data = apic[0].data
                mime = apic[0].mime or mime
        elif MP4 is not None and isinstance(audio, MP4):
            covers = tags.get("covr") if tags else None
            if covers:
                data = bytes(covers[0])
                fmt = getattr(covers[0], "imageformat", None)
                if fmt == 14:
                    mime = "image/png"
    except Exception:
        data = None

    if not data:
        return None

    ext = ".png" if "png" in mime else ".jpg"
    target = config.COVER_DIR / f"{tid}{ext}"
    if not target.exists():
        try:
            target.write_bytes(data)
        except Exception:
            return None
    return target.name


def _iter_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=config.FOLLOW_SYMLINKS):
        if config.IGNORE_HIDDEN:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            filenames = [f for f in filenames if not f.startswith(".")]
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan() -> list[dict]:
    """Walk the configured media roots and build the library list."""
    config.ensure_dirs()
    tracks: list[dict] = []
    seen: set[str] = set()

    for root in config.media_dirs():
        if not root.exists():
            continue
        for path in _iter_files(root):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in config.AUDIO_EXTS:
                kind = "audio"
            elif suffix in config.VIDEO_EXTS:
                kind = "video"
            else:
                continue

            tid = track_id(path)
            if tid in seen:
                continue
            seen.add(tid)

            try:
                stat = path.stat()
            except OSError:
                continue

            meta = _read_tags(path)
            cover = _extract_cover(path, tid)

            codec = ""
            web_safe = True
            if kind == "video":
                codec = transcode.probe_codec(path) or ""
                web_safe = transcode.is_web_safe(codec)

            tracks.append(
                {
                    "id": tid,
                    "title": meta["title"],
                    "artist": meta["artist"],
                    "album": meta["album"],
                    "duration": meta["duration"],
                    "type": kind,
                    "ext": suffix.lstrip("."),
                    "codec": codec,
                    "web_safe": web_safe,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "has_cover": cover is not None,
                    "path": str(path),
                }
            )

    tracks.sort(key=lambda t: (t["artist"].lower(), t["album"].lower(), t["title"].lower()))
    save_cache(tracks)
    return tracks


def _cache_signature() -> dict:
    return {
        "schema": 2,
        "roots": [str(p) for p in config.media_dirs()],
        "audio_extensions": sorted(config.AUDIO_EXTS),
        "video_extensions": sorted(config.VIDEO_EXTS),
        "follow_symlinks": config.FOLLOW_SYMLINKS,
        "ignore_hidden": config.IGNORE_HIDDEN,
    }


def save_cache(tracks: list[dict]) -> None:
    config.ensure_dirs()
    payload = {"generated": time.time(), "signature": _cache_signature(), "tracks": tracks}
    config.LIBRARY_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _scan_paths() -> set[str]:
    """Cheaply list playable files currently on disk (no tag reading)."""
    found: set[str] = set()
    for root in config.media_dirs():
        if not root.exists():
            continue
        for path in _iter_files(root):
            suffix = path.suffix.lower()
            if suffix in config.AUDIO_EXTS or suffix in config.VIDEO_EXTS:
                found.add(str(path))
    return found


def load_cache() -> list[dict] | None:
    """Load a cached library, refreshing entries whose file changed/disappeared."""
    if not config.LIBRARY_CACHE.exists():
        return None
    try:
        payload = json.loads(config.LIBRARY_CACHE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

    # A change to the media config invalidates the cache.
    if payload.get("signature") != _cache_signature():
        return None

    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return None

    # Detect added/removed files with a lightweight directory walk.
    cached_paths = {str(t.get("path", "")) for t in tracks}
    disk_paths = _scan_paths()
    if cached_paths != disk_paths:
        return None

    fresh: list[dict] = []
    for t in tracks:
        path = Path(t.get("path", ""))
        try:
            stat = path.stat()
        except OSError:
            continue  # file removed since cache
        if abs(stat.st_mtime - float(t.get("mtime", 0))) > 0.5 or stat.st_size != t.get("size"):
            return None  # something changed -> caller should rescan
        fresh.append(t)
    return fresh
