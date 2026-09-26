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


# ---- local vs external drives -------------------------------------------------
#
# Only the boot disk is identifiable everywhere. Whether ANOTHER volume is an
# internal SSD or a USB stick needs the OS's own answer, and where that answer is
# not available (or not trustworthy — Windows reports most USB SSDs as "fixed")
# a volume is filed as EXTERNAL. Guessing "local" wrongly would present a stick
# that is about to be unplugged as a permanent home for referenced tracks.

# Keyed by (path, st_dev): a volume's answer does not change while it is mounted,
# and `diskutil` is a subprocess — too slow to run for every drive on every poll.
_mac_info_cache: dict[tuple[str, int], dict] = {}


def _mac_info(path: Path) -> dict:
    import plistlib
    import subprocess

    try:
        key = (str(path), path.stat().st_dev)
    except OSError:
        return {}
    if key not in _mac_info_cache:
        try:
            out = subprocess.run(
                ["diskutil", "info", "-plist", str(path)],
                capture_output=True, timeout=5, check=True,
            ).stdout
            _mac_info_cache[key] = plistlib.loads(out)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
            _mac_info_cache[key] = {}
    return _mac_info_cache[key]


def is_internal(path: Path) -> bool:
    """Whether a non-boot volume is a drive built into this computer."""
    if sys.platform != "darwin":
        return False
    info = _mac_info(path)
    return (
        bool(info.get("Internal"))
        and not info.get("Ejectable")
        and not info.get("RemovableMediaOrExternalDevice")
        and not info.get("RemovableMedia")
    )


def _is_disk_image(path: Path) -> bool:
    """A mounted .dmg (an installer, usually) — a mount, but not a drive."""
    return sys.platform == "darwin" and _mac_info(path).get("BusProtocol") == "Disk Image"


def drives() -> list[dict]:
    """Every drive, split into `local` (boot + internal) and `external`.

    The boot disk's path is the filesystem root, not its `/Volumes` alias: on
    macOS `/Volumes/Macintosh HD` is a link back to `/`, and browsing through it
    would show every path twice over.
    """
    out: list[dict] = []
    boot_seen = False
    for vol in sorted(set(mount_points()), key=lambda p: p.name.lower()):
        try:
            if is_boot_volume(vol):
                if boot_seen:
                    continue
                boot_seen = True
                root = Path(vol.anchor) if sys.platform == "win32" else Path("/")
                out.append({"name": vol.name or str(vol), "path": str(root), "kind": "local"})
                continue
            if _is_disk_image(vol):
                continue
            kind = "local" if is_internal(vol) else "external"
            out.append({"name": vol.name or str(vol), "path": str(vol), "kind": kind})
        except OSError:
            continue
    if not boot_seen and sys.platform != "win32":
        # Linux mounts nothing for the root filesystem under /media.
        out.insert(0, {"name": "Computer", "path": "/", "kind": "local"})
    return out


# ---- what a folder listing should show ----------------------------------------

_UF_HIDDEN = 0x8000  # macOS/BSD `chflags hidden`: /usr, /bin, /Volumes, …
_WIN_HIDDEN = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def is_hidden(entry: os.DirEntry | Path) -> bool:
    """Whether a file browser should leave this entry out.

    macOS hides its system folders by FLAG, not by name, so a list of names to
    skip would be both incomplete and wrong on a renamed volume. The flag is what
    Finder itself reads.
    """
    name = entry.name
    if name.startswith(".") or is_os_housekeeping(name):
        return True
    try:
        st = entry.stat(follow_symlinks=False) if isinstance(entry, os.DirEntry) else entry.lstat()
    except OSError:
        return True
    if getattr(st, "st_flags", 0) & _UF_HIDDEN:
        return True
    if getattr(st, "st_file_attributes", 0) & _WIN_HIDDEN:
        return True
    return False


def subfolders(path: Path) -> list[Path]:
    """The visible folders directly inside `path`, sorted as Finder would."""
    out: list[Path] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=True) and not is_hidden(entry):
                        out.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name.lower())
