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
import logging
import math
import shutil
import subprocess
import threading
from pathlib import Path

from . import config

log = logging.getLogger("mediasrv.transcode")

# What browsers (especially iPadOS Safari) can play natively inside <video>.
WEB_SAFE_CONTAINERS = {".mp4", ".m4v", ".mov"}
WEB_SAFE_VIDEO = {"h264"}
WEB_SAFE_AUDIO = {"aac", "mp3"}

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

_encoder_cache: dict[str, bool] = {}


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


def _container(track: dict) -> str:
    ext = (track.get("ext") or "").lower().lstrip(".")
    return f".{ext}" if ext else ""


def probe_streams(path: Path) -> dict:
    """Return ``{"video","audio","duration"}`` for a file."""
    result: dict = {"video": None, "audio": None, "duration": 0.0}
    if not FFPROBE:
        return result
    try:
        proc = subprocess.run(
            [
                FFPROBE,
                "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name:stream_disposition=attached_pic:format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
        for stream in data.get("streams", []):
            kind = stream.get("codec_type")
            codec = (stream.get("codec_name") or "").lower()
            attached = (stream.get("disposition") or {}).get("attached_pic")
            if kind == "video" and not attached and result["video"] is None:
                result["video"] = codec or None
            elif kind == "audio" and result["audio"] is None:
                result["audio"] = codec or None
        try:
            result["duration"] = round(float(data.get("format", {}).get("duration") or 0.0), 2)
        except (TypeError, ValueError):
            result["duration"] = 0.0
    except Exception:
        pass
    return result


def probe_codec(path: Path) -> str | None:
    """Return the (first real) video stream codec name, or None."""
    return probe_streams(path)["video"]


def is_web_safe(container: str, video: str | None, audio: str | None) -> bool:
    """True only if Safari can play it as-is: MP4/MOV + H.264 + AAC/MP3."""
    container = (container or "").lower()
    video = (video or "").lower()
    audio = (audio or "").lower()
    if container not in WEB_SAFE_CONTAINERS:
        return False
    if video not in WEB_SAFE_VIDEO:
        return False
    if audio and audio not in WEB_SAFE_AUDIO:
        return False
    return True


def strategy(container: str, video: str | None, audio: str | None) -> str:
    """How to make a file playable: none | remux | audio | video."""
    if is_web_safe(container, video, audio):
        return "none"
    if (video or "").lower() in WEB_SAFE_VIDEO:
        # Video is fine: just repackage, and only touch audio if needed.
        if not audio or audio.lower() in WEB_SAFE_AUDIO:
            return "remux"
        return "audio"
    return "video"


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
        out = self.output_path(tid)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.parent / f"{out.stem}.part.mp4"
        self._set(tid, state="processing", progress=0, error=None)

        plan = strategy(_container(track), track.get("codec"), track.get("audio_codec"))
        try:
            if plan in ("remux", "audio"):
                self._run_simple(track, plan, tmp_out)
            else:
                self._run_segmented(track, tmp_out)
            tmp_out.replace(out)
            self._set(tid, state="ready", progress=100, error=None)
        except Exception as exc:
            self._set(tid, state="error", error=str(exc))
            try:
                tmp_out.unlink()
            except OSError:
                pass

    def _run_simple(self, track: dict, plan: str, dst: Path) -> None:
        """Fast path: repackage to MP4 with stream copy (no re-encoding).

        ``remux`` copies video and audio; ``audio`` copies video and only
        re-encodes the audio (e.g. DTS/AC3 -> AAC).
        """
        tid = track["id"]
        src = Path(track["path"])
        duration = float(track.get("duration") or 0.0)

        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c:v", "copy"]
        if plan == "remux":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", config.TRANSCODE_AUDIO_BITRATE]
        cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst)]

        def report(secs: float) -> None:
            if duration > 0:
                self._set(tid, progress=max(0, min(99, int(secs / duration * 100))))

        self._exec(cmd, report)

    def _run_segmented(self, track: dict, tmp_out: Path) -> None:
        """Resumable full re-encode, written as fixed-length segments."""
        tid = track["id"]
        src = Path(track["path"])

        duration = float(track.get("duration") or 0.0)
        if duration <= 0:
            duration = float(probe_streams(src).get("duration") or 0.0)
        seg_len = max(1, config.TRANSCODE_SEGMENT_SECONDS)

        # Without a known duration we can't segment safely -> single pass.
        if duration <= 0:
            log.warning("transcode: unknown duration for %s; encoding in one pass", src)
            self._run_full(track, tmp_out)
            return

        seg_dir = self._work_dir(tid)
        seg_dir.mkdir(parents=True, exist_ok=True)

        # Discard segments from a previous run if the source or settings changed.
        try:
            src_mtime = src.stat().st_mtime
        except OSError:
            src_mtime = 0.0
        want_meta = {"v": 2, "path": str(src), "mtime": src_mtime, "segment": seg_len}
        meta_file = seg_dir / "meta.json"
        try:
            existing_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_meta = None
        if existing_meta != want_meta:
            shutil.rmtree(seg_dir, ignore_errors=True)
            seg_dir.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(want_meta), encoding="utf-8")

        total = max(1, math.ceil(duration / seg_len))

        index = 0
        while index < total:
            seg_final = seg_dir / f"seg-{index:05d}.mp4"
            if not seg_final.exists():
                seg_part = seg_dir / f"seg-{index:05d}.part.mp4"
                start = index * seg_len

                def report(secs: float, i: int = index) -> None:
                    frac = min(1.0, secs / seg_len) if seg_len else 0.0
                    self._set(tid, progress=max(0, min(89, int(((i + frac) / total) * 90))))

                self._exec(self._segment_cmd(src, seg_part, start, seg_len), report)
                seg_part.replace(seg_final)
                # An empty segment means we overran a broken/unknown duration.
                try:
                    if index < total - 1 and seg_final.stat().st_size < 1024:
                        raise RuntimeError("ffmpeg produced an empty segment (bad duration?)")
                except OSError:
                    pass
            index += 1
            self._set(tid, progress=min(90, int(index / total * 90)))

        self._set(tid, progress=92)
        self._exec(self._concat_cmd(seg_dir, tmp_out, total))
        self._set(tid, progress=99)
        shutil.rmtree(seg_dir, ignore_errors=True)

    def _run_full(self, track: dict, dst: Path) -> None:
        """Single-pass full re-encode (used when the duration is unknown)."""
        tid = track["id"]
        src = Path(track["path"])
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += self._encoder_args()
        cmd += [
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", config.TRANSCODE_AUDIO_BITRATE,
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(dst),
        ]
        self._exec(cmd, lambda secs: None)
        self._set(tid, progress=99)

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
