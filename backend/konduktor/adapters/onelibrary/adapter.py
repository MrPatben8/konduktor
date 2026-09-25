"""The OneLibrary adapter: owns the drive's database and the generic projection.

Mirrors `adapters.rekordbox.adapter`. It holds:

  * a ``OneLibraryStore`` — the retained NATIVE model (the open drive),
  * a ``TrackIndex`` — the generic READ projection built from it.

**Read-only.** Every command is refused and `capabilities.writable` is False with
cause `platform_incomplete`, so the UI never offers an edit in the first place;
the raises here are the backstop, not the mechanism.

That scope is deliberate rather than provisional. The purpose of this adapter is
to make a OneLibrary drive a readable SOURCE — the half an import into another
library needs — and every genuinely unknown part of the format sits on the write
side: the `cue` table's eight MPEG frame/offset columns, whose values a player
needs to seek accurately in a VBR file; the waveform tags a CDJ draws from; and
the update counters a drive uses to reconcile edits made on hardware. None of
those has to be guessed at to read a drive, and none of them should be guessed at
at all.
"""
from __future__ import annotations

from pathlib import Path

from ...core.adapter import Unsupported
from ...core.capabilities import Capabilities
from ...core.model import HotcueChip, PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping, common_dir_prefix
from ...core.query import TrackIndex
from . import capabilities as caps
from . import projection
from .store import OneLibraryStore

# `playlist.attribute`, from pyrekordbox's own enum. A drive carries no smart
# playlists — rekordbox resolves them to static lists on export, because a CDJ
# cannot evaluate rules — but the value is handled rather than assumed absent.
ATTRIBUTE_PLAYLIST = 0
ATTRIBUTE_FOLDER = 1
ATTRIBUTE_SMART = 4


