"""Locate Rekordbox libraries in their default install locations.

Rekordbox keeps one library per install, at a fixed per-OS path:

    macOS    ~/Library/Pioneer/rekordbox/master.db
    Windows  %APPDATA%/Pioneer/rekordbox/master.db

``pyrekordbox`` already resolves this (including the v6/v7 split and any
non-default location recorded in Rekordbox's own settings), so that is the
primary source and the hard-coded paths are the fallback.
"""
from __future__ import annotations

from pathlib import Path

_RELATIVE = Path("Pioneer") / "rekordbox" / "master.db"


def _configured_paths() -> list[Path]:
    """Library paths pyrekordbox finds from Rekordbox's own configuration."""
    out: list[Path] = []
    try:
        from pyrekordbox.config import get_config
    except Exception:  # noqa: BLE001 — never let discovery break startup
        return out
    for version in ("rekordbox7", "rekordbox6"):
        try:
            conf = get_config(version)
        except Exception:  # noqa: BLE001
            continue
        if not conf:
            continue
        p = conf.get("db_path")
        if p:
            out.append(Path(p))
    return out


def _default_paths() -> list[Path]:
    home = Path.home()
    return [
        home / "Library" / _RELATIVE,          # macOS
        home / "AppData" / "Roaming" / _RELATIVE,  # Windows
    ]


def describe(path: Path) -> dict:
    """A candidate record for a master.db path, shaped like the picker wants."""
    path = Path(path)
    exists = path.is_file()
    # The version Rekordbox itself is, not a schema version stamped in the file:
    # master.db carries no version field, so the folder is the best label there is.
    return {
        "path": str(path),
        "label": "Rekordbox",
        "version": None,
        "modified": (path.stat().st_mtime if exists else None),
        "exists": exists,
    }


def detect_libraries() -> list[dict]:
    """Every Rekordbox master.db found in a default location, newest first."""
    seen: set[str] = set()
    found: list[dict] = []
    for p in _configured_paths() + _default_paths():
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        found.append(describe(p))
    found.sort(key=lambda c: c["modified"] or 0.0, reverse=True)
    return found
