"""Traktor LOCATION <-> OS path conversion.

A Traktor ``<LOCATION>`` is a ``(VOLUME, DIR, FILE)`` triple using ``/:`` as its
directory separator, with the boot volume spelled ``Macintosh HD``. These two
functions are exact inverses of each other and are the only place that
convention is encoded — keep them together, and keep them out of ``core``.
"""
from __future__ import annotations

import re
from pathlib import Path

_WIN_DRIVE = re.compile(r"^([A-Za-z]:)[\\/](.*)$")
_MAC_VOLUME = re.compile(r"^/Volumes/([^/]+)/(.*)$")


def resolve_path(volume: str | None, dir_: str | None, file: str | None) -> Path:
    """Map a Traktor LOCATION to an OS path.

    Paths are trusted (per project decision): boot volume 'Macintosh HD' -> '/',
    a Windows drive like 'X:' stays a drive path, other volumes -> /Volumes/<v>.
    """
    d = (dir_ or "").replace("/:", "/")
    name = file or ""
    vol = volume or ""
    if vol.endswith(":"):  # Windows drive letter, e.g. "X:"
        return Path(vol + d + name)
    if vol and vol != "Macintosh HD":  # other mounted volume (macOS/*nix)
        return Path("/Volumes") / vol / (d.lstrip("/") + name)
    return Path(d + name)  # boot volume


def _traktor_dir(posix_body: str) -> str:
    """Convert a POSIX directory body (leading+trailing '/', e.g. '/Music/')
    to Traktor's '/:'-segmented form ('/:Music/:')."""
    return posix_body.replace("/", "/:")


def os_path_to_location(os_path: Path) -> tuple[str, str, str]:
    """Inverse of ``file_tags.resolve_path``: build a Traktor
    ``(volume, dir, file)`` from an OS path, for the platform implied by the
    path's shape. Round-trips with ``resolve_path``.

    Used by write-back only; the produced LOCATION is valid for the OS whose
    path shape it matches (Windows drive, macOS ``/Volumes`` mount, or a POSIX
    boot path -> ``Macintosh HD``).
    """
    # Derive the filename from the string (not Path.name) so a Windows-style
    # path is handled even when this runs on a POSIX host, and vice-versa.
    s = str(os_path)

    m = _WIN_DRIVE.match(s)
    if m:
        volume = m.group(1)  # e.g. "E:"
        body = m.group(2).replace("\\", "/")  # "Music/x.mp3"
        name = body.rsplit("/", 1)[-1]
        dir_body = "/" + body[: len(body) - len(name)]  # "/Music/"
        return volume, _traktor_dir(dir_body), name

    m = _MAC_VOLUME.match(s)
    if m:
        volume = m.group(1)  # mounted volume name
        body = m.group(2)  # "Music/x.mp3"
        name = body.rsplit("/", 1)[-1]
        dir_body = "/" + body[: len(body) - len(name)]  # "/Music/"
        return volume, _traktor_dir(dir_body), name

    # POSIX boot volume, e.g. "/Users/you/Music/x.mp3".
    name = s.rsplit("/", 1)[-1]
    dir_body = s[: len(s) - len(name)]  # "/Users/you/Music/"
    return "Macintosh HD", _traktor_dir(dir_body), name
