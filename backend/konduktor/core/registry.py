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


def driver_by_platform(platform: str) -> LibraryDriver:
    """The driver for a platform name, e.g. ``"traktor"``."""
    for d in _DRIVERS:
        if d.platform == platform:
            return d
    raise LibraryNotSupported(f"No adapter registered for platform {platform!r}")


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


def detect_for(platform: str) -> list:
    """Every library one platform has right now, best first.

    The picker asks which platform FIRST, so detection is scoped to the answer.
    Unscoped detection (``detect_all``) concatenates drivers in registration
    order, which made "the best candidate" mean "whatever the first-registered
    driver found" — with a removable drive registered first, a plugged-in stick
    could be offered as the user's collection.
    """
    try:
        return driver_by_platform(platform).detect()
    except (LibraryNotSupported, OSError):
        return []


def detect_all() -> list:
    """Every library found in the platforms' default install locations."""
    out: list = []
    for d in _DRIVERS:
        try:
            out.extend(d.detect())
        except OSError:
            continue
    return out


def browsable_suffixes(platform: str | None = None) -> tuple[str, ...]:
    """File extensions the in-app browser should show, for one platform or all.

    A platform that selects a DIRECTORY contributes nothing, even though it has
    suffixes: its libraries are not the thing a user picks, so listing them
    would offer a file the browser's Open cannot act on.
    """
    try:
        targets = [driver_by_platform(platform)] if platform else _DRIVERS
    except LibraryNotSupported:
        targets = []
    seen: list[str] = []
    for d in targets:
        if getattr(d, "selects", "file") != "file":
            continue
        for suffix in d.suffixes:
            if suffix not in seen:
                seen.append(suffix)
    return tuple(seen)
