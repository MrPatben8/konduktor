"""Which adapter can open a given path.

Selection is a ``can_open()`` probe, not an extension match. That matters as soon
as a second platform exists: Rekordbox's library is a SQLite file called
``master.db`` and Serato's is a ``_Serato_`` *directory*, so neither can be
recognised by suffix alone, and a probe lets an adapter be as specific as it
needs to be (Traktor's reads 4 KB and looks for the NML header).
"""
from __future__ import annotations

from pathlib import Path

from .adapter import LibraryDriver, LibraryNotSupported

_DRIVERS: list[LibraryDriver] = []


def register(driver: LibraryDriver) -> None:
    """Register a platform. Called once per adapter package, at import."""
    if not any(d.platform == driver.platform for d in _DRIVERS):
        _DRIVERS.append(driver)


def drivers() -> list[LibraryDriver]:
    return list(_DRIVERS)


def driver_for(path: Path) -> LibraryDriver:
    """The first registered driver that recognises `path`."""
    for d in _DRIVERS:
        try:
            if d.can_open(path):
                return d
        except OSError:
            continue
    raise LibraryNotSupported(f"No DJ library Konduktor recognises at {path}")


def open_library(path: Path):
    return driver_for(path).open(path)


def describe(path: Path) -> dict:
    """Describe a library path using whichever adapter recognises it.

    Used for the picker's "last opened" shortcut, which must still render when
    the file has since been moved or deleted — so an unrecognised path gets a
    plain description with ``exists`` false rather than an error.
    """
    try:
        return driver_for(path).describe(path)
    except (LibraryNotSupported, OSError):
        pass
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    return {
        "path": str(path),
        "label": path.name,
        "version": None,
        "modified": None,
        "exists": exists,
    }


def detect_all() -> list:
    """Every library found in the platforms' default install locations."""
    out: list = []
    for d in _DRIVERS:
        try:
            out.extend(d.detect())
        except OSError:
            continue
    return out


def browsable_suffixes() -> tuple[str, ...]:
    """File extensions the in-app browser should show, across all platforms."""
    seen: list[str] = []
    for d in _DRIVERS:
        for suffix in d.suffixes:
            if suffix not in seen:
                seen.append(suffix)
    return tuple(seen)
