"""Persistent favorites store (a plain JSON set of track ids)."""

from __future__ import annotations

import json

from . import config


def _read() -> list[str]:
    config.ensure_dirs()
    if not config.FAVORITES_FILE.exists():
        return []
    try:
        data = json.loads(config.FAVORITES_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        return [str(x) for x in data.get("ids", [])]
    return []


def _write(ids: list[str]) -> None:
    config.ensure_dirs()
    config.FAVORITES_FILE.write_text(
        json.dumps({"ids": ids}, indent=2), encoding="utf-8"
    )


def all_ids() -> set[str]:
    return set(_read())


def toggle(tid: str) -> bool:
    """Add or remove ``tid``; return True if it is now a favorite."""
    ids = _read()
    if tid in ids:
        ids = [x for x in ids if x != tid]
        _write(ids)
        return False
    ids.append(tid)
    _write(ids)
    return True
