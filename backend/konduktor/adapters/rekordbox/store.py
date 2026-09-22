"""The retained native model for Rekordbox: the open ``master.db`` session.

This is the only module that knows ``pyrekordbox`` exists. It owns the decrypted
SQLCipher session, the ANLZ analysis files, and the caches the projection reads.

**Writes cover track metadata and playlists.** Cues and the beatgrid are
deliberately absent rather than stubbed — they are separate stores (two DB tables
kept consistent, and the ANLZ analysis files) and a store that cannot write them
cannot half-write them.

Edits accumulate in the SQLAlchemy session and are only made permanent by
`save()`, which maps exactly onto Konduktor's "edit in memory, Save writes to
disk" model. `pyrekordbox`'s `commit()` is what assigns Rekordbox's update
sequence numbers, and a real Rekordbox was verified to accept and continue from
the result — see the handoff's Findings.

Two things here are not obvious and are load-bearing:

  * **ANLZ is read LAZILY.** The beatgrid lives in a per-track analysis file, not
    in the database, and parsing them all costs ~1.4 ms each — about 12 seconds
    for a library the size of the project's Traktor collection. So grids are
    parsed on demand and cached, and `Track.grid_marker_count` carries a
    documented approximation until a track's grid is actually read.
  * **Cue counts come from ONE aggregate query**, not from walking each track's
    relationship, which would be a per-track round trip at open.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload

from ...core.adapter import InvalidCommand, LibraryNotSupported, NotFound, SaveOutcome
from ...core.edit_journal import EditJournal
from ...core.pathmap import PathMapping
from .cue_types import beat_loop_size, kind_for, role_and_slot

log = logging.getLogger(__name__)


def _iso_stamp(value) -> str:
    """Rekordbox writes timestamps in the JSON mirror as ISO-8601 with a
    +00:00 offset and millisecond precision (e.g. 2026-09-17T20:55:13.862+00:00).
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}+00:00"
    return str(value)


