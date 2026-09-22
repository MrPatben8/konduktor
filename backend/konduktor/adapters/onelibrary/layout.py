"""Where things live on a OneLibrary drive.

A OneLibrary library is a **removable drive**, not a file, and that is the one
structural fact that separates this adapter from every other one. Everything the
library refers to — the audio, the analysis files, the artwork — is stored
DRIVE-RELATIVE (``/Contents/Loopmasters/Demo Track 1.mp3``), because the same
stick must resolve on a CDJ, on macOS and on Windows, where it is mounted at
three different places. Nothing inside the database is an absolute host path.

So resolving a stored path means joining it to the mount point, and that is what
this module exists to do. It is kept separate from the store because it is also
the knowledge a future *export* would need to lay a drive out from nothing.

Two layout details are measured from a real rekordbox 7 export rather than
assumed:

  * the database is at ``PIONEER/rekordbox/exportLibrary.db``, alongside — not
    instead of — the legacy ``export.pdb``. Both formats coexist on one drive,
    and newer players prefer OneLibrary when they find it.
  * analysis files sit under ``PIONEER/USBANLZ/P0<xx>/<8 hex>/ANLZ0000.*``,
    where the two directory levels are a hash of the track's path. Konduktor
    never has to compute that hash to READ, because ``content.analysisDataFilePath``
    stores the resulting path outright — it would only matter for writing.

``.PIONEER`` is accepted as well as ``PIONEER``. Traktor's own OneLibrary writer
looks for both, so a drive written by it may use the dot-prefixed (hidden) form.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# In preference order: a drive may legitimately carry either spelling, and the
# visible one is what rekordbox itself writes.
PIONEER_DIRS = ("PIONEER", ".PIONEER")
DB_SUBPATH = Path("rekordbox") / "exportLibrary.db"

# The analysis file the database points at is the `.DAT`; its siblings differ
# only by extension. `.EXT` carries the complete cue list and the colour
# waveforms, `.2EX` the newer high-resolution ones.
ANLZ_EXTENDED = ".EXT"


@dataclass(frozen=True)
class DriveLayout:
    """A located OneLibrary drive: its mount point and its database."""

    root: Path
    database: Path

    @classmethod
    def locate(cls, path: Path) -> "DriveLayout | None":
        """Find the layout for `path`, or None if it is not a OneLibrary drive.

        Accepts either end of the drive deliberately: the MOUNT POINT, which is
        what discovery finds and what a user picks in a file browser, or the
        ``exportLibrary.db`` ITSELF, which is what someone reaches by drilling
        into the folder or by re-opening a remembered path. Both name the same
        library, and refusing one of them would make the picker and the
        last-opened shortcut disagree about what a valid library is.
        """
        path = Path(path)
        try:
            if path.is_file():
                # .../PIONEER/rekordbox/exportLibrary.db -> the drive is 3 up.
                if path.name.lower() != "exportLibrary.db".lower():
                    return None
                root = path.parent.parent.parent
                if path.parent.parent.name not in PIONEER_DIRS:
                    return None
                return cls(root=root, database=path)
            if path.is_dir():
                for pioneer in PIONEER_DIRS:
                    db = path / pioneer / DB_SUBPATH
                    if db.is_file():
                        return cls(root=path, database=db)
        except OSError:
            return None
        return None

    def resolve(self, drive_relative: str | None) -> Path | None:
        """A stored drive-relative path as a real path on this host.

        Stored paths are POSIX-style and lead with a slash. That leading slash is
        NOT a host root — treating it as one is the mistake this method exists to
        prevent, since ``Path('/Volumes/X') / '/Contents/a.mp3'`` silently
        discards the mount point and yields ``/Contents/a.mp3``.
        """
        if not drive_relative:
            return None
        relative = str(drive_relative).replace("\\", "/").lstrip("/")
        if not relative:
            return None
        return self.root / relative

    def extended_anlz(self, dat_path: Path) -> Path:
        """The `.EXT` beside a given `.DAT`.

        Cues are read from the `.EXT`, not the `.DAT` the database points at:
        see `cues.py` for why that distinction is load-bearing rather than a
        preference.
        """
        return dat_path.with_suffix(ANLZ_EXTENDED)
