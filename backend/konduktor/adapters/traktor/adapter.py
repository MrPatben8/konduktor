"""The Traktor adapter: owns the native NML model and the generic projection.

This is the object the app talks to. It holds:

  * a ``TraktorStore`` — the retained NATIVE model, which is the write target.
    Commands are replayed onto it; it is never regenerated from the projection.
  * a ``TrackIndex`` — the generic READ projection, rebuilt from the store.

Only one parse happens per open, and the projection is refreshed from the same
object graph the commands mutate — so the two cannot drift.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ...core.adapter import InvalidCommand, NotFound, Unsupported
from ...core.capabilities import Capabilities
from ...core.model import PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping
from ...core.query import TrackIndex
from . import capabilities as caps
from . import projection
from .cue_types import CUE_TYPE_TO_NATIVE
from .store import PlaylistError, TraktorStore




class TraktorAdapter:
    platform = "traktor"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._store = TraktorStore(self.path)
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
    def store(self) -> TraktorStore:
        return self._store

    def entries_for(self, track_ids: list[str]) -> list[tuple[str, str]]:
        return self._store.entries_for(track_ids)

    def replace_track(self, track_id: str, entry=None) -> None:
        self._refresh(track_id)

    def reload(self) -> None:
        self._store._load()
        self._rebuild()

    def playlist_count(self) -> int:
        return self._store.count_playlists()

    def capabilities(self) -> Capabilities:
        return caps.capabilities_for(self.path, sorted(TraktorStore.EDITABLE_FIELDS))

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

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        with _translate():
            return self._store.create_folder(name, parent_id)

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

    # ---- removing tracks ---------------------------------------------------
    def remove_tracks(self, track_ids: list[str]) -> int:
        with _translate():
            n = self._store.remove_entries(track_ids)
        if n:
            # Rebuilt rather than patched: an entry sharing a removed key went
            # too, and the index would otherwise still hold its projection.
            self._rebuild()
        return n

    # ---- adding tracks ----------------------------------------------------
    def add_tracks(self, items: list) -> list[str]:
        """Add tracks that came from somewhere else, with their prep.

        Two steps, and the split is the point. The store creates a bare ENTRY —
        the only genuinely new code — and then the track's cues and grid are
        written by **replaying the ordinary commands** onto it. So an imported
        track's beatgrid goes in through the same `replace_grid` that the deck's
        Reset button uses, and inherits its companion-cue handling, its TEMPO
        mirroring and its tests, rather than growing a second implementation
        that would drift.

        Lossiness follows the settled rule — degrade to the nearest equivalent,
        drop only where none exists:

          * **Memory cues become hot cues in spare slots.** Traktor has no
            memory cues, and every Traktor cue occupies a slot. Filling the
            leftovers preserves the positions a DJ actually marked; when the
            bank is full the rest are dropped, because there is nowhere left.
          * **Cue colour is dropped.** Traktor derives a cue's colour from its
            type, so a free RGB value has nowhere to go.

        Returns the new track ids, in the order the items were given.
        """
        added: list[str] = []
        for item in items:
            audio = Path(item.audio_path)
            if not audio.is_file():
                raise InvalidCommand(f"No audio file at {audio}")
            with _translate():
                track_id = self._store.add_entry(item.track, audio)
            # Into the projection BEFORE the cues are replayed: those go through
            # the ordinary commands, and those look the track up by id.
            entry = self._store.model_entry(track_id)
            self._index.add(projection.to_track(entry))
            added.append(track_id)
            if item.cues is not None:
                self._apply_imported_cues(track_id, item.cues)
            self._refresh(track_id)
        return added

    def _apply_imported_cues(self, track_id: str, cues) -> None:
        """Replay a generic TrackCues onto a freshly added entry."""
        markers = [(m.start, m.bpm) for m in (cues.grid_markers or [])]
        if markers:
            # `replace_grid`, NOT `set_analysed_grid`. The latter also places
            # Traktor's beat-1 companion cue, and that is wrong twice over here:
            # it invents a cue the source never had (companions are a Traktor
            # convention, and the project's rule is to follow them but never
            # invent one outside Auto Grid), and it occupies pad A — which
            # silently displaced the imported track's own pad A cue.
            self.replace_grid(track_id, markers)

        slots = self.capabilities().cues.hotcue_slots
        taken = {c.slot for c in self.track_cues(track_id).cues if c.slot is not None}
        hotcues = [c for c in (cues.cues or []) if c.role == "hotcue" and c.slot is not None]

        # Hot cues keep the pad they were on wherever that pad is free, because
        # muscle memory is most of what a hot cue layout IS.
        displaced: list = []
        for cue in sorted(hotcues, key=lambda c: c.slot):
            if cue.slot in taken or not (0 <= cue.slot < slots):
                displaced.append(cue)
                continue
            self._place_imported_cue(track_id, cue.slot, cue)
            taken.add(cue.slot)

        # Everything without a pad of its own competes for what is left: memory
        # cues, which Traktor has no equivalent for, and any hot cue whose pad
        # was already occupied. Earliest first, so that when the bank runs out
        # it is the late cues that are lost rather than an arbitrary set — and
        # a displaced hot cue is moved rather than dropped, because losing one
        # silently is much worse than having it turn up on the wrong pad.
        leftovers = displaced + [c for c in (cues.cues or []) if c.role != "hotcue"]
        spare = (s for s in range(slots) if s not in taken)
        for cue in sorted(leftovers, key=lambda c: c.start):
            slot = next(spare, None)
            if slot is None:
                break  # bank full: the rest are dropped, per the lossiness rule
            self._place_imported_cue(track_id, slot, cue)

    def _place_imported_cue(self, track_id: str, slot: int, cue) -> None:
        cue_type = "loop" if getattr(cue, "length", 0) else "cue"
        if cue_type not in CUE_TYPE_TO_NATIVE:
            return
        self.set_cue(
            track_id,
            slot=slot,
            start_sec=cue.start,
            cue_type=cue_type,
            length_sec=getattr(cue, "length", 0.0) or 0.0,
            name=getattr(cue, "name", None),
        )

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
        native = [
            SimpleNamespace(
                slot=c.slot,
                start=c.start,
                name=c.name,
                type=self._native_cue_type(getattr(c, "type", "cue")),
                length=getattr(c, "length", 0.0),
            )
            for c in cues
        ]
        with _translate():
            self._store.place_hotcues(track_id, native, overwrite=overwrite)
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
