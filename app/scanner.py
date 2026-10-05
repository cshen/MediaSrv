"""Scan a folder tree for playable audio/video and read their metadata.

The scan result is cached to ``library.json`` so the server starts fast.
Embedded cover art is extracted once into ``data/covers/<id>.<ext>``.

Scanning never blocks the web server: the API triggers it in a background
thread and reports a ``scanning`` flag while it runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path

from . import config, transcode

try:  # mutagen is optional at import time; degrade gracefully if missing.
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
except Exception:  # pragma: no cover
    MutagenFile = None  # type: ignore
    ID3 = None  # type: ignore
    MP4 = None  # type: ignore

log = logging.getLogger("mediasrv.scanner")


def track_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def _first(value) -> str:
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    return str(value) if value else ""


def _extract_cover(audio, tid: str) -> str | None:
    """Extract embedded artwork from an already-opened mutagen file."""
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
                if getattr(covers[0], "imageformat", None) == 14:
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


def _read_metadata(path: Path, tid: str) -> tuple[dict, str | None]:
    """Read tags, duration and cover from a file, opening it only once."""
    info = {"title": path.stem, "artist": "", "album": "", "duration": 0.0}
    if MutagenFile is None:
        return info, None

    try:
        audio = MutagenFile(path)
    except Exception as exc:
        log.warning("could not read tags for %s: %s", path, exc)
        return info, None
    if audio is None:
        return info, None

    try:
        info_obj = getattr(audio, "info", None)
        if info_obj is not None:
            info["duration"] = round(float(getattr(info_obj, "length", 0.0) or 0.0), 2)
    except Exception:
        pass

    tags = getattr(audio, "tags", None)
    if tags:
        try:
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
        except Exception:
            pass

    cover = _extract_cover(audio, tid)
    return info, cover


def _is_excluded(path: Path) -> bool:
    if not config.EXCLUDE:
        return False
    text = str(path)
    return any(fnmatch(text, pat) or fnmatch(path.name, pat) for pat in config.EXCLUDE)


def _iter_files(root: Path) -> Iterator[Path]:
    """Iterate files under ``root``.

    A manual walk (instead of os.walk) so we can log each directory *before*
    listing it -- the last logged line identifies a folder that hangs (e.g. a
    stalled network/external volume).
    """
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        log.info("scan: listing %s", current)
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as err:
            log.warning("scan: cannot list %s: %s", current, err)
            continue

        files: list[Path] = []
        subdirs: list[Path] = []
        for entry in entries:
            name = entry.name
            if config.IGNORE_HIDDEN and name.startswith("."):
                continue
            child = current / name
            if _is_excluded(child):
                continue
            try:
                if entry.is_dir(follow_symlinks=config.FOLLOW_SYMLINKS):
                    subdirs.append(child)
                else:
                    files.append(child)
            except OSError as err:
                log.warning("scan: cannot stat %s: %s", child, err)

        for path in sorted(files):
            yield path
        stack.extend(sorted(subdirs, reverse=True))


def _kind(suffix: str) -> str | None:
    if suffix in config.AUDIO_EXTS:
        return "audio"
    if suffix in config.VIDEO_EXTS:
        return "video"
    return None


def _scan_paths() -> set[str]:
    """Cheaply list playable files currently on disk (no tag reading)."""
    found: set[str] = set()
    for root in config.media_dirs():
        if not root.exists():
            continue
        for path in _iter_files(root):
            if _kind(path.suffix.lower()):
                found.add(str(path))
    return found


def _list_media() -> list[tuple[Path, str]]:
    """List all playable files (path, kind) without reading their tags."""
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for root in config.media_dirs():
        if not root.exists():
            log.warning("media folder does not exist: %s", root)
            continue
        log.info("scan: walking root %s", root)
        for path in _iter_files(root):
            kind = _kind(path.suffix.lower())
            if not kind:
                continue
            if not path.is_file():  # skip FIFOs/sockets/devices, which can block
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((path, kind))
    return found


def scan(progress=None) -> list[dict]:
    """Walk the configured media roots and build the library list.

    ``progress`` is an optional callable ``(done, total, path)`` invoked after
    each file, for UI/status reporting.
    """
    config.ensure_dirs()
    started = time.time()
    roots = ", ".join(str(p) for p in config.media_dirs())
    log.info("scan: start (roots: %s)", roots)

    media = _list_media()
    total = len(media)
    log.info("scan: %d media file(s) to inspect", total)

    tracks: list[dict] = []
    skipped = 0

    for index, (path, kind) in enumerate(media, 1):
        item_started = time.time()
        try:
            tid = track_id(path)
            stat = path.stat()
            meta, cover = _read_metadata(path, tid)

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
                    "ext": path.suffix.lower().lstrip("."),
                    "codec": codec,
                    "web_safe": web_safe,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "has_cover": cover is not None,
                    "path": str(path),
                }
            )
        except Exception as exc:
            skipped += 1
            log.warning("skipping %s: %s", path, exc)

        if progress is not None:
            try:
                progress(index, total, path)
            except Exception:
                pass

        elapsed = time.time() - item_started
        if elapsed > 5:
            log.warning("slow file (%.1fs): %s", elapsed, path)
        elif index % 50 == 0:
            log.info("scan progress: %d/%d", index, total)

    tracks.sort(key=lambda t: (t["artist"].lower(), t["album"].lower(), t["title"].lower()))
    save_cache(tracks)
    log.info(
        "scan: done: %d tracks (%d skipped) in %.1fs",
        len(tracks),
        skipped,
        time.time() - started,
    )
    return tracks


def _cache_signature() -> dict:
    return {
        "schema": 2,
        "roots": [str(p) for p in config.media_dirs()],
        "audio_extensions": sorted(config.AUDIO_EXTS),
        "video_extensions": sorted(config.VIDEO_EXTS),
        "follow_symlinks": config.FOLLOW_SYMLINKS,
        "ignore_hidden": config.IGNORE_HIDDEN,
        "exclude": list(config.EXCLUDE),
    }


def save_cache(tracks: list[dict]) -> None:
    config.ensure_dirs()
    payload = {"generated": time.time(), "signature": _cache_signature(), "tracks": tracks}
    tmp = config.LIBRARY_CACHE.parent / (config.LIBRARY_CACHE.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(config.LIBRARY_CACHE)


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
