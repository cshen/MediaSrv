"""Transcode videos that browsers cannot play natively into H.264 + AAC MP4.

iPadOS Safari (and some other browsers) cannot play VP9/AV1 inside an MP4
container. Files like that are converted on demand, in the background, to a
web-safe MP4 kept in ``data/transcoded/``. The original file is never touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

from . import config

# Codecs every browser (including iPadOS Safari) plays inside MP4.
WEB_SAFE_CODECS = {"h264"}

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

_encoder_cache: dict[str, bool] = {}


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


def probe_codec(path: Path) -> str | None:
    """Return the video stream codec name, or None if it cannot be determined."""
    if not FFPROBE:
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            return str(streams[0].get("codec_name") or "").lower() or None
    except Exception:
        return None
    return None


def is_web_safe(codec: str | None) -> bool:
    # Unknown codec -> assume playable so we never transcode unnecessarily.
    if not codec:
        return True
    return codec.lower() in WEB_SAFE_CODECS


def _has_encoder(name: str) -> bool:
    if name in _encoder_cache:
        return _encoder_cache[name]
    ok = False
    if FFMPEG:
        try:
            out = subprocess.run(
                [FFMPEG, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            ok = name in out
        except Exception:
            ok = False
    _encoder_cache[name] = ok
    return ok


class Transcoder:
    """Single-worker background transcoder with per-track status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._worker: threading.Thread | None = None

    def output_path(self, tid: str) -> Path:
        return config.TRANSCODE_DIR / f"{tid}.mp4"

    def ready_path(self, track: dict) -> Path | None:
        """Return the transcoded file if present and newer than the source."""
        out = self.output_path(track["id"])
        if not out.exists():
            return None
        try:
            if Path(track["path"]).stat().st_mtime > out.stat().st_mtime + 0.5:
                out.unlink()  # source changed -> re-transcode
                return None
        except OSError:
            pass
        return out

    def status(self, tid: str) -> dict:
        if self.output_path(tid).exists():
            return {"state": "ready", "progress": 100}
        with self._lock:
            job = self._jobs.get(tid)
            if job:
                return dict(job)
        return {"state": "idle", "progress": 0}

    def start(self, track: dict) -> dict:
        """Queue a track for transcoding (idempotent). Returns current status."""
        tid = track["id"]
        if self.ready_path(track) is not None:
            return {"state": "ready", "progress": 100}
        with self._lock:
            job = self._jobs.get(tid)
            if job:
                return dict(job)
            self._jobs[tid] = {"state": "queued", "progress": 0, "error": None}
            self._pending.append(track)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True)
                self._worker.start()
        return {"state": "queued", "progress": 0}

    def _set(self, tid: str, **fields) -> None:
        with self._lock:
            self._jobs.setdefault(tid, {}).update(fields)

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                track = self._pending.pop(0)
            try:
                self._run(track)
            except Exception as exc:  # pragma: no cover - defensive
                self._set(track["id"], state="error", error=str(exc))

    def _run(self, track: dict) -> None:
        tid = track["id"]
        src = Path(track["path"])
        out = self.output_path(tid)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.mp4")

        self._set(tid, state="processing", progress=0, error=None)
        duration = float(track.get("duration") or 0.0)
        cmd = self._build_cmd(src, tmp)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("out_time_"):
                    continue
                try:
                    micros = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration > 0:
                    pct = max(0, min(99, int((micros / 1_000_000) / duration * 100)))
                    self._set(tid, progress=pct)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg failed (exit {proc.returncode})")
            tmp.replace(out)
            self._set(tid, state="ready", progress=100, error=None)
        except Exception as exc:
            self._set(tid, state="error", error=str(exc))
            try:
                tmp.unlink()
            except OSError:
                pass

    def _build_cmd(self, src: Path, dst: Path) -> list[str]:
        assert FFMPEG is not None
        encoder = config.TRANSCODE_ENCODER
        if encoder == "auto":
            encoder = "videotoolbox" if _has_encoder("h264_videotoolbox") else "libx264"

        cmd = [
            FFMPEG, "-y", "-v", "error",
            "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?",
        ]
        if encoder in ("videotoolbox", "h264_videotoolbox"):
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", config.TRANSCODE_VIDEO_BITRATE]
        else:
            cmd += [
                "-c:v", "libx264",
                "-crf", str(config.TRANSCODE_CRF),
                "-preset", config.TRANSCODE_PRESET,
            ]
        cmd += [
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", config.TRANSCODE_AUDIO_BITRATE,
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(dst),
        ]
        return cmd


transcoder = Transcoder()
