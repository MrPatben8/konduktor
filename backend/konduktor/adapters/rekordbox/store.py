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

import logging
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import joinedload

from ...core.adapter import InvalidCommand, LibraryNotSupported, NotFound, SaveOutcome
from ...core.edit_journal import EditJournal
from ...core.pathmap import PathMapping

log = logging.getLogger(__name__)


class RekordboxStore:
    """One open Rekordbox library."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mapping = PathMapping()
        self._grid_cache: dict[str, tuple[list[float], list[float]] | None] = {}
        self._journal = EditJournal()
        self._load()

    # ---- open ------------------------------------------------------------
    def _load(self) -> None:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables

        self._tables = tables
        try:
            self._db = Rekordbox6Database(path=str(self.path), unlock=True)
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
            out[str(content_id)] = (total + n, hot + (n if (kind or 0) != 0 else 0))
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
        from pyrekordbox.db6.database import SPECIAL_PLAYLIST_IDS

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
        self._db.commit(autoinc=True)
        self._journal.clear()
        # Reads go through the same session, so nothing needs re-projecting from
        # scratch — but the grid cache is keyed by track and survives a save.
        return SaveOutcome(summary=summary, snapshot=None, tag_results=[])
