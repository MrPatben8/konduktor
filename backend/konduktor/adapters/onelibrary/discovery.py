"""Find OneLibrary drives that are plugged in right now.

This is the one platform whose libraries have no fixed location: Traktor and
Rekordbox each keep theirs at a known per-OS path, but a OneLibrary library is
whatever USB stick happens to be mounted. So detection scans mount points rather
than probing a list of known paths, and what it finds legitimately changes
between two calls a second apart.

Only the mount ROOTS are scanned, never their contents recursively. A drive can
hold tens of thousands of files, the database is at a fixed place within it, and
walking a slow USB stick at startup is exactly the kind of thing that makes an
app feel broken.
"""
from __future__ import annotations

import logging
import os
import string
import sys
from pathlib import Path

from .layout import DriveLayout

log = logging.getLogger(__name__)


def _mount_points() -> list[Path]:
    """Plausible removable-media mount points for this OS."""
    out: list[Path] = []
    if sys.platform == "darwin":
        out.append(Path("/Volumes"))
    elif sys.platform.startswith("linux"):
        out.extend([Path("/media"), Path("/mnt"), Path("/run/media")])
        user = os.environ.get("USER")
        if user:
            out.append(Path("/media") / user)
            out.append(Path("/run/media") / user)

    roots: list[Path] = []
    for base in out:
        try:
            if not base.is_dir():
                continue
            roots.extend(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue

    if sys.platform == "win32":
        # No mount-point directory to list: check the drive letters directly.
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            try:
                if drive.is_dir():
                    roots.append(drive)
            except OSError:
                continue
    return roots


def describe(path: Path) -> dict:
    """A candidate record for a drive or database path, shaped for the picker."""
    path = Path(path)
    layout = DriveLayout.locate(path)
    database = layout.database if layout is not None else path
    exists = False
    try:
        exists = database.is_file()
    except OSError:
        pass
    # The drive's name is what a person recognises — "Hardy", not
    # "exportLibrary.db" — so the mount point supplies the label.
    root_name = layout.root.name if layout is not None else path.name
    return {
        "path": str(database),
        "label": f"OneLibrary — {root_name}" if root_name else "OneLibrary",
        "version": None,
        "modified": (database.stat().st_mtime if exists else None),
        "exists": exists,
    }


def detect_libraries() -> list[dict]:
    """Every mounted drive that carries a OneLibrary database, newest first."""
    found: list[dict] = []
    seen: set[str] = set()
    for root in _mount_points():
        layout = DriveLayout.locate(root)
        if layout is None:
            continue
        try:
            key = str(layout.database.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(describe(layout.root))
    found.sort(key=lambda c: c["modified"] or 0.0, reverse=True)
    return found
