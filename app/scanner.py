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
import threading
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


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _fix_mojibake(text: str | None) -> str:
    """Repair CJK text whose bytes were decoded as Latin-1.

    Old ID3 tags often store UTF-8 or GBK bytes but label them Latin-1, giving
    mojibake like ``ÐíÃÀ¾²`` (GBK "许美静") or ``å¤©ç¢`` (UTF-8 "天盛"). We
    recover the original bytes and try the real encodings.
    """
    if not text:
        return ""
    if any("\u4e00" <= c <= "\u9fff" for c in text):
        return text  # already valid CJK
    if not any(0x80 <= ord(c) <= 0xFF for c in text):
        return text  # pure ASCII/Latin-1, nothing to fix
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            candidate = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if any("\u4e00" <= c <= "\u9fff" for c in candidate):
            return candidate
    return text


def _pick_text(primary: str | None, fallback: str | None) -> str:
    """Prefer the repaired primary value; use the fallback if primary is lossy."""
    a = _fix_mojibake(primary)
    b = _fix_mojibake(fallback)
    if b and (not a or "?" in a) and "?" not in b:
        return b
    return a or b


def _read_id3v1(path: Path) -> dict | None:
    """Read the ID3v1 tag (last 128 bytes) of an MP3, if present."""
    try:
        with path.open("rb") as fh:
            fh.seek(-128, os.SEEK_END)
            tail = fh.read(128)
    except OSError:
        return None
    if len(tail) < 128 or tail[:3] != b"TAG":
        return None

    def field(chunk: bytes) -> str:
        chunk = chunk.split(b"\x00", 1)[0].rstrip(b" ")
        return _decode_bytes(chunk)

    return {
        "title": field(tail[3:33]),
        "artist": field(tail[33:63]),
        "album": field(tail[63:93]),
    }


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
    info = {"title": "", "artist": "", "album": "", "duration": 0.0}
    if MutagenFile is None:
        info["title"] = path.stem
        return info, None

    try:
        audio = MutagenFile(path)
    except Exception as exc:
        log.warning("could not read tags for %s: %s", path, exc)
        info["title"] = path.stem
        return info, None
    if audio is None:
        info["title"] = path.stem
        return info, None

    try:
        info_obj = getattr(audio, "info", None)
        if info_obj is not None:
            info["duration"] = round(float(getattr(info_obj, "length", 0.0) or 0.0), 2)
    except Exception:
        pass

    v2: dict[str, str | None] = {"title": None, "artist": None, "album": None}
    tags = getattr(audio, "tags", None)
    if tags:
        try:
            for key in ("title", "TIT2", "\xa9nam"):
                if key in tags:
                    v2["title"] = _first(tags[key])
                    break
            for key in ("artist", "TPE1", "\xa9ART", "aART"):
                if key in tags:
                    v2["artist"] = _first(tags[key])
                    break
            for key in ("album", "TALB", "\xa9alb"):
                if key in tags:
                    v2["album"] = _first(tags[key])
                    break
        except Exception:
            pass

    # MP3s often keep a correct (GBK) ID3v1 tag next to a broken ID3v2 one.
    v1 = _read_id3v1(path) if path.suffix.lower() == ".mp3" else None

    info["title"] = _pick_text(v2["title"], v1["title"] if v1 else None) or path.stem
    info["artist"] = _pick_text(v2["artist"], v1["artist"] if v1 else None)
    info["album"] = _pick_text(v2["album"], v1["album"] if v1 else None)

    cover = _extract_cover(audio, tid)
    return info, cover


def _is_excluded(path: Path) -> bool:
    if not config.EXCLUDE:
        return False
    text = str(path)
    return any(fnmatch(text, pat) or fnmatch(path.name, pat) for pat in config.EXCLUDE)


def _list_dir(path: Path, timeout: float) -> list[tuple[str, bool]] | None:
    """List ``path`` into ``[(name, is_dir), ...]``.

    Returns None if the directory does not respond within ``timeout`` seconds
    (a frozen mount / TCC-protected app library). The listing runs on a daemon
    thread so a blocking syscall can never hang the scan or the server; the
    stuck thread is simply abandoned.
    """
    if not timeout or timeout <= 0:
        return _list_dir_inline(path)

    box: dict = {}

    def work() -> None:
        try:
            with os.scandir(path) as it:
                out: list[tuple[str, bool]] = []
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=config.FOLLOW_SYMLINKS)
                    except OSError:
                        is_dir = False
                    out.append((entry.name, is_dir))
                box["entries"] = out
        except OSError as exc:
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True, name="scan-listdir")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None  # frozen: give up on this directory
    if "error" in box:
        log.warning("scan: cannot list %s: %s", path, box["error"])
        return []
    return box.get("entries", [])


def _list_dir_inline(path: Path) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=config.FOLLOW_SYMLINKS)
                except OSError:
                    is_dir = False
                out.append((entry.name, is_dir))
    except OSError as exc:
        log.warning("scan: cannot list %s: %s", path, exc)
    return out


def _iter_files(root: Path) -> Iterator[Path]:
    """Iterate files under ``root``, skipping directories that never respond."""
    timeout = config.DIRECTORY_TIMEOUT
    stack: list[Path] = [root]
    frozen = 0
    while stack:
        current = stack.pop()
        log.debug("scan: listing %s", current)
        listing = _list_dir(current, timeout)
        if listing is None:
            frozen += 1
            log.warning(
                "scan: skipping frozen directory %s (no response in %ss)",
                current,
                timeout,
            )
            continue

        files: list[Path] = []
        subdirs: list[Path] = []
        for name, is_dir in listing:
            if config.IGNORE_HIDDEN and name.startswith("."):
                continue
            child = current / name
            if _is_excluded(child):
                continue
            if is_dir:
                subdirs.append(child)
            else:
                files.append(child)

        for path in sorted(files):
            yield path
        stack.extend(sorted(subdirs, reverse=True))

    if frozen:
        log.warning("scan: skipped %d frozen director(ies)", frozen)


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
        "schema": 3,
        "roots": [str(p) for p in config.media_dirs()],
        "audio_extensions": sorted(config.AUDIO_EXTS),
        "video_extensions": sorted(config.VIDEO_EXTS),
        "follow_symlinks": config.FOLLOW_SYMLINKS,
        "ignore_hidden": config.IGNORE_HIDDEN,
        "exclude": list(config.EXCLUDE),
        "directory_timeout": config.DIRECTORY_TIMEOUT,
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
