"""MediaSrv -- a tiny home media player server.

Serves a single-page web player plus the media files themselves, with proper
HTTP Range support so browsers can seek (drag the progress bar) and Safari can
stream video.
"""

from __future__ import annotations

import logging
import mimetypes
import threading
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, favorites, scanner, transcode

log = logging.getLogger("mediasrv")
CHUNK_SIZE = 1024 * 256

app = FastAPI(title="My Media Hub", docs_url=None, redoc_url=None)


class Library:
    """Thread-safe in-memory view of the scanned media.

    Loading/scanning happens in a background thread so requests are never
    blocked; callers read whatever is currently available and may check
    :attr:`scanning`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._loaded = False
        self._scanning = False
        self._progress: dict = {"done": 0, "total": 0}

    @property
    def scanning(self) -> bool:
        with self._lock:
            return self._scanning

    @property
    def progress(self) -> dict:
        with self._lock:
            return dict(self._progress)

    def _on_progress(self, done: int, total: int, path) -> None:
        with self._lock:
            self._progress = {"done": done, "total": total}

    def ensure(self) -> None:
        """Kick off a background load/scan if not already loaded/running."""
        with self._lock:
            if self._loaded or self._scanning:
                return
            self._scanning = True
            self._progress = {"done": 0, "total": 0}
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            tracks = scanner.load_cache()
            if tracks is None:
                tracks = scanner.scan(progress=self._on_progress)
        except Exception:
            log.exception("library load failed")
            tracks = []
        self._install(tracks)

    def rescan(self) -> None:
        """Kick off a background rescan (no-op if one is already running)."""
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
            self._progress = {"done": 0, "total": 0}
        threading.Thread(target=self._rescan, daemon=True).start()

    def _rescan(self) -> None:
        try:
            tracks = scanner.scan(progress=self._on_progress)
        except Exception:
            log.exception("library rescan failed")
            with self._lock:
                tracks = self._tracks
        self._install(tracks)

    def _install(self, tracks: list[dict]) -> None:
        with self._lock:
            self._tracks = tracks
            self._by_id = {t["id"]: t for t in tracks}
            self._loaded = True
            self._scanning = False
            self._progress = {"done": len(tracks), "total": len(tracks)}

    def tracks(self) -> list[dict]:
        self.ensure()
        with self._lock:
            return list(self._tracks)

    def get(self, tid: str) -> dict | None:
        self.ensure()
        with self._lock:
            return self._by_id.get(tid)


library = Library()


def _needs_transcode(track: dict) -> bool:
    return (
        track.get("type") == "video"
        and not track.get("web_safe", True)
        and config.TRANSCODE_ENABLED
        and transcode.available()
    )


def _public(track: dict, fav: set[str]) -> dict:
    return {
        "id": track["id"],
        "title": track["title"],
        "artist": track["artist"],
        "album": track["album"],
        "duration": track["duration"],
        "type": track["type"],
        "ext": track["ext"],
        "has_cover": track["has_cover"],
        "favorite": track["id"] in fav,
        "cover": f"/api/cover/{track['id']}" if track["has_cover"] else None,
        "src": f"/media/{track['id']}",
        "codec": track.get("codec", ""),
        "web_safe": track.get("web_safe", True),
        "needs_transcode": _needs_transcode(track),
    }


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    library.ensure()  # warm the library in the background at boot


@app.get("/api/config")
def api_config() -> JSONResponse:
    return JSONResponse(config.public())


@app.get("/api/tracks")
def api_tracks() -> JSONResponse:
    fav = favorites.all_ids()
    tracks = [_public(t, fav) for t in library.tracks()]
    return JSONResponse(
        {
            "count": len(tracks),
            "tracks": tracks,
            "scanning": library.scanning,
            "progress": library.progress,
        }
    )


@app.post("/api/rescan")
def api_rescan() -> JSONResponse:
    library.rescan()
    return JSONResponse({"scanning": library.scanning})


@app.get("/api/favorites")
def api_favorites() -> JSONResponse:
    return JSONResponse({"ids": sorted(favorites.all_ids())})


@app.post("/api/favorites/{tid}")
def api_toggle_favorite(tid: str) -> JSONResponse:
    if library.get(tid) is None:
        raise HTTPException(status_code=404, detail="unknown track")
    now = favorites.toggle(tid)
    return JSONResponse({"id": tid, "favorite": now})


@app.get("/api/cover/{tid}")
def api_cover(tid: str) -> FileResponse:
    track = library.get(tid)
    if track is None or not track["has_cover"]:
        raise HTTPException(status_code=404, detail="no cover")
    for ext in (".jpg", ".jpeg", ".png"):
        candidate = config.COVER_DIR / f"{tid}{ext}"
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="cover missing")


def _range_response(path: Path, request: Request, content_type: str) -> Response:
    """Serve ``path`` honouring a single HTTP Range header."""
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(path, media_type=content_type)

    try:
        units, _, rng = range_header.partition("=")
        if units.strip().lower() != "bytes":
            raise ValueError
        start_s, _, end_s = rng.partition("-")
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
        else:
            length = int(end_s)
            start = max(file_size - length, 0)
            end = file_size - 1
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid range")

    start = max(start, 0)
    end = min(end, file_size - 1)
    if start > end:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    length = end - start + 1
    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-cache",
    }

    def stream() -> Iterator[bytes]:
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=206, headers=headers, media_type=content_type)


@app.get("/api/transcode/{tid}")
def api_transcode(tid: str) -> JSONResponse:
    track = library.get(tid)
    if track is None:
        raise HTTPException(status_code=404, detail="unknown track")
    if not _needs_transcode(track):
        return JSONResponse({"state": "not_needed", "progress": 100})
    return JSONResponse(transcode.transcoder.start(track))


@app.get("/media/{tid}")
def api_media(tid: str, request: Request) -> Response:
    track = library.get(tid)
    if track is None:
        raise HTTPException(status_code=404, detail="unknown track")

    path = Path(track["path"])
    if _needs_transcode(track):
        out = transcode.transcoder.ready_path(track)
        if out is not None:
            path = out
        else:
            transcode.transcoder.start(track)
            return JSONResponse(status_code=202, content=transcode.transcoder.status(tid))
    elif not path.is_file():
        library.rescan()
        raise HTTPException(status_code=404, detail="file missing")

    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return _range_response(path, request, content_type)


static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    run()
