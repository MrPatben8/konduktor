"""Per-collection OS-path prefix remapping.

A Traktor collection stores absolute audio-file paths. When the same collection
is used on another machine (a portable USB that mounts elsewhere, a relocated
library), those paths aren't reachable. A ``PathMapping`` translates a stored
path prefix (``from_prefix``) to a local one (``to_prefix``).

This module is intentionally pure (no I/O beyond ``~`` expansion) so it can be
unit-tested and reused by both the runtime resolver (``PlaylistStore``) and the
write-back operation. See ``file_tags.resolve_path`` for the LOCATION -> OS path
step this composes with.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_WIN_DRIVE = re.compile(r"^([A-Za-z]:)[\\/](.*)$")
_MAC_VOLUME = re.compile(r"^/Volumes/([^/]+)/(.*)$")


def _norm(prefix: str | None) -> str:
    """Normalize a prefix for matching: expand ``~``, strip a trailing slash."""
    if not prefix:
        return ""
    p = os.path.expanduser(prefix.strip())
    # Strip a single trailing separator (both / and \) but keep a bare root.
    if len(p) > 1:
        p = p.rstrip("/\\")
    return p


@dataclass(frozen=True)
class PathMapping:
    """A single ``from_prefix`` -> ``to_prefix`` OS-path prefix substitution."""

    from_prefix: str = ""
    to_prefix: str = ""

    @classmethod
    def make(cls, from_prefix: str | None, to_prefix: str | None) -> "PathMapping":
        return cls(_norm(from_prefix), _norm(to_prefix))

    @property
    def empty(self) -> bool:
        return not self.from_prefix or not self.to_prefix

    def matches(self, os_path: Path) -> bool:
        """True if ``os_path`` is at or under ``from_prefix`` (segment-aware,
        so ``/a/b`` matches ``/a/b`` and ``/a/b/c`` but not ``/a/bc``)."""
        if self.empty:
            return False
        s = str(os_path)
        if s == self.from_prefix:
            return True
        return s.startswith(self.from_prefix + os.sep) or s.startswith(self.from_prefix + "/")

    def apply(self, os_path: Path) -> Path:
        """Rebase ``os_path`` from ``from_prefix`` onto ``to_prefix``. Returns the
        path unchanged when it doesn't match (non-matching tracks untouched)."""
        if not self.matches(os_path):
            return os_path
        rest = str(os_path)[len(self.from_prefix):]
        # `rest` starts with a separator (or is empty when the path == from_prefix).
        return Path(self.to_prefix + rest)


def common_dir_prefix(paths: list[str]) -> str:
    """Longest directory prefix shared by all ``paths`` (segment-aware).

    Operates on each path's parent directory, so the result is always a folder
    (never a partial filename), and degrades to ``""`` when the paths share no
    common root. Used to auto-suggest the ``from`` prefix in the remap editor.
    """
    if not paths:
        return ""
    dirs = [p.replace("\\", "/").rsplit("/", 1)[0] for p in paths]
    common: list[str] = []
    for segs in zip(*(d.split("/") for d in dirs)):
        if len(set(segs)) == 1:
            common.append(segs[0])
        else:
            break
    return "/".join(common)


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
