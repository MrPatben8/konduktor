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
do NOT move, and the `cue` table stays empty. Landing in steps — track metadata,
playlists, hot cues and the beatgrid so far; adding and removing tracks next —
and every command not written yet is refused, with its capability flag False so
the UI never offers it.
"""
from __future__ import annotations

from pathlib import Path

from ...core.adapter import InvalidCommand, Unsupported
from ...core.capabilities import Capabilities
from ...core.model import GridMarker, HotcueChip, PlaylistNode, Track, TrackCues
from ...core.pathmap import PathMapping, common_dir_prefix
from ...core.query import TrackIndex
from ..rekordbox import palette
from ..rekordbox.cue_types import WRITABLE_CUE_TYPES
from ...exporter import MANIFEST_NAME
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
        # Through the store: an added track plays from its incoming copy until
        # Save puts it in place.
        resolved = self._store.resolve_audio(getattr(row, "path", None))
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
            # A drive an export set wrote carries the export's manifest at its root.
            managed_by_export=(self._store.layout.root / MANIFEST_NAME).is_file(),
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
        outcome = self._store.save()
        # Save MOVES files (an added track's audio leaves its incoming copy), so
        # the projection is rebuilt from what is now on the drive.
        self._rebuild()
        return outcome

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

    # ---- audio placement (`tracks.places_audio`) ----------------------------
    def audio_home(self) -> Path:
        return self._store.audio_home()

    def place_audio(self, source: Path, track) -> Path | None:
        self._require_writable("Adding tracks")
        return self._store.place_audio(source, track)

    def add_tracks(self, items: list, *, checkpoint=None) -> list[str]:
        """Add tracks, each arriving ready to prep — as the Rekordbox adapter does.

        A drive keeps the beatgrid, cues and waveforms in per-track analysis
        files, and rekordbox shows neither grid nor cues without them, so every
        file is decoded ONCE here and its `.DAT`/`.EXT`/`.2EX` built by the
        OneLibrary exporter's own writer — the files rekordbox 7 was verified
        to read. A file with no grid of its own gets Konduktor's.

        Two phases, as in Rekordbox: FIRST the slow, cancellable part (decoding,
        grid detection, building the analysis and artwork bytes) before the
        library is touched; THEN the rows, with hot cues replayed through the
        ordinary `set_cue` so they keep its conventions. Memory cues — and a hot
        cue with no free pad — cross as memory cues, written with the files.
        `audio_path` is where `place_audio` said to copy it (or the file itself,
        already on the stick); Save moves copies into place.
        """
        from ...core import audio_tags, grid_detect, waveform
        from ...core.export import ExportTrack
        from ..rekordbox import artwork, timebase
        from .export import OneLibraryExporter

        self._require_writable("Adding tracks")
        exporter = OneLibraryExporter()
        prepared = []
        total = len(items)
        for n, item in enumerate(items, start=1):
            audio = Path(item.audio_path)
            if checkpoint is not None:
                checkpoint(f"Analysing {item.track.title or audio.name} ({n}/{total})", step=n, of=total)
            if not audio.is_file():
                raise InvalidCommand(f"No audio file at {audio}")
            try:
                rel = self._store.drive_relative(self._store.final_of(audio))
            except ValueError:
                raise InvalidCommand(f"{audio} is not on this drive") from None
            samples = waveform.decode(audio)
            measured = waveform.analyse_samples(samples, lead=timebase.offset(audio))
            markers = list(item.cues.grid_markers) if item.cues else []
            if not markers and samples is not None:
                try:
                    found = grid_detect.detect_grid(str(audio), y=samples, sr=waveform.SR)
                    markers = [GridMarker(start=found.anchor, bpm=found.bpm)]
                except ValueError:
                    pass  # no pulse to fit (a one-shot, silence): no grid
            hot, memory, taken = [], [], set()
            for cue in sorted((item.cues.cues if item.cues else []), key=lambda c: c.start):
                if (cue.role == "hotcue" and cue.slot is not None
                        and 0 <= cue.slot < caps.HOTCUE_SLOTS and cue.slot not in taken):
                    hot.append(cue)
                    taken.add(cue.slot)
                else:
                    memory.append(cue.model_copy(update={"role": "memory", "slot": None}))
            export_track = ExportTrack(track=item.track, destination=audio,
                                       cues=TrackCues(grid_markers=markers, cues=memory))
            # Decoded once above; the writer must not decode it again.
            export_track.waveform = lambda *, lead=0.0, m=measured: m
            art = item.art or audio_tags.read_cover(audio)
            jpegs = artwork.pioneer_jpegs(art[0]) if art else None
            length = item.track.length or (int(measured.duration) if measured else None)
            prepared.append((item, audio, rel, exporter.anlz_files(export_track, rel), jpegs,
                             markers, hot, length))

        added: list[str] = []
        for item, audio, rel, anlz, jpegs, markers, hot, length in prepared:
            track_id = self._store.add_track(
                audio, item.track, rel=rel, anlz_rel=self._store._anlz_rel(rel), anlz=anlz,
                jpegs=jpegs, bpm=markers[0].bpm if markers else item.track.bpm, length=length)
            for cue in hot:
                self._store.set_cue(track_id, slot=cue.slot, start_sec=cue.start,
                                    cue_type="loop" if cue.length else "cue",
                                    length_sec=cue.length or 0.0, name=cue.name)
                code, _rgb = palette.code_for(cue.color)
                if cue.color and code:
                    self._store.set_cue_color(track_id, cue.slot, code)
            added.append(track_id)
        self._rebuild()
        return added

    def remove_tracks(self, track_ids: list[str]) -> int:
        """Remove tracks — and, on a stick, their audio and analysis files at Save
        (decision 3: a stick's audio belongs to its library)."""
        self._require_writable("Removing tracks")
        n = self._store.remove_tracks(track_ids)
        if n:
            self._rebuild()
        return n

    def apply_stem_swaps(self, swaps, *, add_to_playlist=None):
        self._refuse("Converting tracks to stems")

    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None:
        self._require_writable("Editing track metadata")
        self._store.set_track_metadata(track_id, fields)
        self._refresh(track_id)
        return self._index.get(track_id)

    def set_key(self, track_id: str, wheel: int, mode: str) -> Track | None:
        self._require_writable("Setting the key")
        self._store.set_key(track_id, wheel, mode)
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

    def _refresh_cues(self, track_id: str) -> TrackCues:
        """Re-project a track AND return its fresh cues — every cue/grid command
        ends here, so a route can never serve a stale projection."""
        self._refresh(track_id)
        return self.track_cues(track_id) or TrackCues()

    def _check_cue_type(self, cue_type: str) -> None:
        if cue_type not in WRITABLE_CUE_TYPES:
            raise Unsupported(f"OneLibrary has no cue type {cue_type!r}")

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
        self._require_writable("Editing cues")
        if role != "hotcue":
            # Preserved-but-uneditable, exactly as in the Rekordbox adapter.
            raise Unsupported(
                "Memory cues are shown but cannot be edited — only hot cues are writable"
            )
        self._check_cue_type(cue_type)
        self._store.set_cue(track_id, slot=slot, start_sec=start_sec, cue_type=cue_type,
                            length_sec=length_sec, name=name)
        return self._refresh_cues(track_id)

    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues:
        self._require_writable("Editing cues")
        self._check_cue_type(cue_type)
        self._store.set_cue_type(track_id, slot, cue_type)
        return self._refresh_cues(track_id)

    def set_cue_color(self, track_id: str, slot: int, color: str | None) -> TrackCues:
        self._require_writable("Editing cues")
        code = None
        if color is not None:
            code = palette.swatch_code(color)
            if code is None:
                raise InvalidCommand(f"{color!r} is not one of rekordbox's hot cue colours")
        self._store.set_cue_color(track_id, slot, code)
        return self._refresh_cues(track_id)

    def delete_cue(self, track_id: str, slot: int) -> TrackCues:
        self._require_writable("Deleting cues")
        self._store.delete_cue(track_id, slot)
        return self._refresh_cues(track_id)

    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> TrackCues:
        self._require_writable("Placing cues")
        for cue in cues:
            self._check_cue_type(getattr(cue, "type", "cue"))
        self._store.place_cues(track_id, cues, overwrite=overwrite)
        return self._refresh_cues(track_id)

    # ---- commands: beatgrid -----------------------------------------------
    @staticmethod
    def _markers_from(markers: list) -> list:
        """Accept either GridMarkers or the (start, bpm) tuples routes send."""
        return [m if isinstance(m, GridMarker) else GridMarker(start=float(m[0]), bpm=float(m[1]))
                for m in markers]

    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.add_grid_marker(track_id, start_sec, bpm)
        return self._refresh_cues(track_id)

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.move_grid_marker(track_id, index, start_sec)
        return self._refresh_cues(track_id)

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.set_grid_marker_bpm(track_id, index, bpm)
        return self._refresh_cues(track_id)

    def delete_grid_marker(self, track_id: str, index: int) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.delete_grid_marker(track_id, index)
        return self._refresh_cues(track_id)

    def replace_grid(self, track_id: str, markers: list) -> TrackCues:
        self._require_writable("Editing the beatgrid")
        self._store.replace_grid(track_id, self._markers_from(markers))
        return self._refresh_cues(track_id)

    def set_analysed_grid(self, track_id: str, markers: list) -> TrackCues:
        """An analysis result, as rekordbox's analyser would write it: nothing is
        paired with a marker (the companion cue is Traktor's), so a replace."""
        return self.replace_grid(track_id, markers)

    def delete_grid(self, track_id: str) -> TrackCues:
        self._require_writable("Deleting the beatgrid")
        self._store.delete_grid(track_id)
        return self._refresh_cues(track_id)

    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues:
        raise Unsupported("OneLibrary has no beatgrid lock")
