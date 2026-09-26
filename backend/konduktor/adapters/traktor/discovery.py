"""Locate Traktor collections in their default install locations.

Traktor stores each version's data under a versioned folder:
    ~/Documents/Native Instruments/Traktor <version>/collection.nml
This is the same relative path on macOS and Windows (both under the user's
Documents folder), so a single glob covers both.
"""
from __future__ import annotations

import re
from pathlib import Path

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def _traktor_root() -> Path:
    return Path.home() / "Documents" / "Native Instruments"


def _version_key(version: str | None) -> tuple[int, ...]:
    """Sortable tuple for a dotted version string; missing/odd → lowest."""
    if not version:
        return (0,)
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def describe(path: Path) -> dict:
    """Build a candidate record for a collection.nml path. The label/version
    come from the parent folder name (e.g. 'Traktor 4.5.0')."""
    parent = path.parent.name
    m = _VERSION_RE.search(parent)
    exists = path.is_file()
    return {
        "path": str(path),
        "label": parent or path.name,
        "version": m.group(1) if m else None,
        "modified": (path.stat().st_mtime if exists else None),
        "exists": exists,
    }


def detect_collections() -> list[dict]:
    """All Traktor collection.nml files found in default locations, newest
    Traktor version first (then most-recently-modified)."""
    root = _traktor_root()
    found: list[dict] = []
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.lower().startswith("traktor"):
            continue
        nml = d / "collection.nml"
        if nml.is_file():
            found.append(describe(nml))
    found.sort(
        key=lambda c: (_version_key(c["version"]), c["modified"] or 0.0),
        reverse=True,
    )
    return found
