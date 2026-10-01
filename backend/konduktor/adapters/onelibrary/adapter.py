"""The OneLibrary adapter: owns the drive's database and the generic projection.

Mirrors `adapters.rekordbox.adapter`. It holds:

  * a ``OneLibraryStore`` — the retained NATIVE model (the open drive),
  * a ``TrackIndex`` — the generic READ projection built from it.

**Editable when opened as THE library**, with parity with the Rekordbox adapter
as the goal (decisions: `.claude/discussions/discuss-onelibrary-editing-2026-10-01.md`).
Opened `read_only` — as the sidebar's Devices do, beside the loaded library — it
reports `writable=False` with cause `browsing`, and every command is refused.

What a write must look like was MEASURED by diffing a stick before and after
rekordbox 7 edited it, rather than guessed: the update counters a drive carries
do NOT move, and the `cue` table stays empty. Landing in steps — track metadata
and playlists first; cues, grid, adding and removing tracks after — and every
command not written yet is refused, with its capability flag False so the UI
never offers it.
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

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._store = OneLibraryStore(self.path, read_only=read_only)
        self._index = TrackIndex()
        self._rebuild()

    # ---- projection maintenance -----------------------------------------
    def _rebuild(self) -> None:
        from ...core.stem_file import is_stem_file

        tracks = []
        for row in self._store.iter_content():
            path = self._drive_path(row)
            tracks.append(projection.to_track(
                row, path, stem=bool(path) and is_stem_file(path)))
        self._index.rebuild(tracks)

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
        """Re-read the drive, DISCARDING unsaved edits."""
        self._store.discard()
        self._rebuild()

    def _refresh(self, track_id: str) -> None:
        """Re-project one track after a command, so the UI cannot go stale."""
        from ...core.stem_file import is_stem_file

        row = self._store.content(track_id)
        path = self._drive_path(row)
        self._index.replace(projection.to_track(row, path, stem=bool(path) and is_stem_file(path)))

    # ---- identity --------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(
            device_name=self._store.device_name,
            version=self._store.db_version,
            read_only=self.read_only,
            editable_fields=sorted(OneLibraryStore.EDITABLE_FIELDS),
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
        rows = sorted(self._store.playlists(),
                      key=lambda r: (int(getattr(r, "sequenceNo", 0) or 0), int(r.playlist_id)))
        writable = not self.read_only
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
                # Opened read-only, every flag is False and
                # `capabilities.writable` tells the UI why, once.
                can_add_tracks=writable and kind == "playlist",
                can_reorder=writable and kind == "playlist",
                can_rename=writable,
                can_delete=writable,
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

    def unresolved_path_groups(self) -> list:
        """None, ever: a drive's paths are relative to its own mount point."""
        return []

    def set_session_mappings(self, mappings: list[PathMapping]) -> None:
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

    def remap_locations(self, mapping: PathMapping) -> dict[str, str]:
        raise Unsupported(self._readonly_reason("Rewriting stored paths"))

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._store.dirty

    def save(self):
        self._require_writable("Saving")
        return self._store.save()

    def snapshot(self) -> bytes:
        raise Unsupported("OneLibrary drives are not versioned by Konduktor")

    # ---- commands -----------------------------------------------------------
    def _readonly_reason(self, what: str) -> str:
        if self.read_only:
            return (f"{what} is not possible while the drive is open for browsing. "
                    "Open it for editing to change it.")
        return f"{what} is not supported yet for OneLibrary drives."

    def _refuse(self, what: str):
        raise Unsupported(self._readonly_reason(what))

    def _require_writable(self, what: str) -> None:
        """Every write passes through here, so a command added later cannot
        forget that a browsing drive is read-only."""
        if self.read_only:
            self._refuse(what)

    def add_tracks(self, items: list, *, checkpoint=None) -> list[str]:
        self._refuse("Adding tracks")

    def remove_tracks(self, track_ids: list[str]) -> int:
        self._refuse("Removing tracks")

    def apply_stem_swaps(self, swaps, *, add_to_playlist=None):
        self._refuse("Converting tracks to stems")

    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._require_writable("Editing track metadata")
        self._store.set_track_metadata(track_id, fields)
        self._refresh(track_id)
        return self._index.get(track_id)

    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None:
        self._refuse("Editing cover art")

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        return self._store.cover_art(track_id)

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        self._require_writable("Creating playlists")
        return self._store.create_playlist(name, parent_id)

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        self._require_writable("Creating playlist folders")
        return self._store.create_folder(name, parent_id)

    def rename_playlist(self, node_id: str, name: str) -> None:
        self._require_writable("Renaming playlists")
        self._store.rename_playlist(node_id, name)

    def delete_playlist(self, node_id: str) -> None:
        self._require_writable("Deleting playlists")
        self._store.delete_playlist(node_id)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        self._require_writable("Editing playlists")
        return self._store.set_playlist_entries(node_id, track_ids)

    def set_cue(self, track_id: str, **kw) -> TrackCues:
        self._refuse("Editing cues")

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues:
        self._refuse("Editing cues")

    def set_cue_color(self, track_id: str, slot: int, color: str | None) -> TrackCues:
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
