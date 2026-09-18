"""The Rekordbox adapter: owns the native database and the generic projection.

Mirrors `adapters.traktor.adapter`. It holds:

  * a ``RekordboxStore`` — the retained NATIVE model (the open ``master.db``),
  * a ``TrackIndex`` — the generic READ projection built from it.

**Track metadata and playlists are writable; cues and the beatgrid are not
yet.** Those two are separate native stores (two DB tables kept consistent, and
the per-track ANLZ analysis files), so they keep refusing until milestone 3 —
and `capabilities.cues.editable` / `grid.editable` say so, which is what the UI
actually gates on. The raise is the backstop, not the mechanism.

Writes are refused entirely, and permanently, on a **cloud-synced** library:
propagating a bad sync state to the user's other machines is not something
version history can undo — especially here, where there is no version history at
all. That check lives at the top of every command path so no future command can
forget it.
"""
from __future__ import annotations

from pathlib import Path

from ...core.adapter import Unsupported
from ...core.capabilities import Capabilities
from ...core.model import PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping, common_dir_prefix
from ...core.query import TrackIndex
from . import capabilities as caps
from . import projection
from .store import RekordboxStore


class RekordboxAdapter:
    platform = "rekordbox"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._store = RekordboxStore(self.path)
        self._index = TrackIndex()
        self._cloud_synced = self._store.cloud_synced
        self._rebuild()

    # ---- projection maintenance -----------------------------------------
    def _rebuild(self) -> None:
        counts = self._store.cue_counts()
        self._index.rebuild(
            [
                projection.to_track(row, counts.get(str(row.ID), (0, 0)))
                for row in self._store.iter_content()
            ]
        )

    def _refresh(self, track_id: str) -> None:
        """Re-project one track after a command, so the UI cannot go stale.

        Every mutating command must end in this — a forgotten refresh is a
        silently stale projection that no byte- or row-level test would catch.
        """
        counts = self._store.cue_counts()
        row = self._store.content(track_id)
        self._index.replace(projection.to_track(row, counts.get(str(row.ID), (0, 0))))

    def close(self) -> None:
        """Release the database connection.

        Rekordbox's library is a live SQLite file, not a document parsed into
        memory: leaving the connection open keeps a file handle (and possibly a
        `-wal`) around, so an adapter that is being replaced must be closed.
        """
        self._store.close()

    def reload(self) -> None:
        self._store._load()
        self._cloud_synced = self._store.cloud_synced
        self._rebuild()

    # ---- identity --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(
            self.path,
            cloud_synced=self._cloud_synced,
            editable_fields=sorted(RekordboxStore.EDITABLE_FIELDS),
        )

    @property
    def cloud_synced(self) -> bool:
        return self._cloud_synced

    # ---- read ------------------------------------------------------------
    @property
    def tracks(self) -> list[Track]:
        return self._index.tracks

    @property
    def by_key(self) -> dict[str, Track]:
        return self._index.by_key

    def track(self, track_id: str) -> Track | None:
        return self._index.get(track_id)

    def query_tracks(self, **kw):
        return self._index.query_tracks(**kw)

    def facets(self):
        return self._index.facets()

    def stats(self, playlist_count: int):
        return self._index.stats(playlist_count)

    def playlist_count(self) -> int:
        return self._store.count_playlists()

    def track_cues(self, track_id: str) -> TrackCues | None:
        if self._index.get(track_id) is None:
            return None
        cues = projection.to_track_cues(
            self._store.cues(track_id), self._store.anlz_grid(track_id)
        )
        # The track projection carries an APPROXIMATE grid_marker_count until the
        # grid is actually read (see projection.to_track). This is that moment —
        # correct it, so the library table stops showing the guess.
        track = self._index.get(track_id)
        if track is not None:
            track.grid_marker_count = len(cues.grid_markers)
        return cues

    # ---- playlists -------------------------------------------------------
    def playlist_tree(self) -> list[PlaylistNode]:
        """Rekordbox stores the tree as a flat table with ``ParentID`` links."""
        from pyrekordbox.db6 import tables

        rows = self._store.playlists()
        folder = int(tables.PlaylistType.FOLDER)
        smart = int(tables.PlaylistType.SMART_PLAYLIST)
        writable = not self._cloud_synced

        nodes: dict[str, PlaylistNode] = {}
        parents: dict[str, str] = {}
        for r in rows:
            attribute = int(r.Attribute or 0)
            kind = "folder" if attribute == folder else ("smart" if attribute == smart else "playlist")
            node_id = str(r.ID)
            nodes[node_id] = PlaylistNode(
                id=node_id,
                name=r.Name or "",
                kind=kind,
                count=(0 if kind == "folder" else len(self._store.playlist_song_ids(node_id))),
                children=[],
                # A smart playlist has no static entry list; a folder has no tracks.
                selectable=kind == "playlist",
                # Rekordbox's own working lists are filtered out by the store, so
                # everything reaching here is the user's and may be edited — as
                # long as the library itself is writable at all.
                can_add_tracks=writable and kind == "playlist",
                can_reorder=writable and kind == "playlist",
                can_rename=writable,
                can_delete=writable,
                can_contain_children=kind == "folder",
            )
            parent = str(r.ParentID or "root")
            parents[node_id] = "root" if parent in ("0", "", "root", "None") else parent

        roots: list[PlaylistNode] = []
        for node_id, node in nodes.items():
            parent_id = parents.get(node_id, "root")
            parent = nodes.get(parent_id)
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
        return roots

    def playlist_entries(self, node_id: str) -> list[str] | None:
        ids = self._store.playlist_song_ids(node_id)
        return ids if ids or self._is_playlist(node_id) else None

    def playlist_tracks(self, node_id: str) -> list[Track] | None:
        keys = self.playlist_entries(node_id)
        if keys is None:
            return None
        return [t for k in keys if (t := self._index.get(k)) is not None]

    def _is_playlist(self, node_id: str) -> bool:
        return any(str(r.ID) == str(node_id) for r in self._store.playlists())

    # ---- audio / paths ----------------------------------------------------
    def audio_path(self, track_id: str):
        return self._store.audio_path(track_id)

    def set_path_mapping(self, mapping: PathMapping) -> None:
        self._store.set_path_mapping(mapping)

    def path_prefix_suggestions(self) -> dict:
        paths = self._store.all_audio_paths()
        primary = common_dir_prefix(paths)
        return {"primary": primary, "groups": [{"prefix": primary, "count": len(paths)}] if primary else []}

    def remap_preview(self, mapping: PathMapping) -> dict:
        paths = [Path(p) for p in self._store.all_audio_paths()]
        matched = [p for p in paths if mapping.matches(p)]
        samples = []
        for p in matched[:5]:
            target = mapping.apply(p)
            samples.append({"from": str(p), "to": str(target), "exists": target.exists()})
        existing = sum(1 for p in matched if mapping.apply(p).exists())
        return {
            "total": len(paths),
            "matched": len(matched),
            "existing": existing,
            "samples": samples,
        }

    def remap_locations(self, mapping: PathMapping) -> int:
        raise Unsupported(self._readonly_reason("Rewriting stored paths"))

    # ---- commands: track metadata ----------------------------------------
    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._require_writable("Editing track metadata")
        self._store.set_track_metadata(track_id, fields)
        self._refresh(track_id)
        return self._index.get(track_id)

    # ---- commands: playlists ----------------------------------------------
    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        self._require_writable("Creating playlists")
        return self._store.create_playlist(name, parent_id)

    def rename_playlist(self, node_id: str, name: str) -> None:
        self._require_writable("Renaming playlists")
        self._store.rename_playlist(node_id, name)

    def delete_playlist(self, node_id: str) -> None:
        self._require_writable("Deleting playlists")
        self._store.delete_playlist(node_id)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        self._require_writable("Editing playlists")
        return self._store.set_playlist_entries(node_id, track_ids)

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._store.dirty

    def save(self):
        self._require_writable("Saving")
        return self._store.save()

    def snapshot(self) -> bytes:
        """The library's bytes, for version history.

        `master.db` alone is NOT a complete snapshot of a Rekordbox library (the
        beatgrids live in separate ANLZ files, and restoring the database
        wholesale would also roll back Rekordbox's own auth and sampler state),
        which is why `capabilities.save.history` is False. Nothing should be
        calling this.
        """
        raise Unsupported("Rekordbox libraries are not versioned by Konduktor")

    # ---- commands: all refused in this milestone -------------------------
    def _readonly_reason(self, what: str) -> str:
        if self._cloud_synced:
            return (
                f"{what} is not possible: this Rekordbox library is synced with "
                "Rekordbox Cloud, and Konduktor will not write to a cloud-synced "
                "library."
            )
        return f"{what} is not supported yet for Rekordbox libraries."

    def _refuse(self, what: str):
        raise Unsupported(self._readonly_reason(what))

    def _require_writable(self, what: str) -> None:
        """Every write passes through here, so the cloud-sync refusal cannot be
        forgotten by a command added later."""
        if self._cloud_synced:
            raise Unsupported(self._readonly_reason(what))

    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None:
        self._refuse("Editing cover art")

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        return None

    def set_cue(self, track_id: str, **kw) -> TrackCues:
        self._refuse("Editing cues")

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues:
        self._refuse("Editing cues")

    def delete_cue(self, track_id: str, slot: int) -> TrackCues:
        self._refuse("Deleting cues")

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> TrackCues:
        self._refuse("Placing cues")

    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def delete_grid_marker(self, track_id: str, index: int) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def replace_grid(self, track_id: str, markers: list) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def set_analysed_grid(self, track_id: str, markers: list) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def delete_grid(self, track_id: str) -> TrackCues:
        self._refuse("Editing the beatgrid")

    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues:
        self._refuse("Locking the beatgrid")
