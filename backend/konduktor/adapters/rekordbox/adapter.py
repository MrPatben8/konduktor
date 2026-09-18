"""The Rekordbox adapter: owns the native database and the generic projection.

Mirrors `adapters.traktor.adapter`. It holds:

  * a ``RekordboxStore`` — the retained NATIVE model (the open ``master.db``),
  * a ``TrackIndex`` — the generic READ projection built from it.

**Read-only milestone.** Every mutating command raises `Unsupported`, and
`capabilities()` reports nothing as editable, so a correct UI never offers one
in the first place; the raise is the backstop, not the mechanism.

Writes are refused for a second, permanent reason on a cloud-synced library:
propagating a bad sync state to the user's other machines is not something
version history can undo. That check lives here so it cannot be bypassed by a
future command path forgetting it.
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

    def reload(self) -> None:
        self._store._load()
        self._cloud_synced = self._store.cloud_synced
        self._rebuild()

    # ---- identity --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(self.path, cloud_synced=self._cloud_synced)

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
                can_add_tracks=False,
                can_reorder=False,
                can_rename=False,
                can_delete=False,
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

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return False

    def save(self):
        raise Unsupported(self._readonly_reason("Saving"))

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

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        self._refuse("Creating playlists")

    def rename_playlist(self, node_id: str, name: str) -> None:
        self._refuse("Renaming playlists")

    def delete_playlist(self, node_id: str) -> None:
        self._refuse("Deleting playlists")

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        self._refuse("Editing playlists")

    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._refuse("Editing track metadata")

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