class OneLibraryAdapter:
    platform = "onelibrary"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._store = OneLibraryStore(self.path)
        self._index = TrackIndex()
        self._rebuild()

    # ---- projection maintenance -----------------------------------------
    def _rebuild(self) -> None:
        self._index.rebuild(
            [
                projection.to_track(row, self._drive_path(row))
                for row in self._store.iter_content()
            ]
        )

    def _drive_path(self, row) -> str | None:
        resolved = self._store.layout.resolve(getattr(row, "path", None))
        return str(resolved) if resolved is not None else None

    def close(self) -> None:
        """Release the database and its file handle.

        More than housekeeping here: the library is on a removable drive, and a
        held handle is what stops it ejecting.
        """
        self._store.close()

    def reload(self) -> None:
        self._store._load()
        self._rebuild()

    # ---- identity --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(
            device_name=self._store.device_name,
            version=self._store.db_version,
        )

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
        """A track's cues and beatgrid — and where the approximate counts get fixed.

        `projection.to_track` cannot afford the track's analysis files at open
        (~23 ms each for the `.EXT`), so it reports no cues and a guessed marker
        count. This is the moment the real numbers exist, so the projected track
        is corrected here rather than being left disagreeing with the deck.
        """
        if self._index.get(track_id) is None:
            return None
        cues = self._store.cues(track_id)
        grid = self._store.anlz_grid(track_id)
        from . import beatgrid

        markers = beatgrid.markers_from_beats(*grid) if grid is not None else []
        result = projection.to_track_cues(cues, markers)

        track = self._index.get(track_id)
        if track is not None:
            track.grid_marker_count = len(result.grid_markers)
            track.cue_count = len(result.cues)
            track.hotcues = sorted(
                (
                    HotcueChip(slot=c.slot, type=c.type, color=c.color)
                    for c in result.cues
                    if c.role == "hotcue" and c.slot is not None
                ),
                key=lambda chip: chip.slot,
            )
            track.hotcue_count = len(track.hotcues)
        return result

    # ---- playlists -------------------------------------------------------
    def playlist_tree(self) -> list[PlaylistNode]:
        """The drive's playlist tree: a flat table with `playlist_id_parent` links.

        A root node's parent is 0, not NULL — so "no parent" is a value here
        rather than an absence, and treating 0 as a real id would orphan every
        top-level playlist.
        """
        rows = self._store.playlists()
        nodes: dict[str, PlaylistNode] = {}
        parents: dict[str, str] = {}

        for r in rows:
            attribute = int(getattr(r, "attribute", 0) or 0)
            if attribute == ATTRIBUTE_FOLDER:
                kind = "folder"
            elif attribute == ATTRIBUTE_SMART:
                kind = "smart"
            else:
                kind = "playlist"
            node_id = str(getattr(r, "playlist_id", ""))
            nodes[node_id] = PlaylistNode(
                id=node_id,
                name=getattr(r, "name", "") or "",
                kind=kind,
                count=(0 if kind == "folder" else len(self._store.playlist_song_ids(node_id))),
                children=[],
                selectable=kind == "playlist",
                # Read-only: every per-node flag is False, and
                # `capabilities.writable` tells the UI why, once.
                can_add_tracks=False,
                can_reorder=False,
                can_rename=False,
                can_delete=False,
                can_contain_children=kind == "folder",
            )
            parent = str(getattr(r, "playlist_id_parent", 0) or 0)
            parents[node_id] = "root" if parent in ("0", "", "None") else parent

        roots: list[PlaylistNode] = []
        for node_id, node in nodes.items():
            parent = nodes.get(parents.get(node_id, "root"))
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
        return any(str(getattr(r, "playlist_id", "")) == str(node_id) for r in self._store.playlists())

    # ---- audio / paths ----------------------------------------------------
    def audio_path(self, track_id: str):
        return self._store.audio_path(track_id)

    def set_path_mapping(self, mapping: PathMapping) -> None:
        """Ignored: a drive's paths are relative to wherever it is mounted.

        Path remapping exists because a desktop library stores absolute paths
        that go stale when music moves. A OneLibrary drive stores everything
        relative to its own root, so it is self-contained by construction and
        there is nothing for a mapping to fix.
        """
        return None

    def path_prefix_suggestions(self) -> dict:
        paths = self._store.all_audio_paths()
        primary = common_dir_prefix(paths)
        return {
            "primary": primary,
            "groups": [{"prefix": primary, "count": len(paths)}] if primary else [],
        }

    def remap_preview(self, mapping: PathMapping) -> dict:
        paths = [Path(p) for p in self._store.all_audio_paths()]
        return {"total": len(paths), "matched": 0, "existing": 0, "samples": []}

    def remap_locations(self, mapping: PathMapping) -> int:
        raise Unsupported(self._readonly_reason("Rewriting stored paths"))

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return False

    def save(self):
        raise Unsupported(self._readonly_reason("Saving"))

    def snapshot(self) -> bytes:
        raise Unsupported("OneLibrary drives are not versioned by Konduktor")

    # ---- commands: all refused -------------------------------------------
    def _readonly_reason(self, what: str) -> str:
        return f"{what} is not supported yet for OneLibrary drives."

    def _refuse(self, what: str):
        raise Unsupported(self._readonly_reason(what))

    def add_tracks(self, items: list) -> list[str]:
        self._refuse("Adding tracks")

    def remove_tracks(self, track_ids: list[str]) -> int:
        self._refuse("Removing tracks")

    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._refuse("Editing track metadata")

    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None:
        self._refuse("Editing cover art")

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        return None

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        self._refuse("Creating playlists")

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        self._refuse("Creating playlist folders")

    def rename_playlist(self, node_id: str, name: str) -> None:
        self._refuse("Renaming playlists")

    def delete_playlist(self, node_id: str) -> None:
        self._refuse("Deleting playlists")

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        self._refuse("Editing playlists")

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
        self._refuse("Deleting the beatgrid")

    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues:
        raise Unsupported("OneLibrary has no beatgrid lock")
