#!/usr/bin/env python3
"""Render / install the MediaSrv launchd service from config.toml.

All user-dependent values (label, user, home, log path, executable, PATH) are
read from config.toml -- the same [service] section the app uses -- so nothing
is hard-coded in the plist.

Usage (via deploy/install-launchd.sh, which cd's to the repo root):

    install-launchd.sh render      # print the rendered plist
    install-launchd.sh write       # write it to deploy/generated/
    install-launchd.sh install     # install + load it as a LaunchDaemon (sudo)
    install-launchd.sh uninstall   # unload + remove it (sudo)
    install-launchd.sh status      # show launchd status
"""

from __future__ import annotations

import getpass
import os
import pwd
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

TEMPLATE = ROOT / "deploy" / "com.mediasrv.plist.template"
GENERATED_DIR = ROOT / "deploy" / "generated"
DAEMON_DIR = Path("/Library/LaunchDaemons")


def _resolve() -> dict[str, str]:
    svc = config.SERVICE
    user = svc["user"] or getpass.getuser()
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        home = Path.home()

    log = svc["log_file"] or str(home / "Library" / "Logs" / "MediaSrv.log")
    if log.startswith("~"):
        log = str(home / log[2:])

    return {
        "LABEL": svc["label"],
        "USER": user,
        "HOME": str(home),
        "WORKDIR": str(config.BASE_DIR),
        "EXEC": str(config.BASE_DIR / ".venv" / "bin" / "mediasrv"),
        "LOG": log,
        "PATH": svc["path"],
    }


def render() -> tuple[str, str]:
    values = _resolve()
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", escape(value))
    return values["LABEL"], text


def _sudo(*args: str, check: bool = True) -> None:
    subprocess.run(["sudo", *args], check=check)


def cmd_render() -> None:
    _, text = render()
    sys.stdout.write(text)


def cmd_write() -> None:
    label, text = render()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out = GENERATED_DIR / f"{label}.plist"
    out.write_text(text, encoding="utf-8")
    print(out)


def cmd_install() -> None:
    label, text = render()
    tmp = Path("/tmp") / f"{label}.plist"
    tmp.write_text(text, encoding="utf-8")
    dest = DAEMON_DIR / f"{label}.plist"

    _sudo("cp", str(tmp), str(dest))
    _sudo("chown", "root:wheel", str(dest))
    _sudo("chmod", "644", str(dest))
    _sudo("launchctl", "bootout", "system", str(dest), check=False)
    _sudo("launchctl", "bootstrap", "system", str(dest))
    print(f"installed and loaded: {dest}")
    print(f"  view logs: tail -f {_resolve()['LOG']}")


def cmd_uninstall() -> None:
    label, _ = render()
    dest = DAEMON_DIR / f"{label}.plist"
    _sudo("launchctl", "bootout", "system", str(dest), check=False)
    _sudo("rm", "-f", str(dest))
    print(f"removed: {dest}")


def cmd_status() -> None:
    label, _ = render()
    _sudo("launchctl", "print", f"system/{label}", check=False)


COMMANDS = {
    "render": cmd_render,
    "write": cmd_write,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "status": cmd_status,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print("usage: install-launchd.sh [render|write|install|uninstall|status]", file=sys.stderr)
        raise SystemExit(2)
    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
