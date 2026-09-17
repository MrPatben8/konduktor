"""The Traktor adapter: owns the native NML model and the generic projection.

This is the object the app talks to. It holds:

  * a ``PlaylistStore`` — the retained NATIVE model, which is the write target.
    Commands are replayed onto it; it is never regenerated from the projection.
  * a ``TrackIndex`` — the generic READ projection, rebuilt from the store.

Only one parse happens per open, and the projection is refreshed from the same
object graph the commands mutate — so the two cannot drift.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ...core.adapter import InvalidCommand, NotFound, Unsupported
from ...core.capabilities import Capabilities
from ...core.model import PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping
from ...core.query import TrackIndex
from . import capabilities as caps
from . import projection
from .store import PlaylistError, PlaylistStore

# Generic cue vocabulary -> Traktor's CUE_V2 TYPE encoding. This table is the
# only place the integers exist outside the store; type 4 is a grid marker and
# is never a cue, so it has no generic name.
CUE_TYPE_TO_NATIVE = {"cue": 0, "fade_in": 1, "fade_out": 2, "load": 3, "loop": 5}


class TraktorAdapter:
    platform = "traktor"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._store = PlaylistStore(self.path)
        self._index = TrackIndex()
        self._rebuild()

    # ---- projection maintenance ----------------------------------------
    def _rebuild(self) -> None:
        """Re-project every track. Needed after a save or a path remap, where
        track ids themselves change and a per-track patch is impossible."""
        self._index.rebuild([projection.to_track(e) for e in self._store.iter_entries()])

    def _refresh(self, track_id: str) -> TrackCues:
        """Re-project one track after a command, and return its fresh cues."""
        entry = self._store.model_entry(track_id)
        if entry is None:
            return TrackCues()
        self._index.replace(projection.to_track(entry))
        return projection.to_track_cues(entry)

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

    def track_cues(self, track_id: str) -> TrackCues | None:
        entry = self._store.model_entry(track_id)
        return projection.to_track_cues(entry) if entry is not None else None

    # ---- transitional shims --------------------------------------------
    # main.py still drives the store directly and calls replace_track after each
    # command; both go away when the routes move onto the protocol (step 7).
    @property
    def store(self) -> PlaylistStore:
        return self._store

    def entries_for(self, track_ids: list[str]) -> list[tuple[str, str]]:
        return self._store.entries_for(track_ids)

    def replace_track(self, track_id: str, entry=None) -> None:
        self._refresh(track_id)

    def reload(self) -> None:
        self._store._load()
        self._rebuild()

    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(self.path, sorted(PlaylistStore.EDITABLE_FIELDS))

    # ---- playlists -------------------------------------------------------
    def playlist_tree(self) -> list[PlaylistNode]:
        return self._store.tree()

    def playlist_entries(self, node_id: str) -> list[str] | None:
        return self._store.entry_keys(node_id)

    def playlist_tracks(self, node_id: str) -> list[Track] | None:
        keys = self._store.entry_keys(node_id)
        if keys is None:
            return None
        return [t for k in keys if (t := self._index.get(k)) is not None]

    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        with _translate():
            return self._store.create_playlist(name, parent_id)

    def rename_playlist(self, node_id: str, name: str) -> None:
        with _translate():
            self._store.rename_playlist(node_id, name)

    def delete_playlist(self, node_id: str) -> None:
        with _translate():
            self._store.delete_playlist(node_id)

    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int:
        """Replace a playlist's contents. The adapter resolves each id to its
        native entry kind, so callers never see Traktor's STEM/TRACK split."""
        entries = self._store.entries_for(track_ids)
        with _translate():
            self._store.set_entries(node_id, entries)
        return len(entries)

    # ---- metadata / art --------------------------------------------------
    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        with _translate():
            self._store.set_track_metadata(track_id, fields)
        self._refresh(track_id)
        return self._index.get(track_id)

    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None:
        with _translate():
            self._store.set_track_art(track_id, data, mime)

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        return self._store.cover_art(track_id)

    # ---- cues -------------------------------------------------------------
    def _native_cue_type(self, cue_type: str) -> int:
        native = CUE_TYPE_TO_NATIVE.get(cue_type)
        if native is None:
            raise Unsupported(f"Traktor has no cue type {cue_type!r}")
        return native

    def set_cue(
        self,
        track_id: str,
        *,
        slot: int,
        start_sec: float,
        cue_type: str,
        length_sec: float = 0.0,
        role: str = "hotcue",
        name: str | None = None,
    ) -> TrackCues:
        if role != "hotcue":
            # Memory cues are a Rekordbox concept; every Traktor cue holds a slot.
            raise Unsupported("Traktor cues always occupy a hotcue slot")
        with _translate():
            self._store.set_hotcue(
                track_id, slot, start_sec, self._native_cue_type(cue_type), length_sec, name
            )
        return self._refresh(track_id)

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues:
        with _translate():
            self._store.set_hotcue_type(track_id, slot, self._native_cue_type(cue_type))
        return self._refresh(track_id)

    def delete_cue(self, track_id: str, slot: int) -> TrackCues:
        with _translate():
            self._store.delete_hotcue(track_id, slot)
        return self._refresh(track_id)

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> TrackCues:
        with _translate():
            self._store.place_hotcues(track_id, cues, overwrite=overwrite)
        return self._refresh(track_id)

    # ---- beatgrid ---------------------------------------------------------
    def add_grid_marker(self, track_id, start_sec, bpm=None) -> TrackCues:
        with _translate():
            self._store.add_grid_marker(track_id, start_sec, bpm)
        return self._refresh(track_id)

    def move_grid_marker(self, track_id, index, start_sec) -> TrackCues:
        with _translate():
            self._store.move_grid_marker(track_id, index, start_sec)
        return self._refresh(track_id)

    def set_grid_marker_bpm(self, track_id, index, bpm) -> TrackCues:
        with _translate():
            self._store.set_grid_marker_bpm(track_id, index, bpm)
        return self._refresh(track_id)

    def delete_grid_marker(self, track_id, index) -> TrackCues:
        with _translate():
            self._store.delete_grid_marker(track_id, index)
        return self._refresh(track_id)

    def replace_grid(self, track_id, markers: list[tuple[float, float]]) -> TrackCues:
        """Set exactly these markers and nothing else (the deck's Reset)."""
        with _translate():
            self._store.replace_grid(track_id, markers)
        return self._refresh(track_id)

    def set_analysed_grid(self, track_id, markers: list[tuple[float, float]]) -> TrackCues:
        """Write an analysis result the way this platform's own analyser would.

        Traktor pairs its first grid marker with a white beat-1 cue, so a grid it
        wrote looks different from one merely assigned. Keeping that as a
        separate intent is what lets the companion convention stay inside this
        adapter instead of leaking into the generic vocabulary.
        """
        with _translate():
            self._store.replace_grid(track_id, markers)
            self._store.place_grid_companion(track_id, 0, prefer_slot=0)
        return self._refresh(track_id)

    def delete_grid(self, track_id) -> TrackCues:
        with _translate():
            self._store.delete_grid(track_id)
        return self._refresh(track_id)

    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues:
        with _translate():
            self._store.set_lock(track_id, locked)
        return self._refresh(track_id)

    # ---- audio / paths ----------------------------------------------------
    def audio_path(self, track_id: str):
        return self._store.audio_path(track_id)

    def set_path_mapping(self, mapping: PathMapping) -> None:
        self._store.set_path_mapping(mapping)

    def path_prefix_suggestions(self) -> dict:
        return self._store.path_prefix_suggestions()

    def remap_preview(self, mapping: PathMapping) -> dict:
        return self._store.remap_preview(mapping)

    def remap_locations(self, mapping: PathMapping) -> int:
        with _translate():
            n = self._store.remap_locations(mapping)
        # A remap rewrites LOCATIONs, so track ids themselves change — there is
        # no per-track patch that could express this.
        self._rebuild()
        return n

    # ---- save --------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._store.dirty

    def save(self):
        outcome = self._store.save()
        self._rebuild()  # save() re-parses, so every object identity changed
        return outcome

    def snapshot(self) -> bytes:
        return self._store._render()


@contextmanager
def _translate():
    """Map the store's native error onto the generic one the HTTP layer maps."""
    try:
        yield
    except PlaylistError as ex:
        msg = str(ex)
        if "not found" in msg.lower():
            raise NotFound(msg) from ex
        raise InvalidCommand(msg) from ex
