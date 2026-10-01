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
from datetime import datetime
from pathlib import Path

from ...core.adapter import FileTagResult, InvalidCommand, LibraryNotSupported, NotFound, SaveOutcome
from ...core.edit_journal import EditJournal
from ...core.model import CuePoint
from ... import paths
from . import beatgrid, cues as cue_reader
from ..rekordbox import anlz_file, timebase
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


def _work_root() -> Path:
    root = paths.app_data_dir() / "onelibrary" / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _backup_root(layout: DriveLayout) -> Path:
    # Keyed by the drive's mount name: a removable library has no sidecar id
    # (library_id.py), and a person looking for a backup knows their stick's name.
    name = re.sub(r"[^\w.-]+", "_", layout.root.name or "drive")
    return paths.app_data_dir() / "onelibrary" / "backups" / name


class OneLibraryStore:
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
    # not). Everything editable except the rating.
    FILE_TAG_FIELDS = EDITABLE_FIELDS - {"rating"}

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
        self._journal = EditJournal()
        # Parsed ANLZ, keyed by track id. Two caches because the two files cost
        # an order of magnitude apart: a grid read must not drag in the .EXT.
        self._dat_cache: dict[str, object | None] = {}
        self._ext_cache: dict[str, object | None] = {}
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
            try:
                return path.read_bytes(), "image/jpeg"
            except OSError:
                continue
        return None

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
        cues = cue_reader.cues_from_anlz(
            self._anlz(track_id, extended=False),
            self._anlz(track_id, extended=True),
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
             since it was opened — otherwise this would silently undo it;
          2. the working copy is committed and closed, which folds its WAL in, so
             the copy is one self-contained file;
          3. the drive's current database is backed up to app-data;
          4. the copy is written beside the drive's database under a temporary
             name, the stale `-wal`/`-shm` are deleted (SQLite would REPLAY an
             old WAL into the new file), and the copy is renamed over it — a
             stick pulled mid-save keeps one whole library or the other;
          5. edited text fields go into the audio files' own tags (best-effort,
             reported per file, never failing the save);
          6. the working copy is re-taken from what is now on the drive.

        `snapshot` is None: a drive is a database plus analysis files plus the
        audio, so there is no single blob to version (`capabilities.save.history`).
        """
        db = self._require_db()
        if not self.path.parent.is_dir():
            raise InvalidCommand(
                f"{self.layout.root.name} is not connected. Plug it back in and "
                "save again — your changes are still here."
            )
        if _fingerprint(self.path) != self._opened_as:
            raise InvalidCommand(
                f"Another app has changed {self.layout.root.name} since Konduktor "
                "opened it, and saving would undo that. Discard your changes and "
                "reopen the drive to edit it again."
            )
        summary = self._journal.summary()
        # Read BEFORE the session closes: the rows are useless after it.
        tag_jobs = self._tag_jobs()

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
            self._backup()
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

    def _backup(self) -> Path:
        """Copy the drive's database (and a live WAL) to app-data before replacing it."""
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
                "release_date": t.release_date}


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
