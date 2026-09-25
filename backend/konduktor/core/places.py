"""Where a person keeps things on this computer.

Two jobs, both of them "ask the OS rather than assume":

  * **Mounted volumes.** Where a drive appears is per-OS and nothing else in the
    app can work it out. This lives here rather than in the OneLibrary adapter —
    which is where it grew, because that is the platform whose libraries ARE
    drives — because "where does this OS mount things" is not adapter knowledge.
    The file browser needs the same answer, and two scanners would drift.

  * **The user's own folders.** `platformdirs` answers these, and hardcoding
    them would be wrong on every OS in a different way: `~/Videos` is `Movies`
    on macOS, Linux's are XDG user-dirs which are user-configurable AND
    localised, and Windows Known Folders are routinely **relocated by OneDrive**
    so Documents and Desktop are often not under the home directory at all.

A place that does not exist is never returned. A file browser offering a Music
folder that is not there reads as breakage, whereas its absence reads as an
accurate description of the machine.
"""
from __future__ import annotations

import logging
import os
import string
import sys
from pathlib import Path

import platformdirs

log = logging.getLogger(__name__)


#: Entries an OS writes into a volume or folder BY ITSELF — on mount, on
#: indexing, or when Finder/Explorer merely browses it. They say nothing about
#: whether a person keeps things there, so "is this folder empty?" must not
#: count them: a freshly formatted stick mounted once on a Mac is not empty to
#: `iterdir()`, and treating it as occupied made its root unexportable.
#: Compared case-insensitively — Windows' names are, and FAT/exFAT sticks are.
OS_HOUSEKEEPING = frozenset(
    name.lower()
    for name in (
        # macOS
        ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems",
        ".DocumentRevisions-V100", ".DS_Store", ".apdisk",
        ".com.apple.timemachine.donotpresent",
        # Windows
        "System Volume Information", "$RECYCLE.BIN", "desktop.ini", "Thumbs.db",
    )
)


def is_os_housekeeping(name: str) -> bool:
    """Whether a directory entry is the OS's bookkeeping rather than the user's.

    `._*` are AppleDouble files: macOS writes one beside every file it touches
    on a volume that cannot hold extended attributes (FAT, exFAT).
    """
    return name.lower() in OS_HOUSEKEEPING or name.startswith("._")


def mount_points() -> list[Path]:
    """Every mounted volume's root directory, for this OS.

    Only the ROOTS: a drive can hold tens of thousands of files, and walking a
    slow USB stick to answer "what is plugged in" is exactly the kind of thing
    that makes an app feel broken.
    """
    bases: list[Path] = []
    if sys.platform == "darwin":
        bases.append(Path("/Volumes"))
    elif sys.platform.startswith("linux"):
        bases.extend([Path("/media"), Path("/mnt"), Path("/run/media")])
        user = os.environ.get("USER")
        if user:
            bases.append(Path("/media") / user)
            bases.append(Path("/run/media") / user)

    roots: list[Path] = []
    for base in bases:
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


def _boot_device() -> int | None:
    try:
        return Path("/").stat().st_dev
    except OSError:
        return None


def is_boot_volume(path: Path) -> bool:
    """Whether `path` is the volume the OS itself booted from.

    macOS lists the startup disk in `/Volumes` alongside real removable media,
    so a "Drives" list that does not filter it leads with a path back to where
    the user already is. Compared by DEVICE id rather than by name, because
    `Macintosh HD` is only the default name and is freely renamed.
    """
    boot = _boot_device()
    if boot is None:
        return False
    try:
        return path.stat().st_dev == boot
    except OSError:
        return False


def volumes(*, include_boot: bool = False) -> list[Path]:
    """Mounted volumes, newest-mounted order not guaranteed. Sorted by name."""
    found = [v for v in mount_points() if include_boot or not is_boot_volume(v)]
    return sorted(set(found), key=lambda p: p.name.lower())


# The user's own folders, in the order a file browser should show them. Music
# sits high because this is a DJ app: it is the likeliest destination by far.
_USER_DIRS: tuple[tuple[str, str, str], ...] = (
    ("home", "Home", ""),
    ("music", "Music", "user_music_dir"),
    ("desktop", "Desktop", "user_desktop_dir"),
    ("documents", "Documents", "user_documents_dir"),
    ("downloads", "Downloads", "user_downloads_dir"),
)


def user_places() -> list[dict]:
    """The user's own folders that actually exist on this machine."""
    out: list[dict] = []
    seen: set[str] = set()
    for kind, name, getter in _USER_DIRS:
        try:
            path = Path.home() if not getter else Path(getattr(platformdirs, getter)())
            if not path.is_dir():
                continue
            key = str(path)
            # Windows can resolve two Known Folders to one place; listing it
            # twice under different names would just look like a bug.
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "name": name, "path": key})
        except (OSError, AttributeError):
            continue
    return out


def volume_places() -> list[dict]:
    """Mounted drives, named as the user sees them."""
    out: list[dict] = []
    for vol in volumes():
        try:
            out.append({"kind": "volume", "name": vol.name or str(vol), "path": str(vol)})
        except OSError:
            continue
    return out