class RekordboxStore:
    """One open Rekordbox library."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mapping = PathMapping()
        self._grid_cache: dict[str, tuple[list[float], list[float]] | None] = {}
        self._journal = EditJournal()
        # Grid edits buffered until save(): ANLZ files are written to disk, so
        # applying them at command time would break the save contract.
        self._pending_grids: dict[str, tuple[list, list, list]] = {}
        self._load()

    # ---- open ------------------------------------------------------------
    def _load(self) -> None:
        from pyrekordbox.masterdb import MasterDatabase
        from pyrekordbox.masterdb import models as tables

        self._tables = tables
        try:
            self._db = MasterDatabase(path=str(self.path), unlock=True)
        except Exception as ex:  # noqa: BLE001 — surfaced as a clean 4xx
            raise LibraryNotSupported(f"Could not open Rekordbox library: {ex}") from ex
        self._grid_cache.clear()
        self._content_cache: list | None = None
        self._by_id: dict[str, object] | None = None

    def close(self) -> None:
        """Release the session AND the pooled connection.

        `pyrekordbox`'s own `close()` only closes the SQLAlchemy session, which
        returns the connection to the engine's pool — the OS file handle stays
        open. Disposing the engine is what actually releases `master.db`, and
        without it the file cannot be replaced (a restore, or a test copying a
        fresh library over it) on any platform that locks open files.
        """
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 — closing must never raise
            pass
        try:
            self._db.engine.dispose()
        except Exception:  # noqa: BLE001
            pass

    # ---- identity --------------------------------------------------------
    @property
    def cloud_synced(self) -> bool:
        """True when this library participates in Rekordbox's Cloud Library Sync.

        Konduktor REFUSES to write a cloud-synced library: a local edit that the
        server did not issue could propagate a corrupt sync state to the user's
        other machines, which version history cannot undo. Detection is the
        presence of a server-issued sequence number (`usn`) on any row, or a
        populated cloud registry.
        """
        t = self._tables
        with self._db.session.no_autoflush:
            for table in (t.DjmdContent, t.DjmdPlaylist, t.DjmdCue):
                n = self._db.session.execute(
                    select(func.count()).select_from(table).where(table.usn.isnot(None))
                ).scalar()
                if n:
                    return True
            # pyrekordbox does not model every cloud table, so the rest is a
            # raw probe: any row carrying a server-issued usn means synced.
            for table_name in ("cloudAgentRegistry", "djmdCloudProperty"):
                try:
                    n = self._db.session.execute(
                        text(f'SELECT COUNT(*) FROM "{table_name}" WHERE usn IS NOT NULL')
                    ).scalar()
                except Exception:  # noqa: BLE001 — table may be absent
                    continue
                if n:
                    return True
        return False

    @property
    def local_usn(self) -> int | None:
        """Rekordbox's global local update counter (for diagnostics/tests)."""
        try:
            return self._db.get_local_usn()
        except Exception:  # noqa: BLE001
            return None

    # ---- content ---------------------------------------------------------
    def iter_content(self) -> list:
        """Every track, with the lookup tables eagerly joined.

        Without `joinedload` each track's artist/album/genre/key would be a
        separate query — thousands of round trips on a real library.
        """
        if self._content_cache is None:
            t = self._tables
            q = (
                self._db.session.query(t.DjmdContent)
                .options(
                    joinedload(t.DjmdContent.Artist),
                    joinedload(t.DjmdContent.Album),
                    joinedload(t.DjmdContent.Genre),
                    joinedload(t.DjmdContent.Key),
                    joinedload(t.DjmdContent.Label),
                    joinedload(t.DjmdContent.Remixer),
                )
                .filter(t.DjmdContent.rb_local_deleted == 0)
            )
            self._content_cache = list(q)
            self._by_id = {str(c.ID): c for c in self._content_cache}
        return self._content_cache

    def content(self, track_id: str):
        self.iter_content()
        assert self._by_id is not None
        row = self._by_id.get(str(track_id))
        if row is None:
            raise NotFound(f"No track {track_id!r}")
        return row

    def cue_counts(self) -> dict[str, tuple[int, int]]:
        """``{track_id: (total_cues, hotcue_count)}`` in one aggregate query."""
        t = self._tables
        rows = self._db.session.execute(
            select(t.DjmdCue.ContentID, t.DjmdCue.Kind, func.count())
            .where(t.DjmdCue.rb_local_deleted == 0)
            .group_by(t.DjmdCue.ContentID, t.DjmdCue.Kind)
        ).all()
        out: dict[str, tuple[int, int]] = {}
        for content_id, kind, n in rows:
            total, hot = out.get(str(content_id), (0, 0))
            # "Is this a hot cue?" must mean the same here as in the projection,
            # or the count in the library table disagrees with the pads shown in
            # the deck. `role_and_slot` is the single definition — it also rules
            # out the reserved Kind, which occupies no pad.
            is_hotcue = role_and_slot(kind)[0] == "hotcue"
            out[str(content_id)] = (total + n, hot + (n if is_hotcue else 0))
        return out

    def cues(self, track_id: str) -> list:
        """A track's cue rows, ordered by position."""
        t = self._tables
        return list(
            self._db.session.query(t.DjmdCue)
            .filter(t.DjmdCue.ContentID == str(track_id))
            .filter(t.DjmdCue.rb_local_deleted == 0)
            .order_by(t.DjmdCue.InMsec)
        )

    # ---- playlists -------------------------------------------------------
    def playlists(self) -> list:
        """User playlists, excluding Rekordbox's own internal ones.

        Rekordbox keeps working lists ("CUE Analysis Playlist", "Cloud Library
        Sync") in the same table at reserved ids. They are not the user's and
        showing them in the sidebar would be noise.
        """
        from pyrekordbox.masterdb.database import SPECIAL_PLAYLIST_IDS

        t = self._tables
        return [
            p
            for p in self._db.session.query(t.DjmdPlaylist)
            .filter(t.DjmdPlaylist.rb_local_deleted == 0)
            .order_by(t.DjmdPlaylist.Seq)
            if str(p.ID) not in SPECIAL_PLAYLIST_IDS
        ]

    def playlist_song_ids(self, playlist_id: str) -> list[str]:
        t = self._tables
        rows = (
            self._db.session.query(t.DjmdSongPlaylist.ContentID)
            .filter(t.DjmdSongPlaylist.PlaylistID == str(playlist_id))
            .filter(t.DjmdSongPlaylist.rb_local_deleted == 0)
            .order_by(t.DjmdSongPlaylist.TrackNo)
        )
        return [str(r[0]) for r in rows]

    def count_playlists(self) -> int:
        """Playlists the user has, for the status bar — folders excluded."""
        folder = int(self._tables.PlaylistType.FOLDER)
        return sum(1 for p in self.playlists() if int(p.Attribute or 0) != folder)

    # ---- analysis files (the beatgrid) -----------------------------------
    def anlz_grid(self, track_id: str) -> tuple[list[float], list[float]] | None:
        """``(times_sec, bpms)`` per beat for a track, or None if unanalysed.

        Parsed on demand and cached — see the module docstring for why this is
        not done eagerly.
        """
        key = str(track_id)
        if key in self._grid_cache:
            return self._grid_cache[key]
        result = self._read_anlz_grid(key)
        self._grid_cache[key] = result
        return result

    def _read_anlz_grid(self, track_id: str):
        row = self.content(track_id)
        rel = getattr(row, "AnalysisDataPath", None)
        if not rel:
            return None
        # AnalysisDataPath is rooted at the library's `share` directory and is
        # stored with a leading slash.
        path = self.path.parent / "share" / str(rel).lstrip("/\\")
        if not path.is_file():
            return None
        try:
            from pyrekordbox.anlz import AnlzFile

            anlz = AnlzFile.parse_file(str(path))
        except Exception as ex:  # noqa: BLE001 — a bad ANLZ must not break a read
            log.debug("ANLZ parse failed for %s: %s", track_id, ex)
            return None
        for tag in anlz.tags:
            # Never treat AnlzFile as a Mapping: keys()/len() recurse forever in
            # pyrekordbox 0.4.4.
            if tag.type == "PQTZ":
                try:
                    return [float(t) for t in tag.times], [float(b) for b in tag.bpms]
                except Exception:  # noqa: BLE001
                    return None
        return None

    def has_analysis(self, track_id: str) -> bool:
        return bool(getattr(self.content(track_id), "AnalysisDataPath", None))

    # ---- paths -----------------------------------------------------------
    def set_path_mapping(self, mapping: PathMapping) -> None:
        self._mapping = mapping or PathMapping()

    @property
    def path_mapping(self) -> PathMapping:
        return self._mapping

    def audio_path(self, track_id: str) -> Path | None:
        """The track's audio file on THIS machine, after any path remap."""
        folder = getattr(self.content(track_id), "FolderPath", None)
        if not folder:
            return None
        return self._mapping.apply(Path(str(folder)))

    def all_audio_paths(self) -> list[str]:
        return [
            str(c.FolderPath) for c in self.iter_content() if getattr(c, "FolderPath", None)
        ]

    # ---- writes: track metadata -----------------------------------------
    #
    # Deliberately NOT editable: the file path (it is the track's identity),
    # BPM and key (audio/grid territory), and Rekordbox's own read-only
    # bookkeeping. `producer` and `mix` are Traktor concepts with no Rekordbox
    # column — Rekordbox has a Composer, which is not the same field — so they
    # are simply absent from the set rather than mapped to something close.
    _DIRECT_FIELDS = {
        "title": "Title",
        "comment": "Commnt",
        "release_date": "ReleaseDate",
    }
    # Fields stored as a foreign key into a lookup table, which must be
    # found-or-created: `add_*` in pyrekordbox RAISES on an existing name.
    _LOOKUP_FIELDS = {
        "artist": ("DjmdArtist", "ArtistID", "add_artist"),
        "album": ("DjmdAlbum", "AlbumID", "add_album"),
        "genre": ("DjmdGenre", "GenreID", "add_genre"),
        "label": ("DjmdLabel", "LabelID", "add_label"),
        # A remixer IS an artist in Rekordbox — same table, different column.
        "remixer": ("DjmdArtist", "RemixerID", "add_artist"),
    }
    EDITABLE_FIELDS = set(_DIRECT_FIELDS) | set(_LOOKUP_FIELDS) | {"rating"}

    def _lookup_id(self, table_name: str, adder: str, name: str) -> str | None:
        """The id of the lookup row called `name`, creating it if needed."""
        if not name:
            return None
        table = getattr(self._tables, table_name)
        existing = self._db.session.query(table).filter_by(Name=name).first()
        if existing is not None:
            return str(existing.ID)
        row = getattr(self._db, adder)(name)
        return str(row.ID)

    def set_track_metadata(self, track_id: str, fields: dict) -> None:
        row = self.content(track_id)
        for key, value in fields.items():
            if key not in self.EDITABLE_FIELDS:
                continue  # unknown/!safe fields are ignored, never guessed at
            if key == "rating":
                try:
                    stars = int(value or 0)
                except (TypeError, ValueError):
                    raise InvalidCommand(f"Rating must be a number, got {value!r}") from None
                if not 0 <= stars <= 5:
                    raise InvalidCommand(f"Rating must be 0-5, got {stars}")
                before, new = row.Rating, stars
                row.Rating = stars
            elif key in self._DIRECT_FIELDS:
                column = self._DIRECT_FIELDS[key]
                before = getattr(row, column)
                new = (value or None) if isinstance(value, str) else value
                setattr(row, column, new)
            else:
                table_name, column, adder = self._LOOKUP_FIELDS[key]
                before = getattr(row, column)
                new = self._lookup_id(table_name, adder, (value or "").strip())
                setattr(row, column, new)
            self._journal.record("track", "set", track_id, key, before, new)
        # Artist/album/genre/label are FOREIGN KEYS. Setting the id does not move
        # the ORM's cached relationship, so re-projecting the row immediately
        # would read the OLD name back and report a successful edit as a no-op.
        # Flush pushes the ids into the transaction; expiring the row makes the
        # next attribute access reload the joined rows with them.
        self._db.flush()
        self._db.session.expire(row)

    # ---- writes: playlists ------------------------------------------------
    def _playlist(self, node_id: str):
        t = self._tables
        row = (
            self._db.session.query(t.DjmdPlaylist)
            .filter(t.DjmdPlaylist.ID == str(node_id))
            .first()
        )
        if row is None:
            raise NotFound(f"No playlist {node_id!r}")
        return row

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        name = (name or "").strip()
        if not name:
            raise InvalidCommand("A playlist needs a name")
        parent = self._playlist(parent_id) if parent_id else None
        if parent is not None and int(parent.Attribute or 0) != int(
            self._tables.PlaylistType.FOLDER
        ):
            raise InvalidCommand("A playlist can only be created inside a folder")
        row = self._db.create_playlist(name, parent=parent)
        self._journal.record("playlist", "create", name)
        return str(row.ID)

    def rename_playlist(self, node_id: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise InvalidCommand("A playlist needs a name")
        row = self._playlist(node_id)
        self._db.rename_playlist(row, name)
        self._journal.record("playlist", "rename", name)

    def delete_playlist(self, node_id: str) -> None:
        row = self._playlist(node_id)
        name = row.Name
        self._db.delete_playlist(row)
        self._journal.record("playlist", "delete", name)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        """Replace a playlist's contents, in the given order.

        Rekordbox keys order by `DjmdSongPlaylist.TrackNo`, and there is no
        bulk reorder — so this clears the list and re-adds it. `add_to_playlist`
        is used one row at a time because it is what maintains Rekordbox's own
        USN ordering for these rows.
        """
        t = self._tables
        playlist = self._playlist(node_id)
        if int(playlist.Attribute or 0) != int(t.PlaylistType.PLAYLIST):
            raise InvalidCommand("Only a plain playlist has an editable track list")
        known = {tid for tid in track_ids if self._by_id and str(tid) in self._by_id}
        unknown = [tid for tid in track_ids if tid not in known]
        if unknown:
            raise NotFound(f"Unknown track(s): {', '.join(map(str, unknown[:3]))}")
        existing = (
            self._db.session.query(t.DjmdSongPlaylist)
            .filter(t.DjmdSongPlaylist.PlaylistID == str(node_id))
            .all()
        )
        for song in existing:
            self._db.remove_from_playlist(playlist, song)
        for n, track_id in enumerate(track_ids, start=1):
            self._db.add_to_playlist(playlist, str(track_id), track_no=n)
        self._journal.record("playlist", "entries", playlist.Name, after=len(track_ids))
        return len(track_ids)

    # ---- save --------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._journal.dirty

    def edited_fields(self, track_id: str) -> set[str]:
        return self._journal.fields_for(track_id)

    def save(self) -> SaveOutcome:
        """Make the session's edits permanent.

        `commit(autoinc=True)` is what assigns Rekordbox's update sequence
        numbers — one increment per change, stamped onto each changed row. A real
        Rekordbox was verified to accept a library written this way and to carry
        on from the counter it was left at.

        `snapshot` is None: a Rekordbox library is `master.db` plus its analysis
        files plus a playlist XML, so there is no single blob to version, and
        `capabilities.save.history` says so.
        """
        summary = self._journal.summary()
        # Files first: if an analysis file cannot be written, nothing should have
        # been committed to the database either.
        self._flush_grids()
        self._commit()
        self._journal.clear()
        # Reads go through the same session, so nothing needs re-projecting from
        # scratch — but the grid cache is keyed by track and survives a save.
        return SaveOutcome(summary=summary, snapshot=None, tag_results=[])

    def _commit(self) -> None:
        """Commit, warning rather than refusing if Rekordbox is running.

        `pyrekordbox.commit()` raises outright when it sees a Rekordbox process.
        That is not Konduktor's behaviour: the settled decision is to **warn but
        proceed**, matching how Traktor is handled — the user is told, not
        blocked on a detection heuristic. The check is also process-wide rather
        than per-file, so it would refuse to write a temp copy of a library while
        Rekordbox had an entirely different one open, which makes tests depend on
        whether the app happens to be running.

        The veto is suppressed by neutralising the process probe for the duration
        of the call, so every other thing `commit()` does — the USN
        auto-increment, the session commit, and keeping `masterPlaylists6.xml`'s
        timestamps in step — still runs as the library intends. Duplicating that
        body here would silently drift from it on upgrade.
        """
        from pyrekordbox.masterdb import database as rb_database

        if rb_database.get_rekordbox_pid():
            log.warning(
                "Rekordbox appears to be running; saving anyway. It may overwrite "
                "or re-sync the library while it is open."
            )
        original = rb_database.get_rekordbox_pid
        rb_database.get_rekordbox_pid = lambda *a, **kw: 0
        try:
            self._db.commit(autoinc=True)
        except OperationalError as ex:
            # Suppressing pyrekordbox's veto does not remove SQLite's own lock:
            # a running Rekordbox really does hold the database, and the raw
            # error is an unhelpful 500. Say what happened and what to do.
            if "locked" not in str(ex).lower():
                raise
            raise InvalidCommand(
                "Rekordbox has the library open, so it cannot be written right "
                "now. Close Rekordbox and save again — your changes are still here."
            ) from ex
        finally:
            rb_database.get_rekordbox_pid = original

    @property
    def app_running(self) -> bool:
        """Whether a Rekordbox process is up — for warning the user, not blocking."""
        try:
            from pyrekordbox.masterdb import database as rb_database

            return bool(rb_database.get_rekordbox_pid())
        except Exception:  # noqa: BLE001 — detection must never break a save
            return False

    # ---- writes: cues -----------------------------------------------------
    #
    # Rekordbox keeps cues in TWO places that must agree: normalized `djmdCue`
    # rows, and a denormalized JSON mirror in `contentCue.Cues` alongside a
    # `rb_cue_count`. Every mutation here ends in `_sync_content_cue()`.
    #
    # Field conventions below are read off a real library (see the handoff's
    # Findings), not guessed:
    #   * `Kind` is the bank slot; 0 means a memory cue.
    #   * a loop is an ordinary cue row with `OutMsec` > 0 plus `BeatLoopSize`.
    #   * `InFrame`/`OutFrame` are the position in frames at 150 fps.
    #   * an uncoloured cue is `Color=-1, ColorTableIndex=NULL`; Rekordbox writes
    #     `Color=255, ColorTableIndex=0` on the loops and auto-cues it creates.

    _FPS = 150  # Rekordbox stores cue positions in frames as well as ms

    @staticmethod
    def _frames(msec: int) -> int:
        return int(msec * RekordboxStore._FPS / 1000)

    def _cue_row(self, track_id: str, slot: int):
        """The hot cue occupying the 0-based generic `slot`, or None."""
        t = self._tables
        return (
            self._db.session.query(t.DjmdCue)
            .filter(t.DjmdCue.ContentID == str(track_id))
            .filter(t.DjmdCue.Kind == kind_for("hotcue", slot))
            .filter(t.DjmdCue.rb_local_deleted == 0)
            .first()
        )

    def _loop_beats(self, track_id: str, start_sec: float, length_sec: float) -> int | None:
        """Musical length of a loop, from the track's own tempo at that point."""
        if length_sec <= 0:
            return None
        bpm = None
        grid = self.anlz_grid(track_id)
        if grid:
            times, bpms = grid
            for t, b in zip(times, bpms):
                if t <= start_sec:
                    bpm = b
                else:
                    break
            if bpm is None and bpms:
                bpm = bpms[0]
        if not bpm:
            raw = getattr(self.content(track_id), "BPM", 0) or 0
            bpm = raw / 100.0 if raw else None
        if not bpm:
            return None
        beats = round(length_sec / (60.0 / bpm))
        return max(1, int(beats))

    def set_cue(
        self,
        track_id: str,
        *,
        slot: int,
        start_sec: float,
        cue_type: str,
        length_sec: float = 0.0,
        name: str | None = None,
    ) -> None:
        """Create or replace the cue in `slot`. A loop is a cue with a length."""
        if start_sec < 0:
            raise InvalidCommand("A cue cannot be before the start of the track")
        if int(slot) < 0:
            raise InvalidCommand(f"Hot cue slot cannot be negative, got {slot}")
        row = self.content(track_id)
        in_ms = int(round(start_sec * 1000))
        is_loop = cue_type == "loop" and length_sec > 0
        out_ms = int(round((start_sec + length_sec) * 1000)) if is_loop else -1

        kind = kind_for("hotcue", slot)
        existing = self._cue_row(track_id, slot)
        op = "add" if existing is None else "modify"
        cue = existing
        if cue is None:
            t = self._tables
            cue = t.DjmdCue.create(
                ID=str(self._db.generate_unused_id(t.DjmdCue)),
                ContentID=str(track_id),
                # Verified: a cue's ContentUUID is the TRACK's UUID, which is also
                # the id of its contentCue mirror row.
                ContentUUID=str(row.UUID),
                UUID=str(uuid.uuid4()),
                Kind=kind,
                rb_data_status=0,
                rb_local_data_status=0,
                rb_local_deleted=0,
                rb_local_synced=0,
            )
            self._db.add(cue)

        cue.InMsec = in_ms
        cue.InFrame = self._frames(in_ms)
        cue.InMpegFrame = 0
        cue.InMpegAbs = 0
        cue.OutMsec = out_ms
        cue.OutFrame = self._frames(out_ms) if is_loop else 0
        cue.OutMpegFrame = 0
        cue.OutMpegAbs = 0
        cue.Kind = kind
        if is_loop:
            beats = self._loop_beats(track_id, start_sec, length_sec)
            cue.BeatLoopSize = beat_loop_size(beats) if beats else None
            cue.ActiveLoop = 0
            cue.CueMicrosec = 0
            cue.Color = 255
            cue.ColorTableIndex = 0
        else:
            cue.BeatLoopSize = None
            cue.ActiveLoop = None
            cue.CueMicrosec = None
            cue.Color = -1
            cue.ColorTableIndex = None
        cue.Comment = name or None

        self._db.flush()
        self._sync_content_cue(track_id)
        self._journal.record("cue", op, track_id, f"slot:{slot}")

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> None:
        cue = self._cue_row(track_id, slot)
        if cue is None:
            raise NotFound(f"No cue in slot {slot}")
        start = (cue.InMsec or 0) / 1000.0
        length = 0.0
        if cue_type == "loop":
            if cue.OutMsec and cue.OutMsec > 0:
                length = (cue.OutMsec - (cue.InMsec or 0)) / 1000.0
            else:
                raise InvalidCommand(
                    "Turning a cue into a loop needs a length — set the loop first"
                )
        self.set_cue(
            track_id, slot=slot, start_sec=start, cue_type=cue_type,
            length_sec=length, name=cue.Comment,
        )

    def delete_cue(self, track_id: str, slot: int) -> None:
        cue = self._cue_row(track_id, slot)
        if cue is None:
            raise NotFound(f"No cue in slot {slot}")
        self._db.delete(cue)
        self._db.flush()
        self._sync_content_cue(track_id)
        self._journal.record("cue", "delete", track_id, f"slot:{slot}")

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> None:
        """Batch placement (Auto Hotcues). Fills empty slots unless overwriting."""
        for cue in cues:
            slot = int(getattr(cue, "slot"))
            if not overwrite and self._cue_row(track_id, slot) is not None:
                continue
            self.set_cue(
                track_id,
                slot=slot,
                start_sec=float(getattr(cue, "start", 0.0)),
                cue_type=getattr(cue, "type", "cue"),
                length_sec=float(getattr(cue, "length", 0.0) or 0.0),
                name=getattr(cue, "name", None),
            )

    def _sync_content_cue(self, track_id: str) -> None:
        """Rebuild the denormalized `contentCue` mirror for a track.

        Rekordbox keeps a JSON copy of every cue alongside a count, and stamps
        its USN on THAT row rather than on the individual cue rows — it is the
        sync unit. Leaving it stale after editing `djmdCue` would make the two
        disagree, which is this platform's version of Traktor's companion-cue
        trap.

        Keys whose value is NULL are omitted, which is what Rekordbox's own
        serializer does in every row observed.
        """
        t = self._tables
        row = self.content(track_id)
        cues = (
            self._db.session.query(t.DjmdCue)
            .filter(t.DjmdCue.ContentID == str(track_id))
            .filter(t.DjmdCue.rb_local_deleted == 0)
            .order_by(t.DjmdCue.InMsec)
            .all()
        )
        fields = (
            "ID", "ContentID", "ContentUUID", "InMsec", "InFrame", "InMpegFrame",
            "InMpegAbs", "OutMsec", "OutFrame", "OutMpegFrame", "OutMpegAbs",
            "Kind", "Color", "ColorTableIndex", "ActiveLoop", "Comment",
            "BeatLoopSize", "CueMicrosec", "UUID",
        )
        records = []
        for cue in cues:
            rec = {}
            for field in fields:
                value = getattr(cue, field, None)
                if value is None:
                    continue
                rec[field] = value
            for stamp in ("created_at", "updated_at"):
                value = getattr(cue, stamp, None)
                if value is not None:
                    rec[stamp] = _iso_stamp(value)
            records.append(rec)

        mirror = (
            self._db.session.query(t.ContentCue)
            .filter(t.ContentCue.ContentID == str(track_id))
            .first()
        )
        if mirror is None:
            if not records:
                return
            mirror = t.ContentCue.create(
                # Verified: the mirror's id IS the track's UUID.
                ID=str(row.UUID),
                ContentID=str(track_id),
                UUID=str(uuid.uuid4()),
                rb_data_status=0,
                rb_local_data_status=0,
                rb_local_deleted=0,
                rb_local_synced=0,
            )
            self._db.add(mirror)
        mirror.Cues = json.dumps(records, ensure_ascii=False)
        mirror.rb_cue_count = len(records)
        self._db.flush()

    # ---- writes: the beatgrid ---------------------------------------------
    #
    # The grid lives in the track's ANLZ `.DAT` (`PQTZ`), not in the database.
    # Verified in Rekordbox 7: it reads the grid and the deck's BPM readout from
    # `PQTZ`, leaves a rewritten `.DAT` exactly as Konduktor wrote it, ignores
    # the stale extended grid in `.EXT` — and does NOT reconcile
    # `djmdContent.BPM`, which stayed at the old tempo while the deck showed the
    # new one. So that column is Konduktor's to maintain.
    #
    # Edits are BUFFERED and the files are written on save(), so the grid obeys
    # the same "edit in memory, Save writes to disk" contract as everything else
    # — an unsaved grid change must not already be on disk.

    def current_markers(self, track_id: str) -> list:
        """The track's grid as a generic marker list (pending edits included)."""
        from . import beatgrid as grid_math

        grid = self.anlz_grid(track_id)
        if grid is None:
            return []
        times, bpms = grid
        return grid_math.markers_from_beats(times, bpms)

    def track_duration(self, track_id: str) -> float:
        """Seconds, for expanding markers back into beats."""
        length = getattr(self.content(track_id), "Length", None)
        if length:
            return float(length)
        grid = self.anlz_grid(track_id)
        if grid and grid[0]:
            return float(grid[0][-1]) + 1.0
        return 0.0

    def replace_grid(self, track_id: str, markers: list) -> None:
        """Set the grid to exactly these markers. The one primitive; every
        marker-level command is expressed as a read-modify-replace on top."""
        from . import beatgrid as grid_math

        for m in markers:
            if m.bpm <= 0:
                raise InvalidCommand(f"A beatgrid marker needs a positive tempo, got {m.bpm}")
            if m.start < 0:
                raise InvalidCommand("A beatgrid marker cannot be before the track starts")
        ordered = sorted(markers, key=lambda m: m.start)
        duration = self.track_duration(track_id)
        beat_nums, bpms, times = grid_math.beats_from_markers(ordered, duration)
        # Buffer as the same (times, bpms) shape the reader returns, so the
        # projection needs no special case for an unsaved grid.
        self._pending_grids[str(track_id)] = (beat_nums, bpms, times)
        self._grid_cache[str(track_id)] = (times, bpms) if times else None
        # TEMPO mirrors the first marker, exactly as Traktor's <TEMPO> does —
        # Rekordbox will not do it for us.
        row = self.content(track_id)
        row.BPM = int(round(ordered[0].bpm * 100)) if ordered else 0
        self._journal.record("grid", "replace" if ordered else "delete", track_id)

    def delete_grid(self, track_id: str) -> None:
        self.replace_grid(track_id, [])

    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> None:
        from ...core.model import GridMarker

        markers = self.current_markers(track_id)
        if bpm is None:
            # Inherit the tempo governing this point, like Traktor's add does.
            governing = [m for m in markers if m.start <= start_sec]
            bpm = governing[-1].bpm if governing else (markers[0].bpm if markers else None)
        if not bpm:
            raise InvalidCommand("The first marker on an ungridded track needs a tempo")
        if any(abs(m.start - start_sec) < 0.001 for m in markers):
            raise InvalidCommand("There is already a marker here")
        markers.append(GridMarker(start=float(start_sec), bpm=float(bpm)))
        self.replace_grid(track_id, markers)

    def _marker_at(self, track_id: str, index: int) -> tuple[list, int]:
        markers = self.current_markers(track_id)
        if not 0 <= index < len(markers):
            raise NotFound(f"No grid marker {index}")
        return markers, index

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> None:
        markers, i = self._marker_at(track_id, index)
        # Clamp between neighbours so the list cannot reorder under the caller.
        low = markers[i - 1].start + 0.001 if i > 0 else 0.0
        high = markers[i + 1].start - 0.001 if i + 1 < len(markers) else None
        target = max(low, float(start_sec))
        if high is not None:
            target = min(target, high)
        markers[i] = markers[i].model_copy(update={"start": target})
        self.replace_grid(track_id, markers)

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> None:
        markers, i = self._marker_at(track_id, index)
        if bpm <= 0:
            raise InvalidCommand(f"A tempo must be positive, got {bpm}")
        markers[i] = markers[i].model_copy(update={"bpm": float(bpm)})
        self.replace_grid(track_id, markers)

    def delete_grid_marker(self, track_id: str, index: int) -> None:
        markers, i = self._marker_at(track_id, index)
        del markers[i]
        self.replace_grid(track_id, markers)

    def _flush_grids(self) -> None:
        """Write buffered grids into their ANLZ files. Called by save().

        Only `.DAT`'s `PQTZ` is written — `.EXT`'s extended grid carries
        undecoded bytes, and Rekordbox was verified to read the grid from
        `PQTZ` and to be untroubled by the other being stale.
        """
        from . import beatgrid as grid_math

        for track_id, (beat_nums, bpms, times) in list(self._pending_grids.items()):
            rel = getattr(self.content(track_id), "AnalysisDataPath", None)
            if not rel:
                raise InvalidCommand(
                    "This track has no analysis file, so it has nowhere to store a beatgrid"
                )
            path = self.path.parent / "share" / str(rel).lstrip("/\\")
            if not path.is_file():
                raise InvalidCommand(f"Analysis file is missing: {path}")
            grid_math.write_pqtz(path, beat_nums, bpms, times)
        self._pending_grids.clear()
