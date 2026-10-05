"""Transcode videos that browsers cannot play natively into H.264 + AAC MP4.

iPadOS Safari (and some other browsers) cannot play VP9/AV1 inside an MP4
container. Files like that are converted on demand, in the background, to a
web-safe MP4 kept in ``data/transcoded/``. The original file is never touched.

Transcoding is *resumable*: the source is cut into fixed-length segments, each
encoded to its own file. If the server stops part-way through, the finished
segments are kept and only the remaining ones are encoded on the next run,
followed by a fast stream-copy concat into the final MP4.
"""

from __future__ import annotations

import json
import math
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
    """Single-worker background transcoder with per-track status and resume."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._worker: threading.Thread | None = None

    # ---------------------------------------------------------------- paths
    def output_path(self, tid: str) -> Path:
        return config.TRANSCODE_DIR / f"{tid}.mp4"

    def _work_dir(self, tid: str) -> Path:
        return config.TRANSCODE_DIR / tid

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

    # ---------------------------------------------------------------- status
    def status(self, tid: str) -> dict:
        if self.output_path(tid).exists():
            return {"state": "ready", "progress": 100}
        with self._lock:
            job = self._jobs.get(tid)
            if job:
                return dict(job)
        # Not this process's job, but partial segments may exist from before.
        if self._work_dir(tid).exists():
            return {"state": "idle", "progress": 0}
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

    # ---------------------------------------------------------------- worker
    def _run(self, track: dict) -> None:
        tid = track["id"]
        src = Path(track["path"])
        out = self.output_path(tid)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.parent / f"{out.stem}.part.mp4"

        seg_dir = self._work_dir(tid)
        seg_dir.mkdir(parents=True, exist_ok=True)

        duration = float(track.get("duration") or 0.0)
        seg_len = max(1, config.TRANSCODE_SEGMENT_SECONDS)

        self._set(tid, state="processing", progress=0, error=None)

        # If the source changed, throw away any segments from a previous run.
        try:
            src_mtime = src.stat().st_mtime
        except OSError:
            src_mtime = 0.0
        want_meta = {"path": str(src), "mtime": src_mtime, "segment": seg_len}
        meta_file = seg_dir / "meta.json"
        try:
            existing_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_meta = None
        if existing_meta != want_meta:
            shutil.rmtree(seg_dir, ignore_errors=True)
            seg_dir.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(want_meta), encoding="utf-8")

        total = max(1, math.ceil(duration / seg_len)) if duration > 0 else None

        try:
            index = 0
            while total is None or index < total:
                if duration > 0 and index * seg_len >= duration:
                    break
                seg_final = seg_dir / f"seg-{index:05d}.mp4"
                if not seg_final.exists():
                    seg_part = seg_dir / f"seg-{index:05d}.part.mp4"
                    start = index * seg_len

                    def report(secs: float, i: int = index) -> None:
                        frac = min(1.0, secs / seg_len) if seg_len else 0.0
                        pct = int(((i + frac) / total) * 90) if total else 0
                        self._set(tid, progress=max(0, min(89, pct)))

                    self._exec(self._segment_cmd(src, seg_part, start, seg_len), report)
                    seg_part.replace(seg_final)
                index += 1
                if total:
                    self._set(tid, progress=min(90, int(index / total * 90)))
            if total is None:
                total = index

            self._set(tid, progress=92)
            self._exec(self._concat_cmd(seg_dir, tmp_out, total))
            self._set(tid, progress=99)
            tmp_out.replace(out)
            shutil.rmtree(seg_dir, ignore_errors=True)
            self._set(tid, state="ready", progress=100, error=None)
        except Exception as exc:
            self._set(tid, state="error", error=str(exc))
            try:
                tmp_out.unlink()
            except OSError:
                pass

    def _exec(self, cmd: list[str], on_progress=None) -> None:
        assert FFMPEG is not None
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
            if on_progress is None or not line.startswith("out_time_"):
                continue
            try:
                micros = int(line.split("=", 1)[1])
            except ValueError:
                continue
            on_progress(micros / 1_000_000)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (exit {proc.returncode})")

    def _encoder_args(self) -> list[str]:
        encoder = config.TRANSCODE_ENCODER
        if encoder == "auto":
            encoder = "videotoolbox" if _has_encoder("h264_videotoolbox") else "libx264"
        if encoder in ("videotoolbox", "h264_videotoolbox"):
            return ["-c:v", "h264_videotoolbox", "-b:v", config.TRANSCODE_VIDEO_BITRATE]
        return [
            "-c:v", "libx264",
            "-crf", str(config.TRANSCODE_CRF),
            "-preset", config.TRANSCODE_PRESET,
        ]

    def _segment_cmd(self, src: Path, dst: Path, start: float, length: float) -> list[str]:
        assert FFMPEG is not None
        cmd = [FFMPEG, "-y", "-v", "error"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(src), "-t", f"{length:.3f}", "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += self._encoder_args()
        cmd += [
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", config.TRANSCODE_AUDIO_BITRATE,
            "-avoid_negative_ts", "make_zero",
            "-progress", "pipe:1", "-nostats",
            str(dst),
        ]
        return cmd

    def _concat_cmd(self, seg_dir: Path, dst: Path, count: int) -> list[str]:
        assert FFMPEG is not None
        lines = []
        for i in range(count):
            name = f"seg-{i:05d}.mp4"
            if not (seg_dir / name).exists():
                raise RuntimeError(f"missing segment {name}")
            lines.append(f"file '{name}'")
        list_file = seg_dir / "concat.txt"
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [
            FFMPEG, "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(dst),
        ]


transcoder = Transcoder()
