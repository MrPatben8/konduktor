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

import os
from pathlib import Path

import platformdirs

_APP_NAME = "Konduktor"
_APP_AUTHOR = "Liquid Ice Studios"


def app_data_dir() -> Path:
    """The user-writable directory for Konduktor's persisted app data."""
    override = os.environ.get("KONDUKTOR_DATA_DIR")
    base = Path(override) if override else Path(platformdirs.user_data_dir(_APP_NAME, _APP_AUTHOR))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # non-fatal — callers handle missing/unwritable data dirs
    return base
