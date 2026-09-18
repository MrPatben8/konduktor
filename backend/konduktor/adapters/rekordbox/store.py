"""The retained native model for Rekordbox: the open ``master.db`` session.

This is the only module that knows ``pyrekordbox`` exists. It owns the decrypted
SQLCipher session, the ANLZ analysis files, and the caches the projection reads.

**Read-only in this milestone.** The write path is deliberately absent rather
than stubbed: a store that cannot write cannot half-write.

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

from ...core.adapter import LibraryNotSupported, NotFound
from ...core.pathmap import PathMapping

log = logging.getLogger(__name__)


class RekordboxStore:
    """One open Rekordbox library."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mapping = PathMapping()
        self._grid_cache: dict[str, tuple[list[float], list[float]] | None] = {}
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
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 — closing must never raise
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
