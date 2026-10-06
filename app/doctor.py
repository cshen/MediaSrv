"""Fixed, safe diagnostic commands for the web "Doctor" panel.

Only the commands defined in ``COMMANDS`` can ever run; the HTTP endpoint
never accepts or executes arbitrary input.
"""

from __future__ import annotations

import os
import platform
import select
import shutil
import subprocess
import time
from pathlib import Path

from . import config

try:  # POSIX only
    import fcntl
    import pty
    import struct
    import termios

    _HAS_PTY = True
except ImportError:  # pragma: no cover
    _HAS_PTY = False


def _human(num: float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _run(argv: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s: {' '.join(argv)}"


def _du_bytes(path: Path) -> int:
    if shutil.which("du"):
        try:
            proc = subprocess.run(
                ["du", "-sk", str(path)], capture_output=True, text=True, timeout=300
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return int(proc.stdout.split()[0]) * 1024
        except (OSError, ValueError):
            pass
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _transcode_dirs() -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()
    for d in (config.TRANSCODE_DIR, config.DEFAULT_DATA_DIR / "transcoded"):
        if str(d) not in seen:
            seen.add(str(d))
            dirs.append(d)
    return dirs


# ------------------------------------------------------------------ commands
def transcode_size() -> str:
    total = 0
    lines = []
    for d in _transcode_dirs():
        if not d.exists():
            lines.append(f"  {'-':>9}  {d}  (not found)")
            continue
        size = _du_bytes(d)
        total += size
        lines.append(f"  {_human(size):>9}  {d}")
    warn = ""
    if total > 20 * 1024**3:
        warn = f"\n\n!! WARNING: total {_human(total)} exceeds 20 GB"
    return "transcode cache:\n" + "\n".join(lines) + warn


def disk_usage() -> str:
    return _run(["df", "-h"])


def list_transcoded() -> str:
    d = config.TRANSCODE_DIR
    if not d.exists():
        return f"{d} does not exist"
    rows = []
    for entry in sorted(d.iterdir()):
        try:
            if entry.is_dir():
                rows.append(
                    f"  [partial] {entry.name}  ({len(list(entry.glob('*')))} files, "
                    f"{_human(_du_bytes(entry))})"
                )
            else:
                rows.append(f"  [file]    {entry.name}  {_human(entry.stat().st_size)}")
        except OSError:
            pass
    return f"{d}\n" + ("\n".join(rows) if rows else "  (empty)")


def _render_screen(data: bytes, rows: int, cols: int) -> str:
    """Reconstruct a TUI screen by honoring cursor position (CSI) sequences."""
    grid = [[" "] * cols for _ in range(rows)]
    r = c = 0

    def put(ch: str) -> None:
        nonlocal r, c
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = ch
        c += 1
        if c >= cols:
            c = 0
            r = min(r + 1, rows - 1)

    def csi(final: str, params: str) -> None:
        nonlocal r, c
        nums = [int(p) for p in params.replace("?", "").split(";") if p.isdigit()]
        p0 = nums[0] if nums else 0
        if final in ("H", "f"):
            r = max(0, min(rows - 1, (nums[0] if nums else 1) - 1))
            c = max(0, min(cols - 1, (nums[1] if len(nums) > 1 else 1) - 1))
        elif final == "A":
            r = max(0, r - (p0 or 1))
        elif final == "B":
            r = min(rows - 1, r + (p0 or 1))
        elif final == "C":
            c = min(cols - 1, c + (p0 or 1))
        elif final == "D":
            c = max(0, c - (p0 or 1))
        elif final == "G":
            c = max(0, min(cols - 1, (p0 or 1) - 1))
        elif final == "d":
            r = max(0, min(rows - 1, (p0 or 1) - 1))
        elif final == "J" and p0 == 2:
            for i in range(rows):
                grid[i] = [" "] * cols
        elif final == "K":
            if p0 == 0:
                for i in range(c, cols):
                    grid[r][i] = " "
            elif p0 == 2:
                grid[r] = [" "] * cols

    text = data.decode("utf-8", "replace")
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "[":
                j = i + 2
                start = j
                while j < n and (text[j].isdigit() or text[j] in ";?<>"):
                    j += 1
                final = text[j] if j < n else ""
                csi(final, text[start:j])
                i = j + 1
            elif nxt == "]":  # OSC ... BEL or ST
                j = i + 2
                while j < n and text[j] != "\x07":
                    if text[j] == "\x1b" and j + 1 < n and text[j + 1] == "\\":
                        break
                    j += 1
                i = j + 2
            elif nxt in "()*+-./#":  # charset designator (ESC ( B, ESC ) 0, ...)
                i += 3
            else:
                i += 2
            continue
        if ch == "\n":
            c = 0
            r = min(r + 1, rows - 1)
        elif ch == "\r":
            c = 0
        elif ch == "\b":
            c = max(0, c - 1)
        else:
            put(ch)
        i += 1

    lines = ["".join(row).rstrip() for row in grid]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines) or "(no output)"


def _snapshot_tui(argv0: str, seconds: float = 2.5) -> str:
    """Capture a short snapshot of an interactive TUI (htop).

    Runs it on a pseudo-terminal, grabs the screen for a moment, then strips
    the ANSI escapes so it is at least readable as plain text.
    """
    if not _HAS_PTY:
        return "(cannot capture interactive output on this platform)"

    rows, cols = 46, 140
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass

    env = dict(os.environ, TERM="xterm-256color")
    try:
        proc = subprocess.Popen(
            [argv0], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        return f"could not launch {os.path.basename(argv0)}: {exc}"
    os.close(slave)

    chunks: list[bytes] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        os.close(master)
    except OSError:
        pass

    return _render_screen(b"".join(chunks), rows, cols)


def system_monitor() -> str:
    """htop if present, else top."""
    for tool in ("htop", "top"):
        path = shutil.which(tool)
        if not path:
            continue
        if tool == "top":
            argv = (
                ["top", "-l", "1"]
                if platform.system() == "Darwin"
                else ["top", "-b", "-n", "1"]
            )
            return f"[{tool}]\n{_run(argv, timeout=30)}"
        return f"[{tool} snapshot ~2s]\n{_snapshot_tui(path)}"
    return "no system monitor found (htop and top are both missing)"


def clear_segments() -> str:
    d = config.TRANSCODE_DIR
    removed = 0
    freed = 0
    if d.exists():
        for entry in list(d.iterdir()):
            try:
                if entry.is_dir():
                    freed += _du_bytes(entry)
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                elif entry.name.endswith(".part.mp4"):
                    freed += entry.stat().st_size
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    return f"removed {removed} item(s), freed ~{_human(freed)}"


COMMANDS: list[dict] = [
    {
        "id": "size",
        "label": "Transcode cache size",
        "description": "Sizes of the transcoded/ folders (config + default).",
        "danger": False,
        "run": transcode_size,
    },
    {
        "id": "disk",
        "label": "Disk usage",
        "description": "df -h",
        "danger": False,
        "run": disk_usage,
    },
    {
        "id": "list",
        "label": "List transcoded files",
        "description": "Contents of the transcoded/ folder.",
        "danger": False,
        "run": list_transcoded,
    },
    {
        "id": "monitor",
        "label": "System monitor",
        "description": "htop, else top.",
        "danger": False,
        "run": system_monitor,
    },
    {
        "id": "clear",
        "label": "Clear partial segments",
        "description": "Delete transcoded/<id>/ work folders and *.part.mp4 (keeps finished .mp4).",
        "danger": True,
        "run": clear_segments,
    },
]

_BY_ID = {c["id"]: c for c in COMMANDS}


def public_commands() -> list[dict]:
    return [
        {"id": c["id"], "label": c["label"], "description": c["description"], "danger": c["danger"]}
        for c in COMMANDS
    ]


def run(command_id: str) -> str:
    cmd = _BY_ID.get(command_id)
    if cmd is None:
        raise KeyError(command_id)
    return str(cmd["run"]())
