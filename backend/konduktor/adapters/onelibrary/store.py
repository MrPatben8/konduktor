"""The retained native model for a OneLibrary drive.

Owns the drive's ``exportLibrary.db`` and the layout around it, and is the only
place a ``pyrekordbox.devicelib_plus`` row or an ANLZ tag is touched.

The database is SQLCipher-encrypted SQLite, like Rekordbox's ``master.db``, but
with a **different and universal key**: every OneLibrary drive from every vendor
is encrypted with the same one, which is what makes the format readable across
brands at all. `pyrekordbox` carries it, so nothing here needs to know it.

**Konduktor never opens the database ON the drive.** It works on a COPY in
app-data (`_working`), taken at open and written back whole at Save:

  * an unsaved edit lives in an open transaction on the copy, so nothing reaches
    the stick before Save — the same "edit in memory, Save writes" contract every
    library keeps, without a long transaction spilling pages into a file on a
    drive that may be unplugged mid-session;
  * the stick holds no open handle, so it can eject (a held handle is what made
    an earlier version's drives refuse to);
  * a save replaces the file by RENAME, so a stick pulled mid-save is left with
    the old library or the new one, never half of each;
  * the drive's file is fingerprinted at open, and Save refuses if something else
    (rekordbox) has written it since, rather than silently undoing that work.

**Analysis files are parsed lazily, and that is a measurement, not a
preference.** The beatgrid and the cues both live in the per-track ANLZ files,
and parsing one costs ~2 ms for the `.DAT` but ~23 ms for the `.EXT`, which
carries the colour waveforms as well. Reading every track's `.EXT` at open would
cost ~23 s on a 1,000-track drive — so it happens on demand and is cached, the
same bargain the Rekordbox adapter strikes for its grids. The visible
consequence is that cue and marker counts are approximate in the library table
until a track's cues are actually read; see `projection.to_track`.

**What rekordbox writes when IT edits a stick was measured** (Goober,
2026-10-01; the editing discussion log has the diff): no update counter moves
(`hasModified`, `cueUpdateCount`, … stay as they were), the `cue` table stays
empty, a new playlist goes on TOP (`sequenceNo` 0, siblings shift), entries are
numbered from 1. The writes below follow that.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from ...core.adapter import FileTagResult, InvalidCommand, LibraryNotSupported, NotFound, SaveOutcome
from ...core.edit_journal import EditJournal
from ...core.grid_edit import ReplaceGridCommands
from ...core.model import CuePoint
from ... import paths
from . import beatgrid, cues as cue_reader
from ..rekordbox import anlz_file, palette, timebase
from ..rekordbox import anlz_writer as W
from ..rekordbox import beatgrid as rb_beatgrid
from .layout import DriveLayout

log = logging.getLogger(__name__)

#: SQLite's companions to a database file. rekordbox leaves both on a stick it
#: has mounted, and a leftover WAL is REPLAYED into whatever database sits beside
#: it — so they are copied with the database at open and deleted at save.
_SIDECARS = ("-wal", "-shm")

#: Backups kept per drive (app-data, never on the stick). A gig stick is real
#: data, unlike a Rekordbox test library; but each backup is a whole database.
_BACKUPS_KEPT = 10

#: Working copies older than this are a crashed session's, and are cleared.
_STALE_WORK_SEC = 24 * 3600

#: `playlist.attribute`.
ATTRIBUTE_PLAYLIST = 0
ATTRIBUTE_FOLDER = 1

_YEAR = re.compile(r"^\d{4}$")
_DATE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def _fingerprint(db: Path) -> tuple:
    """What the drive's database looks like on disk: size + mtime of it and its WAL.

    Enough to notice another app writing it between open and save; not a hash,
    because reading a whole database back over USB to compare costs a save.
    """
    out = []
    for p in (db, *(db.with_name(db.name + s) for s in _SIDECARS)):
        try:
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append((p.name, None, None))
    return tuple(out)


def _fingerprint_file(path: Path) -> tuple:
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def _work_root() -> Path:
    root = paths.app_data_dir() / "onelibrary" / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _backup_root(layout: DriveLayout) -> Path:
    # Keyed by the drive's mount name: a removable library has no sidecar id
    # (library_id.py), and a person looking for a backup knows their stick's name.
    name = re.sub(r"[^\w.-]+", "_", layout.root.name or "drive")
    return paths.app_data_dir() / "onelibrary" / "backups" / name


class OneLibraryStore(ReplaceGridCommands):
    # Metadata a OneLibrary drive can hold, matched to the Rekordbox adapter's
    # set. `producer` and `mix` have no column; `composer`/`lyricist` are
    # different fields and are not mapped onto them approximately.
    _DIRECT_FIELDS = {"title": "title", "comment": "djComment"}
    # Foreign keys into lookup tables, found-or-created by NAME.
    _LOOKUP_FIELDS = {
        "artist": ("Artist", "artist_id_artist"),
        # A remixer IS an artist — same table, different column.
        "remixer": ("Artist", "artist_id_remixer"),
        "album": ("Album", "album_id"),
        "genre": ("Genre", "genre_id"),
        "label": ("Label", "label_id"),
    }
    EDITABLE_FIELDS = set(_DIRECT_FIELDS) | set(_LOOKUP_FIELDS) | {"rating", "release_date"}
    # Written into the audio file's own tags at Save, as rekordbox does when it
    # edits a stick (measured: the title reached the MP3's TIT2, the rating did
    # not). Everything editable except the rating, plus the key `set_key` sets.
    FILE_TAG_FIELDS = (EDITABLE_FIELDS - {"rating"}) | {"key"}

    def __init__(self, path: Path, *, read_only: bool = False):
        layout = DriveLayout.locate(Path(path))
        if layout is None:
            raise LibraryNotSupported(f"No OneLibrary database found at {path}")
        self.layout = layout
        self.path = layout.database
        self.read_only = read_only
        self._db = None
        self._work: Path | None = None
        self._opened_as: tuple | None = None
        self._pdb_seen: tuple | None = None
        self._journal = EditJournal()
        # Parsed ANLZ, keyed by track id. Two caches because the two files cost
        # an order of magnitude apart: a grid read must not drag in the .EXT.
        self._dat_cache: dict[str, object | None] = {}
        self._ext_cache: dict[str, object | None] = {}
        # Analysis files with unsaved edits: path -> (edited file, how the
        # original looked on disk when first edited). The SAME objects sit in
        # the caches above, so every read sees the edit; Save writes them.
        # (`seen` is None for a file an ADDED track brings: it must not exist yet.)
        self._pending_anlz: dict[Path, tuple[anlz_file.AnlzFile, tuple | None]] = {}
        # Adding and removing tracks (see "writes: adding and removing tracks"):
        # incoming copy -> where it will live; files to create; files to delete.
        self._incoming: dict[Path, Path] = {}
        self._new_files: dict[Path, bytes] = {}
        self._doomed: list[Path] = []
        self._content_cache: list | None = None
        self._by_id: dict[str, object] | None = None
        self._load()

    # ---- open / close ----------------------------------------------------
    def _load(self) -> None:
        """Copy the drive's database into app-data and open the copy."""
        try:
            from pyrekordbox.devicelib_plus import DeviceLibraryPlus
        except ImportError as ex:  # pragma: no cover — dependency missing
            raise LibraryNotSupported(
                "Reading OneLibrary drives needs pyrekordbox with devicelib_plus"
            ) from ex
        _clear_stale_work()
        work = Path(tempfile.mkdtemp(prefix="drive-", dir=_work_root()))
        try:
            self._opened_as = _fingerprint(self.path)
            self._pdb_seen = _fingerprint_file(self.device_library) if self.device_library.is_file() else None
            working = work / self.path.name
            shutil.copyfile(self.path, working)
            for suffix in _SIDECARS[:1]:  # the WAL carries data; -shm is rebuilt
                sidecar = self.path.with_name(self.path.name + suffix)
                if sidecar.is_file() and sidecar.stat().st_size:
                    shutil.copyfile(sidecar, working.with_name(working.name + suffix))
            db = DeviceLibraryPlus(str(working))
        except Exception as ex:  # noqa: BLE001 — surfaced as a clean 4xx
            shutil.rmtree(work, ignore_errors=True)
            raise LibraryNotSupported(f"Could not open OneLibrary database: {ex}") from ex
        self._db, self._work = db, work
        self._journal.clear()
        self._pending_anlz.clear()
        self._new_files.clear()
        self._doomed.clear()
        if not self.read_only:
            self._clear_incoming(crashed=True)
        self._dat_cache.clear()
        self._ext_cache.clear()
        self._content_cache = None
        self._by_id = None

    def close(self) -> None:
        """Release the working copy and delete it.

        The drive itself is never held open (see the module docstring); the copy
        is, and `DeviceLibraryPlus.close()` only unregisters events, so the
        session and engine are closed here explicitly.
        """
        db, self._db = self._db, None
        work, self._work = self._work, None
        if not self.read_only:
            self._clear_incoming()
        if db is not None:
            for step in (lambda: db.session.rollback(), lambda: db.session.close(),
                         lambda: db.engine.dispose()):
                try:
                    step()
                except Exception:  # noqa: BLE001 — closing must not raise
                    log.debug("OneLibrary close step failed", exc_info=True)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)

    def discard(self) -> None:
        """Drop every unsaved edit: a fresh copy of the drive's database."""
        self.close()
        self._load()

    @property
    def device_library(self) -> Path:
        """The legacy `export.pdb` beside the OneLibrary database — on most
        rekordbox sticks; rebuilt at Save when present (`device_library.py`)."""
        return self.path.with_name("export.pdb")

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
        return self.resolve_audio(getattr(self.content(track_id), "path", None))

    def resolve_audio(self, drive_relative: str | None) -> Path | None:
        """A stored path on this host — or, for a track added this session whose
        audio has not been published yet, its incoming copy, so it plays."""
        final = self.layout.resolve(drive_relative)
        if final is not None and final not in self._incoming.values():
            return final
        incoming = next((i for i, f in self._incoming.items() if f == final), None)
        return incoming if incoming is not None and incoming.is_file() else final

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        """The track's artwork: `content.image_id` -> `image.path` -> the file.

        The row names the 80 px JPEG (`.../b<n>.jpg`); a `_m` sibling at 240 px
        sits beside it on every export seen, and is what is served — 80 px is a
        CDJ browse thumbnail, too small for anything Konduktor draws.
        """
        image_id = getattr(self.content(track_id), "image_id", None)
        if not image_id:
            return None
        from pyrekordbox.devicelib_plus import models

        row = self._require_db().query(models.Image).filter_by(image_id=image_id).one_or_none()
        small = self.layout.resolve(getattr(row, "path", None)) if row is not None else None
        if small is None:
            return None
        medium = small.with_name(small.stem + "_m" + small.suffix)
        for path in (medium, small):
            if path in self._new_files:  # an added track's, not written yet
                return self._new_files[path], "image/jpeg"
            try:
                return path.read_bytes(), "image/jpeg"
            except OSError:
                continue
        return None

    def all_audio_paths(self) -> list[str]:
        out: list[str] = []
        for row in self.iter_content():
            resolved = self.resolve_audio(getattr(row, "path", None))
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
            # Our own tag reader, not pyrekordbox's: it cannot parse the compact
            # cue entries rekordbox writes when it edits a stick (see anlz_file).
            cache[key] = anlz_file.parse_file(target)
        except Exception:  # noqa: BLE001 — a bad file must not break browsing
            log.debug("Could not parse ANLZ %s", target, exc_info=True)
            return None
        return cache[key]

    def anlz_grid(self, track_id: str) -> tuple[list[float], list[float], list[int]] | None:
        """The per-beat ``(times, bpms, beat_in_bar)`` from the track's `PQTZ`
        tag, in the DECODED time base (see `rekordbox.timebase`).

        `PQTZ` is in the `.DAT`, which is the cheap file — a grid read does not
        pay for the `.EXT`'s waveforms.
        """
        anlz = self._anlz(track_id, extended=False)
        if anlz is None:
            return None
        grid = beatgrid.beats_from_pqtz(anlz)
        if grid is None:
            return None
        off = timebase.offset(self.audio_path(track_id))
        times, bpms, beats = grid
        return [timebase.from_pioneer(t, off) for t in times], bpms, beats

    def cues(self, track_id: str) -> list[CuePoint]:
        """The track's cues, already generic.

        Unusually for a store, this returns generic types rather than native
        rows: the native shape is a pair of parsed tag containers whose entries
        only mean anything once merged across two files, and handing that to the
        projection would put ANLZ knowledge on both sides of the boundary.
        """
        dat = self._anlz(track_id, extended=False)
        ext = self._anlz(track_id, extended=True)
        # A hot cue accepts commands only where there are files to write it to.
        cues = cue_reader.cues_from_anlz(
            dat, ext, editable=not self.read_only and dat is not None and ext is not None,
        )
        # Positions are stored on rekordbox's clock; the generic model is the
        # decoded audio's. A loop keeps its length.
        off = timebase.offset(self.audio_path(track_id))
        return [c.model_copy(update={"start": timebase.from_pioneer(c.start, off)})
                for c in cues] if off else cues

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

    # ---- writes: track metadata -------------------------------------------
    #
    # Each command changes the working copy inside ONE open transaction; Save
    # commits it and puts the file back on the drive (see `save`).
    def _lookup_id(self, model_name: str, name: str) -> int | None:
        """The id of the lookup row called `name`, creating it if needed.

        `nameForSearch` is left NULL, as on every lookup row rekordbox wrote on
        the reference stick (pyrekordbox's own `add_*` would write "").
        """
        if not name:
            return None
        from pyrekordbox.devicelib_plus import models

        model = getattr(models, model_name)
        pk = f"{model.__tablename__}_id"
        db = self._require_db()
        existing = db.query(model).filter_by(name=name).first()
        if existing is not None:
            return int(getattr(existing, pk))
        row = model(name=name, isComplation=0) if model_name == "Album" else model(name=name)
        db.add(row)
        db.flush()
        return int(getattr(row, pk))

    @staticmethod
    def _release(value) -> tuple[int, str]:
        """(`releaseYear`, `releaseDate`) for an edited release date.

        rekordbox writes 0 and '' for "none" and keeps dates as `YYYY-MM-DD`
        text (every date column on the reference stick has that shape).
        """
        text = str(value or "").strip()
        if not text:
            return 0, ""
        if _YEAR.match(text):
            return int(text), ""
        m = _DATE.match(text)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            try:
                return y, datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                pass
        raise InvalidCommand(f"Release date must be a year or YYYY-MM-DD, got {text!r}")

    def _key_id(self, wheel: int | None, mode: str | None) -> int | None:
        """The `key` row for a key, found by what it MEANS: a stick's table holds
        rekordbox's names ("Abm") beside imported tag text ("12A" — the goober
        fixture has both), so a rendered-name match would duplicate a key the
        stick already has. A new key gets rekordbox's own spelling."""
        if wheel is None or mode is None:
            return None
        from pyrekordbox.devicelib_plus import models

        from ..rekordbox.projection import parse_key, render_key

        for k in self._require_db().query(models.Key).all():
            if parse_key(k.name) == (wheel, mode):
                return int(k.key_id)
        return self._lookup_id("Key", render_key(wheel, mode))

    def set_key(self, track_id: str, wheel: int, mode: str) -> None:
        if not (isinstance(wheel, int) and 1 <= wheel <= 12) or mode not in ("major", "minor"):
            raise InvalidCommand(f"Not a key: wheel {wheel!r}, mode {mode!r}")
        db = self._require_db()
        row = self.content(track_id)
        before = row.key_id
        row.key_id = self._key_id(wheel, mode)
        self._journal.record("track", "set", track_id, "key", before, row.key_id)
        # A foreign key: flush + expire, or re-projecting reads the old name.
        db.flush()
        db.session.expire(row)

    def set_track_metadata(self, track_id: str, fields: dict) -> None:
        from sqlalchemy import text

        db = self._require_db()
        row = self.content(track_id)
        for key, value in fields.items():
            if key not in self.EDITABLE_FIELDS:
                continue  # unknown/unsafe fields are ignored, never guessed at
            if key == "rating":
                try:
                    stars = int(value or 0)
                except (TypeError, ValueError):
                    raise InvalidCommand(f"Rating must be a number, got {value!r}") from None
                if not 0 <= stars <= 5:
                    raise InvalidCommand(f"Rating must be 0-5, got {stars}")
                before, new = row.rating, stars
                row.rating = stars
            elif key == "release_date":
                year, date = self._release(value)
                before, new = (row.releaseYear, row.releaseDate), (year, date)
                row.releaseYear = year
                # Raw SQL, not the ORM: pyrekordbox's DateTime column would
                # store `YYYY-MM-DD HH:MM:SS.SSS +00:00`, which is not the
                # format rekordbox writes.
                db.flush()
                db.session.execute(
                    text("UPDATE content SET releaseDate = :d WHERE content_id = :id"),
                    {"d": date, "id": row.content_id},
                )
            elif key in self._DIRECT_FIELDS:
                column = self._DIRECT_FIELDS[key]
                before = getattr(row, column)
                # rekordbox keeps an empty text column as '', not NULL.
                new = "" if value is None else str(value)
                setattr(row, column, new)
            else:
                model_name, column = self._LOOKUP_FIELDS[key]
                before = getattr(row, column)
                new = self._lookup_id(model_name, (value or "").strip())
                setattr(row, column, new)
            self._journal.record("track", "set", track_id, key, before, new)
        # The lookups are FOREIGN KEYS: setting an id does not move the ORM's
        # cached relationship, so re-projecting now would read the OLD name back
        # and report a successful edit as a no-op. Flush, then expire the row.
        db.flush()
        db.session.expire(row)

    # ---- writes: playlists --------------------------------------------------
    def _playlist(self, node_id: str):
        from pyrekordbox.devicelib_plus import models

        try:
            pid = int(node_id)
        except (TypeError, ValueError):
            raise NotFound(f"No playlist {node_id!r}") from None
        row = self._require_db().query(models.Playlist).filter_by(playlist_id=pid).first()
        if row is None:
            raise NotFound(f"No playlist {node_id!r}")
        return row

    def _siblings(self, parent_id: int, *, excluding: int | None = None) -> list:
        from pyrekordbox.devicelib_plus import models

        rows = self._require_db().query(models.Playlist).filter_by(playlist_id_parent=parent_id).all()
        rows = [r for r in rows if r.playlist_id != excluding]
        return sorted(rows, key=lambda r: (int(r.sequenceNo or 0), int(r.playlist_id)))

    def _create_node(self, name: str, parent_id: str | None, attribute: int) -> str:
        from pyrekordbox.devicelib_plus import models

        name = (name or "").strip()
        noun = "folder" if attribute == ATTRIBUTE_FOLDER else "playlist"
        if not name:
            raise InvalidCommand(f"A {noun} needs a name")
        parent = 0
        if parent_id:
            folder = self._playlist(parent_id)
            if int(folder.attribute or 0) != ATTRIBUTE_FOLDER:
                raise InvalidCommand(f"A {noun} can only be created inside a folder")
            parent = int(folder.playlist_id)
        db = self._require_db()
        # MEASURED: rekordbox puts a new playlist at the TOP of its folder —
        # sequenceNo 0, every sibling moved down one. (pyrekordbox's own
        # add_playlist appends at count+1, which is not what rekordbox does.)
        for sibling in self._siblings(parent):
            sibling.sequenceNo = int(sibling.sequenceNo or 0) + 1
        row = models.Playlist(name=name, sequenceNo=0, attribute=attribute,
                              playlist_id_parent=parent, image_id=None)
        db.add(row)
        db.flush()
        self._journal.record("playlist", "create-folder" if attribute == ATTRIBUTE_FOLDER
                             else "create", name)
        return str(row.playlist_id)

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        return self._create_node(name, parent_id, ATTRIBUTE_PLAYLIST)

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        return self._create_node(name, parent_id, ATTRIBUTE_FOLDER)

    def rename_playlist(self, node_id: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise InvalidCommand("A playlist needs a name")
        row = self._playlist(node_id)
        before = row.name
        row.name = name
        self._require_db().flush()
        self._journal.record("playlist", "rename", name, before=before)

    def delete_playlist(self, node_id: str) -> None:
        """Delete a playlist, or a folder WITH everything in it, and close the gap
        its `sequenceNo` leaves among its siblings."""
        from pyrekordbox.devicelib_plus import models

        db = self._require_db()
        row = self._playlist(node_id)
        name, parent = row.name, int(row.playlist_id_parent or 0)
        doomed, frontier = [], [row]
        while frontier:
            node = frontier.pop()
            doomed.append(node)
            frontier.extend(db.query(models.Playlist)
                            .filter_by(playlist_id_parent=node.playlist_id).all())
        for node in doomed:
            for entry in db.query(models.PlaylistContent).filter_by(playlist_id=node.playlist_id).all():
                db.delete(entry)
            db.delete(node)
        db.flush()
        for n, sibling in enumerate(self._siblings(parent)):
            sibling.sequenceNo = n
        db.flush()
        self._journal.record("playlist", "delete", name)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        """Replace a playlist's contents, in order, numbered from 1 (measured)."""
        from pyrekordbox.devicelib_plus import models

        db = self._require_db()
        playlist = self._playlist(node_id)
        if int(playlist.attribute or 0) != ATTRIBUTE_PLAYLIST:
            raise InvalidCommand("Only a plain playlist has an editable track list")
        self.iter_content()
        unknown = [t for t in track_ids if str(t) not in (self._by_id or {})]
        if unknown:
            raise NotFound(f"Unknown track(s): {', '.join(map(str, unknown[:3]))}")
        for entry in db.query(models.PlaylistContent).filter_by(playlist_id=playlist.playlist_id).all():
            db.delete(entry)
        db.flush()
        for n, track_id in enumerate(track_ids, start=1):
            db.add(models.PlaylistContent(playlist_id=playlist.playlist_id,
                                          content_id=int(self._by_id[str(track_id)].content_id),
                                          sequenceNo=n))
        db.flush()
        self._journal.record("playlist", "entries", playlist.name, after=len(track_ids))
        return len(track_ids)

    # ---- writes: cues and the beatgrid (the ANLZ files) ------------------------
    #
    # Both live in the track's analysis files, not the database (the `cue`
    # table stays empty — measured, rekordbox's own edits included). An edit
    # replaces only the cue LISTS it touches, and inside them only the entries
    # it touches: every other entry keeps rekordbox's bytes, compact forms
    # included, as does every other tag of the file. Each step below was checked
    # against what rekordbox 7 wrote when it made the same edit on Goober.
    #
    # Where a hot cue lives (dense, 1-based pads; 0 = memory):
    #   .DAT  PCOB hot   pads 1-3          .EXT  PCOB hot   pads 4+
    #   .DAT  PCOB mem   memory cues       .EXT  PCOB mem   (empty)
    #                                      .EXT  PCO2 hot   EVERY pad, + colour/name
    #                                      .EXT  PCO2 mem   every memory cue
    _DAT_ORDER = ("PPTH", "PVBR", "PQTZ", "PWAV", "PWV2", "PCOB")
    _EXT_ORDER = ("PPTH", "PWV3", "PCOB", "PCO2")
    HOTCUE_SLOTS = 8

    def _anlz_paths(self, track_id: str) -> tuple[Path, Path]:
        dat = self.layout.resolve(getattr(self.content(track_id), "analysisDataFilePath", None))
        exists = lambda p: p in self._pending_anlz or p.is_file()  # noqa: E731
        if dat is None or not exists(dat) or not exists(self.layout.extended_anlz(dat)):
            raise InvalidCommand(
                "This track has no analysis files on the drive, so it has nowhere to "
                "store cues or a beatgrid. Analyse it in rekordbox first."
            )
        return dat, self.layout.extended_anlz(dat)

    def _editing(self, track_id: str) -> tuple[anlz_file.AnlzFile, anlz_file.AnlzFile]:
        """The track's `.DAT` and `.EXT` as EDITABLE copies, registered for Save.

        A file is fingerprinted the first time it is edited, so Save can refuse
        to overwrite a change another app made meanwhile.
        """
        out = []
        for path, cache in zip(self._anlz_paths(track_id), (self._dat_cache, self._ext_cache)):
            pending = self._pending_anlz.get(path)
            if pending is None:
                try:
                    parsed = anlz_file.parse_file(path)
                except (OSError, anlz_file.AnlzError) as ex:
                    raise InvalidCommand(f"Cannot read the analysis file {path.name}: {ex}") from ex
                pending = (parsed, _fingerprint_file(path))
                self._pending_anlz[path] = pending
            cache[str(track_id)] = pending[0]
            out.append(pending[0])
        return out[0], out[1]

    def _offset(self, track_id: str) -> float:
        return timebase.offset(self.audio_path(track_id))

    def _hot_entry(self, ext: anlz_file.AnlzFile, hot: int) -> bytes | None:
        """The pad's `PCO2` entry — the complete one, with colour and name."""
        i = ext.find("PCO2", anlz_file.LIST_HOT)
        if i is None:
            return None
        return next((e for e in anlz_file.raw_entries(ext.tags[i])
                     if anlz_file.entry_hot_cue(e) == hot), None)

    @staticmethod
    def _decoded(entry: bytes) -> anlz_file.CueEntry:
        return anlz_file.decode_entry(entry)

    def _ensure_pco2(self, dat: anlz_file.AnlzFile, ext: anlz_file.AnlzFile) -> None:
        """Give an `.EXT` without `PCO2` both lists, seeded from its `PCOB`s.

        Readers (Konduktor's, and players) prefer `PCO2` the moment it has any
        entry, so writing one holding only the edited pad would hide every other
        pad and memory cue still listed only in `PCOB`. A drive old enough to
        lack it gets the complete lists on the first edit.
        """
        if ext.find("PCO2") is not None:
            return
        for kind in (anlz_file.LIST_HOT, anlz_file.LIST_MEMORY):
            seeded = []
            for f in (dat, ext):
                i = f.find("PCOB", kind)
                for e in (anlz_file.raw_entries(f.tags[i]) if i is not None else []):
                    c = anlz_file.decode_entry(e)
                    seeded.append(W.pcp2_entry(
                        hot_cue=c.hot_cue, kind=c.type, time_ms=c.time,
                        loop_ms=None if c.loop_time == anlz_file.NO_LOOP else c.loop_time,
                        rgb=None, beats=None))
            ext.put("PCO2", W.pco2_tag(kind, seeded), list_kind=kind, after=self._EXT_ORDER)

    def _replace_hot(self, track_id: str, hot: int, pcpt: bytes | None, pcp2: bytes | None) -> None:
        """Take pad `hot` out of every hot list and, if given, put the new entries in.

        A new entry is APPENDED, as rekordbox does (it appended the hot cue it
        added after pads 7 and 6); order within a list carries no meaning.
        """
        dat, ext = self._editing(track_id)
        self._ensure_pco2(dat, ext)
        for f, order, home in ((dat, self._DAT_ORDER, hot <= 3), (ext, self._EXT_ORDER, hot > 3)):
            i = f.find("PCOB", anlz_file.LIST_HOT)
            kept = ([e for e in anlz_file.raw_entries(f.tags[i]) if anlz_file.entry_hot_cue(e) != hot]
                    if i is not None else [])
            if pcpt is not None and home:
                kept.append(pcpt)
            if i is not None or kept:
                f.put("PCOB", W.pcob_tag(anlz_file.LIST_HOT, kept),
                      list_kind=anlz_file.LIST_HOT, after=order)
        i = ext.find("PCO2", anlz_file.LIST_HOT)
        kept = ([e for e in anlz_file.raw_entries(ext.tags[i]) if anlz_file.entry_hot_cue(e) != hot]
                if i is not None else [])
        if pcp2 is not None:
            kept.append(pcp2)
        ext.put("PCO2", W.pco2_tag(anlz_file.LIST_HOT, kept),
                list_kind=anlz_file.LIST_HOT, after=self._EXT_ORDER)

    def _slot(self, slot: int) -> int:
        slot = int(slot)
        if not 0 <= slot < self.HOTCUE_SLOTS:
            raise InvalidCommand(f"Hot cue slot must be 0-{self.HOTCUE_SLOTS - 1}, got {slot}")
        return slot + 1  # ANLZ pads are dense and 1-based

    def set_cue(self, track_id: str, *, slot: int, start_sec: float, cue_type: str,
                length_sec: float = 0.0, name: str | None = None) -> None:
        """Create or replace the hot cue on a pad. A loop is a cue with a length.

        The pad KEEPS its colour (it belongs to the pad, as in the Rekordbox
        adapter), and its name unless one is given — a hand move passes none.
        A new cue is uncoloured, like one the Rekordbox adapter writes.
        """
        from .export import _loop_beats

        hot = self._slot(slot)
        if start_sec < 0:
            raise InvalidCommand("A cue cannot be before the start of the track")
        dat, ext = self._editing(track_id)
        old = self._hot_entry(ext, hot)
        prior = self._decoded(old) if old is not None else None
        code = (prior.color_code or 0) if prior else 0
        rgb = ((prior.color_red, prior.color_green, prior.color_blue)
               if prior and prior.color_red is not None else (0, 0, 0))
        if name is None and prior is not None:
            name = prior.comment or None
        is_loop = cue_type == "loop" and length_sec > 0
        off = self._offset(track_id)
        time_ms = int(round(timebase.to_pioneer(start_sec, off) * 1000))
        loop_ms = int(round(timebase.to_pioneer(start_sec + length_sec, off) * 1000)) if is_loop else None
        grid = self.anlz_grid(track_id)
        beats = (_loop_beats(start_sec, length_sec, list(zip(grid[2], grid[1], grid[0])))
                 if is_loop and grid else None)
        kind = 2 if is_loop else 1
        self._replace_hot(
            track_id, hot,
            W.pcpt_entry(hot_cue=hot, kind=kind, time_ms=time_ms, loop_ms=loop_ms),
            W.pcp2_entry(hot_cue=hot, kind=kind, time_ms=time_ms, loop_ms=loop_ms,
                         rgb=rgb, beats=beats, code=code, comment=name or None),
        )
        self._journal.record("cue", "add" if old is None else "modify", track_id, f"slot:{slot}")

    def _existing(self, track_id: str, slot: int) -> tuple[int, anlz_file.CueEntry]:
        hot = self._slot(slot)
        _dat, ext = self._editing(track_id)
        entry = self._hot_entry(ext, hot)
        if entry is None:
            raise NotFound(f"No cue in slot {slot}")
        return hot, self._decoded(entry)

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> None:
        _hot, cue = self._existing(track_id, slot)
        off = self._offset(track_id)
        start = timebase.from_pioneer(cue.time / 1000.0, off)
        length = 0.0
        if cue_type == "loop":
            if cue.loop_time != anlz_file.NO_LOOP and cue.loop_time > cue.time:
                length = (cue.loop_time - cue.time) / 1000.0
            else:
                raise InvalidCommand("Turning a cue into a loop needs a length — set the loop first")
        self.set_cue(track_id, slot=slot, start_sec=start, cue_type=cue_type, length_sec=length)

    def set_cue_color(self, track_id: str, slot: int, color_code: int | None) -> None:
        """Recolour a pad: ONLY its `PCP2` colour bytes change (as rekordbox does).

        `PCP2` stores the palette code and its RGB; rekordbox draws from the code.
        None is uncoloured (code 0, black), as an uncoloured rekordbox cue is.
        """
        hot, cue = self._existing(track_id, slot)
        code = int(color_code or 0)
        rgb = palette.PALETTE.get(code, (0, 0, 0)) if code else (0, 0, 0)
        _dat, ext = self._editing(track_id)
        i = ext.find("PCO2", anlz_file.LIST_HOT)
        entries = []
        for e in anlz_file.raw_entries(ext.tags[i]):
            if anlz_file.entry_hot_cue(e) == hot:
                # rekordbox's compact 44-byte form has no colour bytes to change.
                e = anlz_file.entry_with_colour(e, code, rgb) or W.pcp2_entry(
                    hot_cue=hot, kind=cue.type, time_ms=cue.time,
                    loop_ms=None if cue.loop_time == anlz_file.NO_LOOP else cue.loop_time,
                    rgb=rgb, beats=cue.loop_numerator or None, code=code,
                    comment=cue.comment or None)
            entries.append(e)
        ext.tags[i] = anlz_file.Tag("PCO2", W.pco2_tag(anlz_file.LIST_HOT, entries))
        self._journal.record("cue", "modify", track_id, f"slot:{slot}")

    def delete_cue(self, track_id: str, slot: int) -> None:
        hot, _cue = self._existing(track_id, slot)
        self._replace_hot(track_id, hot, None, None)
        self._journal.record("cue", "delete", track_id, f"slot:{slot}")

    def hot_slots(self, track_id: str) -> set[int]:
        """Occupied 0-based slots (pending edits included)."""
        return {c.slot for c in self.cues(track_id) if c.role == "hotcue" and c.slot is not None}

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> None:
        """Batch placement (Auto Hotcues). Fills empty slots unless overwriting."""
        taken = self.hot_slots(track_id)
        for cue in cues:
            slot = int(getattr(cue, "slot"))
            if not overwrite and slot in taken:
                continue
            self.set_cue(track_id, slot=slot, start_sec=float(getattr(cue, "start", 0.0)),
                         cue_type=getattr(cue, "type", "cue"),
                         length_sec=float(getattr(cue, "length", 0.0) or 0.0),
                         name=getattr(cue, "name", None))

    # The grid: `PQTZ` in the .DAT, every beat. rekordbox's measured grid edit,
    # reproduced: PQTZ rewritten, `.EXT`'s extended grid PQT2 BLANKED (not
    # recomputed), every loop's beat length cleared to 0/0, `.2EX` untouched,
    # and `content.bpmx100` following the first marker.
    def current_markers(self, track_id: str) -> list:
        grid = self.anlz_grid(track_id)
        return beatgrid.markers_from_beats(*grid) if grid is not None else []

    def _duration(self, track_id: str) -> float:
        length = getattr(self.content(track_id), "length", None)
        if length:
            # Whole seconds rounded down; one more keeps the track's last beat.
            return float(length) + 1.0
        grid = self.anlz_grid(track_id)
        return float(grid[0][-1]) + 1.0 if grid and grid[0] else 0.0

    def replace_grid(self, track_id: str, markers: list) -> None:
        """Set the grid to exactly these markers — the one primitive; every
        marker-level command (`ReplaceGridCommands`) is a read-modify-replace."""
        for m in markers:
            if m.bpm <= 0:
                raise InvalidCommand(f"A beatgrid marker needs a positive tempo, got {m.bpm}")
            if m.start < 0:
                raise InvalidCommand("A beatgrid marker cannot be before the track starts")
        ordered = sorted(markers, key=lambda m: m.start)
        dat, ext = self._editing(track_id)
        off = self._offset(track_id)
        nums, bpms, times = rb_beatgrid.beats_from_markers(ordered, self._duration(track_id))
        dat.put("PQTZ", W.beatgrid_tag([(n, b, timebase.to_pioneer(t, off))
                                        for n, b, t in zip(nums, bpms, times)]),
                after=("PPTH", "PVBR"))
        i = ext.find("PQT2")
        if i is not None:
            ext.tags[i] = anlz_file.Tag("PQT2", W.blank_pqt2(ext.tags[i].data))
        for kind in (anlz_file.LIST_HOT, anlz_file.LIST_MEMORY):
            i = ext.find("PCO2", kind)
            if i is not None:
                entries = [anlz_file.entry_without_loop_beats(e)
                           for e in anlz_file.raw_entries(ext.tags[i])]
                ext.tags[i] = anlz_file.Tag("PCO2", W.pco2_tag(kind, entries))
        self.content(track_id).bpmx100 = int(round(ordered[0].bpm * 100)) if ordered else 0
        self._require_db().flush()
        self._journal.record("grid", "replace" if ordered else "delete", track_id)

    # ---- writes: adding and removing tracks -------------------------------------
    #
    # A stick's audio belongs to its library: added audio is COPIED onto it (a
    # file already on the stick is used where it is), laid out as rekordbox lays
    # a stick out, and a removed track's audio goes with it. Decisions 3, 6 and 7
    # of the editing discussion; layout read off Goober:
    #   Contents/<Artist>/<Album>/<file>  — `UnknownArtist` / `UnknownAlbum` for
    #   an empty field, and a file name cut to 48 characters (seen once:
    #   "…Sell My .mp3"). dateAdded = the day it was added, dateCreated = the
    #   file's own date, search columns NULL, no update counter set.
    #
    # Copies arrive in `<stick>/.konduktor-incoming/` and are moved into place at
    # Save. Analysis files and artwork for an added track are held in memory
    # until then. A crash leaves only the incoming folder, cleared on the next
    # editable open.
    INCOMING = ".konduktor-incoming"
    _NAME_MAX = 48
    _BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

    def audio_home(self) -> Path:
        return self.layout.root

    def _folder_name(self, value: str | None, fallback: str) -> str:
        # FAT/exFAT refuse these characters, and a trailing dot or space.
        name = self._BAD_CHARS.sub("_", (value or "").strip()).rstrip(". ")
        return name or fallback

    def _final_path(self, source: Path, track) -> Path:
        folder = (self.layout.root / "Contents"
                  / self._folder_name(getattr(track, "artist", None), "UnknownArtist")
                  / self._folder_name(getattr(track, "album", None), "UnknownAlbum"))
        stem, suffix = Path(self._BAD_CHARS.sub("_", source.name)).stem, source.suffix
        stem = stem[:max(1, self._NAME_MAX - len(suffix))]
        taken = {p.name.lower() for p in self._incoming.values() if p.parent == folder}
        candidate, n = f"{stem}{suffix}", 1
        while candidate.lower() in taken or (folder / candidate).exists():
            n += 1
            tail = f"-{n}{suffix}"
            candidate = f"{stem[:max(1, self._NAME_MAX - len(tail))]}{tail}"
        return folder / candidate

    def place_audio(self, source: Path, track) -> Path | None:
        """Where the importer must copy `source` — or None, if it is already on
        this stick and can be used where it is (decision 7)."""
        try:
            if Path(source).resolve().is_relative_to(self.layout.root.resolve()):
                return None
        except OSError:
            pass
        final = self._final_path(Path(source), track)
        incoming = self.layout.root / self.INCOMING / f"{uuid.uuid4().hex[:12]}{final.suffix.lower()}"
        self._incoming[incoming] = final
        return incoming

    def final_of(self, audio: Path) -> Path:
        """Where an added track's audio will live (its incoming copy's target, or
        the file itself when it was already on the stick)."""
        return self._incoming.get(Path(audio), Path(audio))

    def drive_relative(self, path: Path) -> str:
        return "/" + Path(path).relative_to(self.layout.root).as_posix()

    def _clear_incoming(self, *, crashed: bool = False) -> None:
        """Delete copies this session made and never published — or, at an
        editable open, whatever a crashed session left (`crashed`)."""
        folder = self.layout.root / self.INCOMING
        if crashed:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            for incoming in self._incoming:
                incoming.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
        self._incoming.clear()

    def _anlz_rel(self, rel: str) -> str:
        """A fresh `/PIONEER/USBANLZ/P0xx/xxxxxxxx/ANLZ0000.DAT` for a track.
        rekordbox's own hash is unpublished; the column is what players follow,
        so the exporter's stable derivation is used, nudged past any clash."""
        from .export import _anlz_dir

        salt = 0
        while True:
            dat = f"{_anlz_dir(rel + ('#' * salt))}/ANLZ0000.DAT"
            path = self.layout.resolve(dat)
            if not path.parent.exists() and path not in self._pending_anlz:
                return dat
            salt += 1

    def _next_id(self, table: str) -> int:
        from sqlalchemy import text

        value = self._require_db().session.execute(text(f"SELECT MAX({table}_id) FROM {table}")).scalar()
        return int(value or 0) + 1

    def _next_image(self) -> int:
        n = self._next_id("image")
        folder = self.layout.root / "PIONEER" / "Artwork" / "00001"
        while any((folder / f"{x}{n}{s}.jpg").exists() or (folder / f"{x}{n}{s}.jpg") in self._new_files
                  for x in "ab" for s in ("", "_m")):
            n += 1
        return n

    def add_track(self, audio: Path, track, *, rel: str, anlz_rel: str,
                  anlz: list[tuple[str, bytes]], jpegs: tuple[bytes, bytes] | None,
                  bpm: float | None, length: float | None) -> str:
        """Insert one track's row, and hold its analysis files and artwork for Save.

        The row is filled the way rekordbox fills one on a stick (every column of
        a Goober row was compared): file facts from the file, lookups found or
        created by name, no update counter, `contentLink`/`analysedBits` as the
        exporter writes them (verified in rekordbox 7 there).
        """
        from sqlalchemy import text

        from .export import ANALYSED_BITS, CONTENT_LINK, _audio_format, _file_type, _kbps, art_files

        if rel in (self._by_id or {}) or any(self.track_id(r) == rel for r in self.iter_content()):
            raise InvalidCommand(f"{rel} is already on this drive's library")
        db = self._require_db()
        final = self.layout.resolve(rel)
        cid = self._next_id("content")
        image_id = None
        if jpegs is not None:
            image_id = self._next_image()
            for path_rel, data in art_files(image_id, jpegs):
                self._new_files[self.layout.resolve(path_rel)] = data
            db.session.execute(text("INSERT INTO image (image_id, path) VALUES (:i, :p)"),
                               {"i": image_id, "p": f"/PIONEER/Artwork/00001/b{image_id}.jpg"})
        try:
            st = Path(audio).stat()
            size, created = st.st_size, datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        except OSError:
            size, created = 0, None
        sample_rate, bit_depth = _audio_format(Path(audio))
        year, released = 0, ""
        try:
            year, released = self._release(getattr(track, "release_date", None))
        except InvalidCommand:
            pass
        lookup = lambda model, value: self._lookup_id(model, (value or "").strip())  # noqa: E731
        db.session.execute(text(
            "INSERT INTO content (content_id, title, titleForSearch, subtitle, bpmx100, length, "
            "trackNo, discNo, artist_id_artist, artist_id_remixer, album_id, genre_id, label_id, "
            "key_id, color_id, image_id, djComment, rating, releaseYear, releaseDate, dateAdded, "
            "dateCreated, path, fileName, fileSize, fileType, bitrate, bitDepth, samplingRate, "
            "isrc, djPlayCount, isHotCueAutoLoadOn, isKuvoDeliverStatusOn, kuvoDeliveryComment, "
            "masterDbId, masterContentId, analysisDataFilePath, analysedBits, contentLink, "
            "hasModified) VALUES (:cid, :title, NULL, '', :bpm, :length, 0, 0, :artist, :remixer, "
            ":album, :genre, :label, :key, 0, :image, :comment, :rating, :year, :released, "
            ":added, :created, :path, :file, :size, :type, :bitrate, :depth, :rate, '', 0, 1, 1, "
            "'', 0, :cid, :anlz, :bits, :link, 0)"), {
                "cid": cid, "title": getattr(track, "title", None) or final.stem,
                "bpm": int(round(bpm * 100)) if bpm else 0, "length": int(length or 0),
                "artist": lookup("Artist", track.artist), "remixer": lookup("Artist", track.remixer),
                "album": lookup("Album", track.album), "genre": lookup("Genre", track.genre),
                "label": lookup("Label", track.label),
                # Rendered from the parsed wheel: a source's "10m" means nothing here.
                "key": self._key_id(track.key_wheel, track.key_mode),
                "image": image_id, "comment": track.comment or "",
                "rating": max(0, min(5, int(track.rating or 0))), "year": year, "released": released,
                "added": datetime.now().strftime("%Y-%m-%d"), "created": created, "path": rel,
                "file": final.name, "size": size, "type": _file_type(final),
                "bitrate": _kbps(track.bitrate) or 0, "depth": bit_depth, "rate": sample_rate,
                "anlz": anlz_rel, "bits": ANALYSED_BITS, "link": CONTENT_LINK,
            })
        db.session.execute(text("UPDATE property SET numberOfContents = numberOfContents + 1"))
        db.flush()
        dat = self.layout.resolve(anlz_rel)
        for suffix, data in anlz:
            path = dat.with_suffix(suffix)
            if suffix == ".2EX":
                self._new_files[path] = data
            else:
                self._pending_anlz[path] = (anlz_file.parse(data), None)
        self._content_cache = None
        self._by_id = None
        self._dat_cache.pop(rel, None)
        self._ext_cache.pop(rel, None)
        self._dat_cache[rel] = self._pending_anlz[dat][0]
        self._ext_cache[rel] = self._pending_anlz[dat.with_suffix(".EXT")][0]
        self._journal.record("track", "add", rel)
        return rel

    def remove_tracks(self, track_ids: list[str]) -> int:
        """Remove tracks from the library and every list that names them; their
        audio, analysis files and (unshared) artwork are deleted at Save
        (decision 3). Not measured against rekordbox — it cannot delete a track
        from a stick — so this removes every row that references the track, and
        nothing else."""
        from sqlalchemy import text

        db = self._require_db()
        removed = 0
        for track_id in dict.fromkeys(map(str, track_ids)):
            row = self.content(track_id)
            cid, image_id = int(row.content_id), row.image_id
            audio = self.layout.resolve(row.path)
            dat = self.layout.resolve(row.analysisDataFilePath)
            # Files: the audio (or its unpublished copy), the analysis files.
            for incoming, final in list(self._incoming.items()):
                if final == audio:
                    incoming.unlink(missing_ok=True)
                    del self._incoming[incoming]
                    audio = None
            if audio is not None:
                self._doomed.append(audio)
            if dat is not None:
                for suffix in (".DAT", ".EXT", ".2EX"):
                    path = dat.with_suffix(suffix)
                    # Pending edits, or an added track's never-written files, go
                    # with it; whatever is already on the stick is deleted at Save.
                    self._pending_anlz.pop(path, None)
                    self._new_files.pop(path, None)
                    if path.exists():
                        self._doomed.append(path)
            # Rows: every table that names the content.
            affected = [r[0] for r in db.session.execute(
                text("SELECT DISTINCT playlist_id FROM playlist_content WHERE content_id = :c"), {"c": cid})]
            for table in ("playlist_content", "history_content", "myTag_content", "cue"):
                db.session.execute(text(f"DELETE FROM {table} WHERE content_id = :c"), {"c": cid})
            for pid in affected:
                rows = db.session.execute(text(
                    "SELECT rowid FROM playlist_content WHERE playlist_id = :p ORDER BY sequenceNo"),
                    {"p": pid}).fetchall()
                for n, (rowid,) in enumerate(rows, start=1):
                    db.session.execute(text("UPDATE playlist_content SET sequenceNo = :n WHERE rowid = :r"),
                                       {"n": n, "r": rowid})
            db.session.execute(text("DELETE FROM content WHERE content_id = :c"), {"c": cid})
            if image_id and not db.session.execute(
                    text("SELECT 1 FROM content WHERE image_id = :i LIMIT 1"), {"i": image_id}).first():
                path = db.session.execute(text("SELECT path FROM image WHERE image_id = :i"),
                                          {"i": image_id}).scalar()
                db.session.execute(text("DELETE FROM image WHERE image_id = :i"), {"i": image_id})
                small = self.layout.resolve(path)
                if small is not None:
                    for letter in ("a", "b"):
                        for tail in ("", "_m"):
                            p = small.with_name(f"{letter}{small.stem[1:]}{tail}{small.suffix}")
                            if self._new_files.pop(p, None) is None and p.exists():
                                self._doomed.append(p)
            db.session.execute(text("UPDATE property SET numberOfContents = numberOfContents - 1"))
            self._dat_cache.pop(track_id, None)
            self._ext_cache.pop(track_id, None)
            self._journal.record("track", "remove", track_id)
            removed += 1
        db.flush()
        db.session.expire_all()
        self._content_cache = None
        self._by_id = None
        return removed

    # ---- save ----------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._journal.dirty

    def edited_fields(self, track_id: str) -> set[str]:
        return self._journal.fields_for(track_id)

    def save(self) -> SaveOutcome:
        """Write the session's edits to the drive.

        In this order, each step a precondition for the next:

          1. the drive is still there, and NOTHING else has written its database
             (or an analysis file, or `export.pdb`) since Konduktor read it —
             otherwise this would silently undo that;
          2. the drive's current database and those analysis files are backed
             up to app-data;
          3. each edited analysis file is written beside itself and renamed over
             it — BEFORE the database, as the Rekordbox store writes files before
             committing, so a failure here leaves the database untouched — and
             then, where the stick has one, the legacy `export.pdb`, edited to
             match (`device_library.rebuild`; untouched if it already does);
          4. the working copy is committed and closed, which folds its WAL in, so
             the copy is one self-contained file;
          5. the copy is written beside the drive's database under a temporary
             name, the stale `-wal`/`-shm` are deleted (SQLite would REPLAY an
             old WAL into the new file), and the copy is renamed over it — a
             stick pulled mid-save keeps one whole library or the other;
          6. edited text fields go into the audio files' own tags (best-effort,
             reported per file, never failing the save);
          7. the working copy is re-taken from what is now on the drive.

        `snapshot` is None: a drive is a database plus analysis files plus the
        audio, so there is no single blob to version (`capabilities.save.history`).
        """
        db = self._require_db()
        if not self.path.parent.is_dir():
            raise InvalidCommand(
                f"{self.layout.root.name} is not connected. Plug it back in and "
                "save again — your changes are still here."
            )
        changed_anlz = []
        for path, (edited, seen) in self._pending_anlz.items():
            if seen is None:  # an added track's: nothing may be there yet
                if path.exists():
                    changed_anlz.append(path)
            elif not path.is_file() or _fingerprint_file(path) != seen:
                changed_anlz.append(path)
        changed_anlz += [p for p in self._new_files if p.exists()]
        # Added audio to publish: copies whose track is (still) in the library.
        referenced = {self.layout.resolve(getattr(r, "path", None)) for r in self.iter_content()}
        publish = [(i, f) for i, f in self._incoming.items() if i.is_file() and f in referenced]
        changed_anlz += [f for _i, f in publish if f.exists()]
        pdb_now = _fingerprint_file(self.device_library) if self.device_library.is_file() else None
        if _fingerprint(self.path) != self._opened_as or changed_anlz or pdb_now != self._pdb_seen:
            raise InvalidCommand(
                f"Another app has changed {self.layout.root.name} since Konduktor "
                "opened it, and saving would undo that. Discard your changes and "
                "reopen the drive to edit it again."
            )
        summary = self._journal.summary()
        # Read BEFORE the session closes: the rows are useless after it.
        tag_jobs = self._tag_jobs()
        # Only files whose bytes really changed: an edit that was undone, or a
        # command that failed after reading, leaves nothing to write.
        anlz_writes = []
        for path, (edited, seen) in self._pending_anlz.items():
            data = edited.to_bytes()
            if seen is None or data != path.read_bytes():
                anlz_writes.append((path, data))
        doomed = list(dict.fromkeys(self._doomed))
        # The legacy Device Library, edited to match (None: already does). Read
        # inside the transaction, which sees every edit.
        pdb_bytes = None
        if self.device_library.is_file():
            from . import device_library

            db.flush()
            pdb_bytes = device_library.rebuild(self.device_library.read_bytes(), db.session)

        self._backup([path for path, _ in anlz_writes if path.exists()]
                     + ([self.device_library] if pdb_bytes is not None else []))
        # Added audio first: everything written after it points at it.
        for incoming, final in publish:
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming, final)
            del self._incoming[incoming]
        for path, data in list(self._new_files.items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f".{path.name}.konduktor-partial")
            partial.write_bytes(data)
            os.replace(partial, path)
            del self._new_files[path]
        for path, data in anlz_writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f".{path.name}.konduktor-partial")
            partial.write_bytes(data)
            os.replace(partial, path)
            # Ours now: a retry after a later failure must not mistake this
            # write for another app's.
            self._pending_anlz[path] = (self._pending_anlz[path][0], _fingerprint_file(path))
        if pdb_bytes is not None:
            partial = self.device_library.with_name(".export.pdb.konduktor-partial")
            partial.write_bytes(pdb_bytes)
            os.replace(partial, self.device_library)
            self._pdb_seen = _fingerprint_file(self.device_library)

        db.session.commit()
        work, working = self._work, self._work / self.path.name
        self._db = None
        self._content_cache = None
        self._by_id = None
        for step in (db.session.close, db.engine.dispose):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.debug("OneLibrary close step failed", exc_info=True)
        try:
            partial = self.path.with_name(f".{self.path.name}.konduktor-partial")
            shutil.copyfile(working, partial)
            with open(partial, "rb+") as f:
                os.fsync(f.fileno())
            for suffix in _SIDECARS:
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            os.replace(partial, self.path)
        except BaseException:
            # The edits are still in the working copy, committed: reopen it so a
            # retry (after replugging, say) can save them.
            self._reopen_working(work)
            raise
        # The library no longer names them: removed tracks' files go now, and
        # never before the database that stopped naming them is on the drive.
        for path in doomed:
            try:
                path.unlink(missing_ok=True)
                _prune_empty(path.parent, stop=self.layout.root)
            except OSError as ex:
                log.warning("Could not delete %s: %s", path, ex)
        self._doomed.clear()
        self._clear_incoming()
        tag_results = self._sync_file_tags(tag_jobs)
        shutil.rmtree(work, ignore_errors=True)
        self._work = None
        self._load()
        return SaveOutcome(summary=summary, snapshot=None, tag_results=tag_results)

    def _reopen_working(self, work: Path) -> None:
        from pyrekordbox.devicelib_plus import DeviceLibraryPlus

        self._db, self._work = DeviceLibraryPlus(str(work / self.path.name)), work
        self._content_cache = None
        self._by_id = None

    def _backup(self, anlz: list[Path] = ()) -> Path:
        """Copy the drive's database (and a live WAL), and the analysis files about
        to be rewritten, to app-data — laid out as they are on the drive."""
        root = _backup_root(self.layout)
        dest = root / time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while dest.exists():
            n += 1
            dest = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        dest.mkdir(parents=True)
        for p in (self.path, self.path.with_name(self.path.name + "-wal")):
            if p.is_file():
                shutil.copy2(p, dest / p.name)
        for p in anlz:
            target = dest / p.relative_to(self.layout.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        for old in sorted(d for d in root.iterdir() if d.is_dir())[:-_BACKUPS_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    def _tag_jobs(self) -> list[tuple[str, Path, dict]]:
        """(track id, audio file, {field: value}) for every file-tag write a save
        owes: each edited track's edited text fields, as the library now holds them."""
        jobs = []
        for track_id in self._journal.edited_tracks():
            fields = self._journal.fields_for(track_id) & self.FILE_TAG_FIELDS
            if not fields:
                continue
            try:
                path = self.audio_path(track_id)
            except NotFound:
                continue  # removed this session
            if path is not None:
                values = self._tag_values(track_id)
                jobs.append((track_id, path, {f: values.get(f) for f in fields}))
        return jobs

    @staticmethod
    def _sync_file_tags(jobs: list[tuple[str, Path, dict]]) -> list[FileTagResult]:
        """Write edited text fields into each audio file, as rekordbox does."""
        from ...core import audio_tags

        results: list[FileTagResult] = []
        for track_id, path, meta in jobs:
            r = audio_tags.write_tags(path, meta)
            results.append(FileTagResult(track_id=track_id, file=str(path), ok=r.ok,
                                         status=r.status, detail=r.detail))
        return results

    def _tag_values(self, track_id: str) -> dict:
        """The fields' values as the library now holds them (from the working copy)."""
        from . import projection

        t = projection.to_track(self.content(track_id))
        return {"title": t.title, "artist": t.artist, "album": t.album, "genre": t.genre,
                "label": t.label, "remixer": t.remixer, "comment": t.comment,
                "release_date": t.release_date, "key": t.key}


def _prune_empty(folder: Path, *, stop: Path) -> None:
    """Remove `folder` and its parents while empty, never `stop` or above, nor
    the stick's top-level `Contents`/`PIONEER` folders."""
    keep = {stop, stop / "Contents", stop / "PIONEER", stop / "PIONEER" / "USBANLZ"}
    while folder not in keep and folder.is_relative_to(stop):
        try:
            if any(p for p in folder.iterdir() if not p.name.startswith("._") and p.name != ".DS_Store"):
                return
            for p in folder.iterdir():  # macOS's own litter does not keep a folder
                p.unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _clear_stale_work() -> None:
    """Remove working copies a crashed session left behind."""
    try:
        root = _work_root()
        cutoff = time.time() - _STALE_WORK_SEC
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
