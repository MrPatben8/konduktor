"""The retained native model for a OneLibrary drive.

Owns the open ``exportLibrary.db`` and the drive layout around it, and is the
only place a ``pyrekordbox.devicelib_plus`` row or an ANLZ tag is touched.

The database is SQLCipher-encrypted SQLite, like Rekordbox's ``master.db``, but
with a **different and universal key**: every OneLibrary drive from every vendor
is encrypted with the same one, which is what makes the format readable across
brands at all. `pyrekordbox` carries it, so nothing here needs to know it.

**Analysis files are parsed lazily, and that is a measurement, not a
preference.** The beatgrid and the cues both live in the per-track ANLZ files,
and parsing one costs ~2 ms for the `.DAT` but ~23 ms for the `.EXT`, which
carries the colour waveforms as well. Reading every track's `.EXT` at open would
cost ~23 s on a 1,000-track drive — so it happens on demand and is cached, the
same bargain the Rekordbox adapter strikes for its grids. The visible
consequence is that cue and marker counts are approximate in the library table
until a track's cues are actually read; see `projection.to_track`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ...core.adapter import LibraryNotSupported, NotFound
from ...core.model import CuePoint
from . import beatgrid, cues as cue_reader
from .layout import DriveLayout

log = logging.getLogger(__name__)


class OneLibraryStore:
    def __init__(self, path: Path):
        layout = DriveLayout.locate(Path(path))
        if layout is None:
            raise LibraryNotSupported(f"No OneLibrary database found at {path}")
        self.layout = layout
        self.path = layout.database
        self._db = None
        # Parsed ANLZ, keyed by track id. Two caches because the two files cost
        # an order of magnitude apart: a grid read must not drag in the .EXT.
        self._dat_cache: dict[str, object | None] = {}
        self._ext_cache: dict[str, object | None] = {}
        self._content_cache: list | None = None
        self._by_id: dict[str, object] | None = None
        self._load()

    # ---- open / close ----------------------------------------------------
    def _load(self) -> None:
        try:
            from pyrekordbox.devicelib_plus import DeviceLibraryPlus
        except ImportError as ex:  # pragma: no cover — dependency missing
            raise LibraryNotSupported(
                "Reading OneLibrary drives needs pyrekordbox with devicelib_plus"
            ) from ex
        try:
            self._db = DeviceLibraryPlus(str(self.path))
        except Exception as ex:  # noqa: BLE001 — surfaced as a clean 4xx
            raise LibraryNotSupported(f"Could not open OneLibrary database: {ex}") from ex
        self._dat_cache.clear()
        self._ext_cache.clear()
        self._content_cache = None
        self._by_id = None

    def close(self) -> None:
        """Release the database AND the underlying file handle.

        Disposing the engine matters more here than anywhere else: the library
        is on a REMOVABLE drive, and a lingering handle is what makes a stick
        refuse to eject. `DeviceLibraryPlus.close()` only ends the session, so
        the engine is disposed explicitly — the same gap the Rekordbox store
        had to close for `master.db`.
        """
        db, self._db = self._db, None
        if db is None:
            return
        try:
            db.close()
        except Exception:  # noqa: BLE001 — closing must not raise
            log.debug("OneLibrary session close failed", exc_info=True)
        engine = getattr(db, "engine", None)
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                log.debug("OneLibrary engine dispose failed", exc_info=True)

    def _require_db(self):
        if self._db is None:
            raise LibraryNotSupported("The OneLibrary database is closed")
        return self._db

    # ---- identity --------------------------------------------------------
    @property
    def device_name(self) -> str | None:
        """The drive's own name, which is NOT the volume label.

        Stored in `property.deviceName` and often blank — rekordbox leaves it
        empty unless the user names the device — so the mount point's name is
        the fallback a person will actually recognise.
        """
        try:
            row = self._require_db().get_property().first()
        except Exception:  # noqa: BLE001
            return None
        name = (getattr(row, "deviceName", "") or "").strip() if row is not None else ""
        return name or self.layout.root.name or None

    @property
    def db_version(self) -> str | None:
        try:
            row = self._require_db().get_property().first()
        except Exception:  # noqa: BLE001
            return None
        value = getattr(row, "dbVersion", None) if row is not None else None
        return str(value) if value else None

    # ---- tracks ----------------------------------------------------------
    def iter_content(self) -> list:
        """Every track row, cached for the life of the open library."""
        if self._content_cache is None:
            rows = list(self._require_db().get_content())
            self._content_cache = rows
            self._by_id = {self.track_id(r): r for r in rows}
        return self._content_cache

    @staticmethod
    def track_id(row) -> str:
        """A track's stable id: its DRIVE-RELATIVE path.

        Not `content_id`, which is a row number rekordbox reassigns from 1 every
        time it rewrites the drive — so it would change under a user who merely
        re-exported, breaking every playlist reference Konduktor had remembered.
        The path is stable, unique on the drive, and is also the key a future
        import would match against a Traktor collection, so it is the honest
        identity here.
        """
        return str(getattr(row, "path", "") or f"content:{getattr(row, 'content_id', '')}")

    def content(self, track_id: str):
        self.iter_content()
        row = (self._by_id or {}).get(str(track_id))
        if row is None:
            raise NotFound(f"No track {track_id!r} on this OneLibrary drive")
        return row

    def audio_path(self, track_id: str) -> Path | None:
        return self.layout.resolve(getattr(self.content(track_id), "path", None))

    def all_audio_paths(self) -> list[str]:
        out: list[str] = []
        for row in self.iter_content():
            resolved = self.layout.resolve(getattr(row, "path", None))
            if resolved is not None:
                out.append(str(resolved))
        return out

    # ---- analysis files --------------------------------------------------
    def _anlz(self, track_id: str, *, extended: bool):
        """The parsed `.DAT` or `.EXT` for a track, or None if unreadable.

        A missing or corrupt analysis file is a normal state on a real drive —
        an unanalysed track has none — so it is cached as None and never raised.
        """
        cache = self._ext_cache if extended else self._dat_cache
        key = str(track_id)
        if key in cache:
            return cache[key]

        cache[key] = None
        row = self.content(track_id)
        dat = self.layout.resolve(getattr(row, "analysisDataFilePath", None))
        if dat is None:
            return None
        target = self.layout.extended_anlz(dat) if extended else dat
        try:
            if not target.is_file():
                return None
            from pyrekordbox.anlz import AnlzFile

            cache[key] = AnlzFile.parse_file(str(target))
        except Exception:  # noqa: BLE001 — a bad file must not break browsing
            log.debug("Could not parse ANLZ %s", target, exc_info=True)
            return None
        return cache[key]

    def anlz_grid(self, track_id: str) -> tuple[list[float], list[float]] | None:
        """The per-beat ``(times, bpms)`` from the track's `PQTZ` tag.

        `PQTZ` is in the `.DAT`, which is the cheap file — a grid read does not
        pay for the `.EXT`'s waveforms.
        """
        anlz = self._anlz(track_id, extended=False)
        if anlz is None:
            return None
        return beatgrid.beats_from_pqtz(anlz)

    def cues(self, track_id: str) -> list[CuePoint]:
        """The track's cues, already generic.

        Unusually for a store, this returns generic types rather than native
        rows: the native shape is a pair of parsed tag containers whose entries
        only mean anything once merged across two files, and handing that to the
        projection would put ANLZ knowledge on both sides of the boundary.
        """
        return cue_reader.cues_from_anlz(
            self._anlz(track_id, extended=False),
            self._anlz(track_id, extended=True),
        )

    # ---- playlists -------------------------------------------------------
    def playlists(self) -> list:
        return list(self._require_db().get_playlist())

    def count_playlists(self) -> int:
        try:
            return self._require_db().get_playlist().count()
        except Exception:  # noqa: BLE001
            return 0

    def playlist_song_ids(self, node_id: str) -> list[str]:
        """Track ids in a playlist, in the order the drive stores them.

        `playlist_content` joins on `content_id`, but Konduktor's track id is the
        path (see `track_id`), so the row numbers are translated here rather than
        leaking a second notion of identity upward.
        """
        try:
            pid = int(node_id)
        except (TypeError, ValueError):
            return []
        self.iter_content()
        by_content_id = {
            int(getattr(r, "content_id", -1)): self.track_id(r) for r in self.iter_content()
        }
        rows = [
            r
            for r in self._require_db().get_playlist_content()
            if int(getattr(r, "playlist_id", -1)) == pid
        ]
        rows.sort(key=lambda r: int(getattr(r, "sequenceNo", 0) or 0))
        out: list[str] = []
        for r in rows:
            track_id = by_content_id.get(int(getattr(r, "content_id", -1)))
            if track_id is not None:
                out.append(track_id)
        return out
