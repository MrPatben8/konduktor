"""Resolves the app's per-OS, user-writable data directory.

Everything Konduktor persists that is NOT part of the user's collection lives
here: `userprefs.json` and the per-collection version-history git repos. It must
be a real writable location — the package-relative path prefs.py used to use is
read-only/ephemeral in a frozen PyInstaller build (`sys._MEIPASS`).

Resolution order:
  1. ``KONDUKTOR_DATA_DIR`` env var (tests point this at a temp dir to stay
     isolated from the real user data).
  2. platformdirs' per-OS user-data dir
     (~/Library/Application Support/Konduktor, %LOCALAPPDATA%\\Konduktor, …).

Best-effort: any failure to create the dir degrades to returning the path
anyway; callers already treat all data I/O as best-effort.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import platformdirs

_APP_NAME = "Konduktor"
_APP_AUTHOR = "Liquid Ice Studios"


def write_json(path: Path, data) -> None:
    """Write JSON so a crash mid-write cannot leave a truncated file.

    Deliberately NOT best-effort, unlike `prefs.save_prefs`: this is for data the
    user CURATED — export sets, library identity — where losing it is real work
    lost, not a forgotten window size. Failures raise so a caller can say so.

    Written to a temp file in the SAME directory and then `os.replace`d, which is
    atomic on POSIX and on Windows: a reader sees either the whole old file or
    the whole new one, never half of either.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # already replaced, which is the success path


def read_json(path: Path, default=None):
    """Read JSON, or `default` if it is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def app_data_dir() -> Path:
    """The user-writable directory for Konduktor's persisted app data."""
    override = os.environ.get("KONDUKTOR_DATA_DIR")
    base = Path(override) if override else Path(platformdirs.user_data_dir(_APP_NAME, _APP_AUTHOR))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # non-fatal — callers handle missing/unwritable data dirs
    return base
