"""Editable playlist store backed by the traktor-nml-utils dataclass model.

Design goals:
  * minimal diff — unedited playlists must serialize byte-for-byte as Traktor
    wrote them (the xsdata serializer used by the library reproduces Traktor's
    exact formatting; lxml does not),
  * surgical — only the objects actually edited differ in the output; the rest
    of the ~14 MB file renders byte-identically,
  * safe — every save is committed to the collection's version history (see
    ``history.py``); the commit is additive and never alters the saved bytes.

We edit the parsed dataclass model in memory and, on save, render the WHOLE
model through the library's own pipeline — proven byte-identical for unedited
content, so no splicing is needed.

Playlists are identified by their stable UUID; folders by a synthetic path id
(``fld:<name>/<name>``). Smart playlists are read-only.
"""
from __future__ import annotations

import threading
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

from traktor_nml_utils import (
    TraktorCollection,
    format_traktor_layout,
    restore_traktor_float_format,
)
from traktor_nml_utils.models.collection import (
    Albumtype,
    CueV2Type,
    Entrytype,
    GridType,
    Infotype,
    Locationtype,
    Nodetype,
    Playlisttype,
    Primarykeytype,
    Subnodestype,
    Tempotype,
)
from xsdata.formats.dataclass.serializers import XmlSerializer

from ...core.adapter import InvalidCommand, SaveOutcome
from ...core.edit_journal import EditJournal
from ...core.pathmap import common_dir_prefix
from ...core.pathmap import PathMapping
from ...schemas import PlaylistNode
from . import beatgrid
from .locations import os_path_to_location, resolve_path

# Traktor's POPM frame owner: an ID3 rating is per-owner, so writing under
# this email is what makes the stars show up in Traktor itself.
TRAKTOR_POPM_EMAIL = "traktor@native-instruments.de"


class PlaylistError(InvalidCommand):
    """A command this library cannot accept.

    Subclasses the generic `InvalidCommand` so the HTTP layer maps it without
    knowing Traktor exists; the name is kept because it is what every call site
    and the fidelity tests already catch.
    """


@dataclass
class FileTagResult:
    track_id: str
    file: str
    ok: bool
    status: str  # "written" | "file-not-found" | "unsupported-format" | "error"
    detail: str = ""


class TraktorStore:
    def __init__(self, nml_path: Path):
        self.nml_path = Path(nml_path)
        self._lock = threading.RLock()
        self.dirty = False
        # Active OS-path prefix remapping (empty = identity). Survives _load().
        self._path_mapping = PathMapping()
        self._load()

    # ---- load ----------------------------------------------------------
    def _load(self) -> None:
        self._collection = TraktorCollection(path=self.nml_path)
        self._nml = self._collection.nml
        # Index collection entries by their Traktor primary key (volume+dir+file)
        # so track-metadata edits can find their ENTRY in O(1).
        self._entry_by_key: dict[str, Entrytype] = {}
        for e in self._nml.collection.entry:
            loc = e.location
            if loc:
                key = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"
                self._entry_by_key[key] = e
        # Every edit this session, at field resolution: drives the file-tag sync,
        # the version-history message, and (later) undo.
        self._journal = EditJournal()
        # track_id -> (image_bytes, mime) of staged replacement cover art
        self._track_art: dict[str, tuple[bytes, str]] = {}
        self.dirty = False

    def _note(
        self,
        category: str,
        detail: str | None = None,
        track_id: str | None = None,
        target: str | None = None,
    ) -> None:
        """Record one command in the edit journal.

        `category` names the scope and `detail` the operation; playlists carry
        their name as the target instead of a track id, and a remap carries its
        count. `target` optionally names the field/slot/marker touched.
        """
        if category.startswith("playlist-"):
            self._journal.record("playlist", category.split("-", 1)[1], detail)
        elif category == "remap":
            self._journal.record("library", "remap", after=detail)
        elif category == "hotcue":
            self._journal.record("cue", detail or "modify", track_id, target)
        else:
            self._journal.record(category, detail or "edit", track_id, target)

    def _root(self) -> Nodetype:
        node = self._nml.playlists.node if self._nml.playlists else None
        if node is None:
            raise PlaylistError("PLAYLISTS has no root NODE")
        return node

    @staticmethod
    def _children(node: Nodetype) -> list[Nodetype]:
        return list(node.subnodes.node) if node.subnodes else []

    # ---- tree walking --------------------------------------------------
    def tree(self) -> list[PlaylistNode]:
        with self._lock:
            return [self._to_model(c, []) for c in self._children(self._root())]

    def _to_model(self, node: Nodetype, parent_path: list[str]) -> PlaylistNode:
        ntype = node.type or "FOLDER"
        name = node.name or "(unnamed)"
        if ntype == "PLAYLIST" or node.playlist is not None:
            pl = node.playlist
            keys = self._entry_keys_of(pl) if pl is not None else []
            return PlaylistNode(
                id=pl.uuid if pl and pl.uuid else name,
                name=name,
                kind="playlist",
                count=len(keys),
                selectable=True,
                can_add_tracks=True,
                can_reorder=True,
                can_rename=True,
                can_delete=True,
            )
        if ntype == "SMARTLIST" or node.smartplaylist is not None:
            # Rule-based, so it has no static entry list to show or edit.
            return PlaylistNode(
                id="sl:" + "/".join(parent_path + [name]), name=name, kind="smart"
            )
        path = parent_path + [name]
        return PlaylistNode(
            id="fld:" + "/".join(path),
            name=name,
            kind="folder",
            children=[self._to_model(c, path) for c in self._children(node)],
            can_contain_children=True,
        )

    @staticmethod
    def _entry_keys_of(pl: Playlisttype) -> list[str]:
        return [
            e.primarykey.key
            for e in (pl.entry or [])
            if e.primarykey and e.primarykey.key
        ]

    # ---- lookups -------------------------------------------------------
    def _iter_nodes(self, node: Nodetype):
        yield node
        for c in self._children(node):
            yield from self._iter_nodes(c)

    def _find_playlist_node(self, playlist_uuid: str) -> Nodetype | None:
        for n in self._iter_nodes(self._root()):
            if n.playlist is not None and n.playlist.uuid == playlist_uuid:
                return n
        return None

    def _find_parent_of(self, target: Nodetype) -> Nodetype | None:
        for n in self._iter_nodes(self._root()):
            if n.subnodes and target in n.subnodes.node:
                return n
        return None

    def _find_folder(self, folder_id: str | None) -> Nodetype:
        root = self._root()
        if folder_id in (None, "", "fld:"):
            return root
        if not folder_id.startswith("fld:"):
            raise PlaylistError(f"Not a folder id: {folder_id}")
        cur = root
        for name in folder_id[len("fld:") :].split("/"):
            match = next(
                (
                    c
                    for c in self._children(cur)
                    if (c.type or "FOLDER") == "FOLDER" and c.name == name
                ),
                None,
            )
            if match is None:
                raise PlaylistError(f"Folder not found: {folder_id}")
            cur = match
        return cur

    def entry_keys(self, playlist_uuid: str) -> list[str] | None:
        with self._lock:
            node = self._find_playlist_node(playlist_uuid)
            return None if node is None else self._entry_keys_of(node.playlist)

    # ---- edits ---------------------------------------------------------
    def create_playlist(self, name: str, parent_id: str | None = None) -> str:
        with self._lock:
            folder = self._find_folder(parent_id)
            if folder.subnodes is None:
                folder.subnodes = Subnodestype(node=[], count=0)
            new_uuid = uuidlib.uuid4().hex
            node = Nodetype(
                type="PLAYLIST",
                name=name,
                playlist=Playlisttype(entry=[], entries=0, type="LIST", uuid=new_uuid),
            )
            folder.subnodes.node.append(node)
            folder.subnodes.count = len(folder.subnodes.node)
            self._note("playlist-create", name)
            self.dirty = True
            return new_uuid

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Create a playlist FOLDER and return its synthetic id.

        A folder has no UUID of its own in the NML — it is identified by its path
        of names — so the id is built the same way `_to_model` builds it, and the
        two must agree or the folder will be invisible to the very next call.

        An existing folder of the same name in the same parent is RETURNED rather
        than duplicated. Import creates a folder named after the drive, and a
        second import from the same stick should land beside the first rather
        than next to an identically-named twin.
        """
        with self._lock:
            parent = self._find_folder(parent_id)
            existing = next(
                (
                    c
                    for c in self._children(parent)
                    if (c.type or "FOLDER") == "FOLDER" and c.name == name
                ),
                None,
            )
            prefix = parent_id[len("fld:"):] if parent_id and parent_id.startswith("fld:") else ""
            folder_id = "fld:" + "/".join([p for p in (prefix, name) if p])
            if existing is not None:
                return folder_id
            if parent.subnodes is None:
                parent.subnodes = Subnodestype(node=[], count=0)
            parent.subnodes.node.append(
                Nodetype(type="FOLDER", name=name, subnodes=Subnodestype(node=[], count=0))
            )
            parent.subnodes.count = len(parent.subnodes.node)
            self._note("playlist-create-folder", name)
            self.dirty = True
            return folder_id

    def rename_playlist(self, playlist_uuid: str, name: str) -> None:
        with self._lock:
            node = self._find_playlist_node(playlist_uuid)
            if node is None:
                raise PlaylistError(f"Playlist not found: {playlist_uuid}")
            node.name = name
            self._note("playlist-rename", name)
            self.dirty = True

    def delete_playlist(self, playlist_uuid: str) -> None:
        with self._lock:
            node = self._find_playlist_node(playlist_uuid)
            if node is None:
                raise PlaylistError(f"Playlist not found: {playlist_uuid}")
            parent = self._find_parent_of(node)
            if parent is None:
                raise PlaylistError("Cannot delete a top-level node")
            parent.subnodes.node.remove(node)
            parent.subnodes.count = len(parent.subnodes.node)
            self._note("playlist-delete", node.name)
            self.dirty = True

    def set_entries(self, playlist_uuid: str, entries: list[tuple[str, str]]) -> None:
        with self._lock:
            node = self._find_playlist_node(playlist_uuid)
            if node is None:
                raise PlaylistError(f"Playlist not found: {playlist_uuid}")
            pl = node.playlist
            pl.entry = [
                Entrytype(primarykey=Primarykeytype(type=ptype or "TRACK", key=key))
                for key, ptype in entries
            ]
            pl.entries = len(pl.entry)
            self._note("playlist-entries", node.name)
            self.dirty = True

    # ---- track metadata editing ---------------------------------------
    # Safe, free-text-ish fields only. Deliberately NOT editable here: file path
    # (it's the primary key), bpm/key (audio/grid territory), and read-only info
    # like bitrate/playcount.
    _INFO_FIELDS = {"genre", "label", "remixer", "producer", "comment", "mix", "release_date"}
    EDITABLE_FIELDS = {"title", "artist", "album", "rating"} | _INFO_FIELDS

    def set_track_metadata(self, track_id: str, fields: dict) -> None:
        with self._lock:
            entry = self._entry_by_key.get(track_id)
            if entry is None:
                raise PlaylistError(f"Track not found: {track_id}")
            if entry.info is None:
                entry.info = Infotype()
            for k, v in fields.items():
                if k in ("title", "artist"):
                    setattr(entry, k, v or None)
                elif k == "album":
                    if entry.album is None:
                        entry.album = Albumtype()
                    entry.album.title = v or None
                elif k in self._INFO_FIELDS:
                    setattr(entry.info, k, v or None)
                elif k == "rating":
                    stars = max(0, min(5, int(v))) if v is not None else 0
                    # Traktor RANKING = stars * 51; unrated has no RANKING attr.
                    entry.info.ranking = stars * 51 or None
                else:
                    continue  # unknown / read-only fields are ignored
                self._journal.record("track", "set", track_id, k)
            self.dirty = True

    # ---- adding tracks --------------------------------------------------
    def add_entry(self, track, audio_path: Path) -> str:
        """Append a brand-new ENTRY for `audio_path` and return its track id.

        The one place Konduktor builds a collection entry instead of editing one
        Traktor wrote. Two rules make that safe:

          * **Append only.** The new ENTRY goes on the end of COLLECTION and
            nothing else is touched, so every existing entry still renders the
            bytes it was parsed from. `test_save_fidelity.py` checks exactly
            that rather than trusting it.
          * **The primary key must be free.** Traktor keys playlist entries on
            ``volume+dir+file``, so two ENTRYs sharing one is a corrupt
            collection, not a duplicate track. Importing copies audio to a fresh
            filename, which is what keeps repeat imports honest.

        Deliberately left empty: ``audio_id`` (Traktor's analysis fingerprint)
        and ``loudness``. Both are outputs of Traktor's own analysis and cannot
        be computed here; Traktor re-analyses a track that has none. Writing a
        plausible-looking fingerprint would be inventing data.
        """
        with self._lock:
            volume, dir_, file = os_path_to_location(Path(audio_path))
            key = f"{volume}{dir_}{file}"
            if key in self._entry_by_key:
                raise PlaylistError(
                    f"The collection already has an entry for {audio_path}"
                )

            info = Infotype(
                genre=getattr(track, "genre", None) or None,
                label=getattr(track, "label", None) or None,
                comment=getattr(track, "comment", None) or None,
                remixer=getattr(track, "remixer", None) or None,
                producer=getattr(track, "producer", None) or None,
                mix=getattr(track, "mix", None) or None,
                key=getattr(track, "key", None) or None,
                bitrate=getattr(track, "bitrate", None) or None,
                playcount=getattr(track, "playcount", None) or None,
                release_date=getattr(track, "release_date", None) or None,
                import_date=getattr(track, "import_date", None) or None,
                # Traktor's RANKING is stars x 51; an unrated track has no
                # attribute at all rather than a zero.
                ranking=(max(0, min(5, int(getattr(track, "rating", 0) or 0))) * 51) or None,
                playtime=getattr(track, "length", None) or None,
            )
            album = None
            if getattr(track, "album", None):
                album = Albumtype(title=track.album)

            bpm = getattr(track, "bpm", None)
            entry = Entrytype(
                location=Locationtype(volume=volume, dir=dir_, file=file),
                title=getattr(track, "title", None) or None,
                artist=getattr(track, "artist", None) or None,
                album=album,
                info=info,
                # TEMPO mirrors the first grid marker. It is set here from the
                # projected BPM so a track with no grid still shows a tempo;
                # `replace_grid` overwrites it from the markers when they land.
                tempo=Tempotype(bpm=float(bpm)) if bpm else None,
                cue_v2=[],
            )
            self._nml.collection.entry.append(entry)
            self._entry_by_key[key] = entry
            # `<COLLECTION ENTRIES="N">` is a real count Traktor writes and
            # reads, not decoration. No existing command changes how many
            # entries there are, so nothing has ever had to maintain it —
            # leaving it stale is a corrupt file that still looks well-formed.
            self._nml.collection.entries = len(self._nml.collection.entry)
            self._note("track", "add", key)
            self.dirty = True
            return key

    def iter_entries(self):
        """Every collection ENTRY in document order.

        Document order, not ``_entry_by_key.values()``: two entries can share a
        primary key, and the dict would silently drop one of them.
        """
        with self._lock:
            return list(self._nml.collection.entry)

    def stem_keys(self) -> set[str]:
        """Track ids whose ENTRY has a <STEMS> child.

        Playlist entries for these use ``PRIMARYKEY TYPE="STEM"`` (verified
        945/945 against the real collection); everything else uses ``TRACK``.
        """
        with self._lock:
            return {
                f"{e.location.volume or ''}{e.location.dir or ''}{e.location.file or ''}"
                for e in self._nml.collection.entry
                if e.location and getattr(e, "stems", None) is not None
            }

    def entries_for(self, track_ids: list[str]) -> list[tuple[str, str]]:
        """Pair each known track id with its PRIMARYKEY TYPE, dropping unknowns."""
        stems = self.stem_keys()
        with self._lock:
            known = self._entry_by_key
            return [
                (tid, "STEM" if tid in stems else "TRACK")
                for tid in track_ids
                if tid in known
            ]

    def model_entry(self, track_id: str) -> Entrytype | None:
        with self._lock:
            return self._entry_by_key.get(track_id)

    # ---- hotcues ------------------------------------------------------
    # Creatable/editable types: 0 cue, 1 fade-in, 2 fade-out, 3 load, 5 loop.
    # A loop (type 5) carries a length; the point types don't. Grid markers
    # (type 4, redefine the beatgrid) are excluded. Cues are NML-only.
    POINT_CUE_TYPES = {0, 1, 2, 3}  # types the dropdown can switch between
    CREATABLE_TYPES = {0, 1, 2, 3, 5}

    def _entry_or_raise(self, track_id: str) -> Entrytype:
        entry = self._entry_by_key.get(track_id)
        if entry is None:
            raise PlaylistError(f"Track not found: {track_id}")
        return entry

    def set_hotcue(
        self,
        track_id: str,
        slot: int,
        start_sec: float,
        cue_type: int,
        length_sec: float = 0.0,
        name: str | None = None,
    ) -> None:
        """Create (or reposition + retype) the hotcue in `slot` at `start_sec`.

        `length_sec` > 0 makes it a loop (used with cue_type 5). `name` sets the
        cue label on create; when unset it falls back to Traktor's "n.n."."""
        if not 0 <= slot <= 7:
            raise PlaylistError(f"Invalid hotcue slot: {slot}")
        if cue_type not in self.CREATABLE_TYPES:
            raise PlaylistError(f"Unsupported cue type: {cue_type}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            start_ms = max(0.0, start_sec * 1000.0)  # Traktor stores START/LEN in ms
            len_ms = max(0.0, length_sec * 1000.0)
            existing = next(
                (c for c in (entry.cue_v2 or []) if c.hotcue == slot), None
            )
            if existing is not None:
                existing.start = start_ms
                existing.type = cue_type
                existing.len = len_ms
            else:
                if entry.cue_v2 is None:
                    entry.cue_v2 = []
                entry.cue_v2.append(
                    CueV2Type(
                        name=name or "n.n.",  # Traktor's default name for a manual cue
                        displ_order=0,
                        type=cue_type,
                        start=start_ms,
                        len=len_ms,
                        repeats=-1,
                        hotcue=slot,
                        color=None,
                        grid=None,
                    )
                )
            # "add" only when it's a genuinely new hotcue; repositioning/retyping
            # an existing slot is a "modify" (keeps the net add/remove count honest).
            self._note(
                "hotcue", "modify" if existing is not None else "add", track_id, f"slot:{slot}"
            )
            self.dirty = True

    def place_hotcues(
        self, track_id: str, specs: list, *, overwrite: bool = False
    ) -> None:
        """Batch-create hotcues from `specs` (each has .slot/.start/.name and
        optional .type/.length). With `overwrite` False (the default), slots that
        already hold a hotcue are skipped so hand-placed cues are never clobbered.
        Used by Auto Hotcues."""
        with self._lock:
            entry = self._entry_or_raise(track_id)
            occupied = {
                c.hotcue for c in (entry.cue_v2 or []) if c.hotcue is not None and c.hotcue >= 0
            }
        for spec in specs:
            if not overwrite and spec.slot in occupied:
                continue
            self.set_hotcue(
                track_id,
                spec.slot,
                spec.start,
                getattr(spec, "type", 0),
                getattr(spec, "length", 0.0),
                name=getattr(spec, "name", None),
            )

    def set_hotcue_type(self, track_id: str, slot: int, cue_type: int) -> None:
        """Change the type of the existing hotcue in `slot` (keeps its position)."""
        if cue_type not in self.POINT_CUE_TYPES:
            raise PlaylistError(f"Unsupported cue type: {cue_type}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            cue = next((c for c in (entry.cue_v2 or []) if c.hotcue == slot), None)
            if cue is None:
                raise PlaylistError(f"Hotcue {slot} is not set")
            cue.type = cue_type
            self._note("hotcue", "modify", track_id, f"slot:{slot}")
            self.dirty = True

    def delete_hotcue(self, track_id: str, slot: int) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            before = entry.cue_v2 or []
            entry.cue_v2 = [c for c in before if c.hotcue != slot]
            if len(entry.cue_v2) != len(before):  # only count an actual removal
                self._note("hotcue", "delete", track_id, f"slot:{slot}")
            self.dirty = True

    # ---- beatgrid -----------------------------------------------------
    # A beatgrid is an ORDERED LIST of markers (see beatgrid.py); a constant
    # grid is a list of length one. Markers are addressed by their index in that
    # start-ordered list. CUE_V2 has no id attribute and save() reparses the
    # whole file, so any synthetic id would have to be rebuilt by position
    # anyway — the index IS the identity.
    _MARKER_MIN_GAP_MS = 1.0

    @staticmethod
    def _grid_marker(entry: Entrytype) -> CueV2Type | None:
        """The first grid marker BY START — the one <TEMPO BPM> mirrors."""
        return beatgrid.first_marker(entry)

    @staticmethod
    def _sync_tempo(entry: Entrytype) -> None:
        """Mirror <TEMPO BPM> from the first marker (7968/7968 in the reference
        collection). Called last by every grid mutator, so a move that changes
        which marker is first re-mirrors for free.

        With no markers left, TEMPO is LEFT ALONE: Traktor keeps the analysed
        BPM on gridless tracks (53 such entries exist in the reference file).
        """
        marker = beatgrid.first_marker(entry)
        if marker is None or marker.grid is None or not marker.grid.bpm:
            return
        if entry.tempo is None:
            entry.tempo = Tempotype(bpm=marker.grid.bpm, bpm_quality=100.0)
        else:
            entry.tempo.bpm = marker.grid.bpm

    @staticmethod
    def _sort_cues(entry: Entrytype) -> None:
        """Keep CUE_V2 ascending by START — true for 8485/8485 reference
        entries, so an inserted marker must not become the one exception.
        Stable, so a marker stays ahead of a companion at an identical START.
        """
        if entry.cue_v2:
            entry.cue_v2.sort(key=lambda c: c.start or 0.0)

    def _markers_or_raise(
        self, entry: Entrytype, index: int
    ) -> tuple[list[CueV2Type], CueV2Type]:
        markers = beatgrid.grid_markers(entry)
        if not markers:
            raise PlaylistError("Track has no beatgrid")
        if not 0 <= index < len(markers):
            raise PlaylistError(
                f"Grid marker {index} out of range (track has {len(markers)})"
            )
        return markers, markers[index]

    def _drop_grid(self, entry: Entrytype) -> None:
        """Remove every marker AND every companion. No _note — callers do that."""
        doomed = {id(m) for m in beatgrid.grid_markers(entry)}
        doomed |= {id(c) for c in beatgrid.companions(entry).values()}
        entry.cue_v2 = [c for c in (entry.cue_v2 or []) if id(c) not in doomed]

    def add_grid_marker(
        self,
        track_id: str,
        start_sec: float,
        bpm: float | None = None,
        name: str | None = None,
    ) -> int:
        """Add a beatgrid marker at `start_sec`; returns its index.

        With `bpm` unset the marker INHERITS the tempo of the section it lands
        in, so splitting a section is musically a no-op until it's retempoed.
        Creates no companion cue — Traktor tolerates markers without one, and
        inventing them would silently consume the user's hotcue slots.
        """
        if bpm is not None and bpm <= 0:
            raise PlaylistError(f"BPM must be positive: {bpm}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            start_ms = max(0.0, start_sec * 1000.0)
            markers = beatgrid.grid_markers(entry)
            for m in markers:
                if abs((m.start or 0.0) - start_ms) < self._MARKER_MIN_GAP_MS:
                    raise PlaylistError("A grid marker already exists at that position")
            if bpm is None:
                bpm = self._bpm_governing(entry, start_ms)
                if bpm is None:
                    raise PlaylistError("No BPM available to create a beatgrid")
            if entry.cue_v2 is None:
                entry.cue_v2 = []
            entry.cue_v2.append(
                CueV2Type(
                    name=name or ("AutoGrid" if not markers else "n.n."),
                    displ_order=0,
                    type=4,
                    start=start_ms,
                    len=0.0,
                    repeats=-1,
                    hotcue=-1,
                    color=None,
                    grid=GridType(bpm=bpm),
                )
            )
            self._sort_cues(entry)
            self._sync_tempo(entry)
            self._note("grid", "marker-add", track_id)
            self.dirty = True
            return next(
                i
                for i, m in enumerate(beatgrid.grid_markers(entry))
                if abs((m.start or 0.0) - start_ms) < 1e-9
            )

    @staticmethod
    def _bpm_governing(entry: Entrytype, start_ms: float) -> float | None:
        """The tempo in force at `start_ms`: the last marker at or before it,
        else the first marker (extrapolated backwards), else <TEMPO BPM>."""
        markers = beatgrid.grid_markers(entry)
        if not markers:
            return entry.tempo.bpm if entry.tempo else None
        governing = markers[0]
        for m in markers:
            if (m.start or 0.0) <= start_ms:
                governing = m
            else:
                break
        return governing.grid.bpm if governing.grid else None

    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> None:
        """Move marker `index`, DRAGGING ITS COMPANION with it.

        The new position is CLAMPED between the neighbouring markers rather than
        allowed to reorder them, so an index can never go stale mid-edit. Phase
        nudges are +/-1 and +/-10 ms, so this is no practical constraint;
        restructuring a grid is add-then-delete.
        """
        with self._lock:
            entry = self._entry_or_raise(track_id)
            markers, marker = self._markers_or_raise(entry, index)
            start_ms = max(0.0, start_sec * 1000.0)
            if index > 0:
                lo = (markers[index - 1].start or 0.0) + self._MARKER_MIN_GAP_MS
                start_ms = max(start_ms, lo)
            if index < len(markers) - 1:
                hi = (markers[index + 1].start or 0.0) - self._MARKER_MIN_GAP_MS
                start_ms = min(start_ms, hi)
            companion = beatgrid.companions(entry).get(index)
            marker.start = start_ms
            if companion is not None:
                # Copy the float — never recompute from seconds. A 1-ULP drift
                # here would change the rendered bytes and break byte-reverts.
                companion.start = marker.start
            self._sync_tempo(entry)
            self._note("grid", "marker-move", track_id, f"marker:{index}")
            self.dirty = True

    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> None:
        """Set marker `index`'s tempo (governs until the next marker)."""
        if bpm <= 0:
            raise PlaylistError(f"BPM must be positive: {bpm}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            _, marker = self._markers_or_raise(entry, index)
            if marker.grid is None:
                marker.grid = GridType(bpm=bpm)
            else:
                marker.grid.bpm = bpm
            self._sync_tempo(entry)
            self._note("grid", "marker-bpm", track_id, f"marker:{index}")
            self.dirty = True

    def delete_grid_marker(self, track_id: str, index: int) -> None:
        """Remove marker `index` and its companion (freeing that hotcue slot)."""
        with self._lock:
            entry = self._entry_or_raise(track_id)
            markers, marker = self._markers_or_raise(entry, index)
            companion = beatgrid.companions(entry).get(index)
            doomed = {id(marker)} | ({id(companion)} if companion is not None else set())
            entry.cue_v2 = [c for c in (entry.cue_v2 or []) if id(c) not in doomed]
            self._sync_tempo(entry)
            # Removing the last marker IS deleting the grid — report it as one.
            self._note(
                "grid",
                "delete" if len(markers) == 1 else "marker-delete",
                track_id,
                f"marker:{index}",
            )
            self.dirty = True

    def replace_grid(
        self, track_id: str, markers: list[tuple[float, float]]
    ) -> None:
        """Throw the grid away and rebuild it from `markers` [(start_sec, bpm)].

        Serves both Auto Grid (a single marker) and the deck's Reset (restore
        the list as loaded) as one atomic, single-event command. Companions of
        the old grid go with it; none are created.
        """
        for start_sec, bpm in markers:
            if bpm <= 0:
                raise PlaylistError(f"BPM must be positive: {bpm}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            self._drop_grid(entry)
            if entry.cue_v2 is None:
                entry.cue_v2 = []
            for i, (start_sec, bpm) in enumerate(
                sorted(markers, key=lambda m: m[0])
            ):
                entry.cue_v2.append(
                    CueV2Type(
                        name="AutoGrid" if i == 0 else "n.n.",
                        displ_order=0,
                        type=4,
                        start=max(0.0, start_sec * 1000.0),
                        len=0.0,
                        repeats=-1,
                        hotcue=-1,
                        color=None,
                        grid=GridType(bpm=bpm),
                    )
                )
            self._sort_cues(entry)
            self._sync_tempo(entry)
            self._note("grid", "replace", track_id)
            self.dirty = True

    def place_grid_companion(
        self, track_id: str, index: int, *, prefer_slot: int = 0
    ) -> int | None:
        """Give marker `index` the white beat-1 hotcue Traktor writes beside its
        own markers: `prefer_slot` when free, else the lowest free slot.

        Returns the slot used, or None when the bank is full or the marker
        already has a companion. NEVER overwrites. This is the ONLY place
        Konduktor creates a companion — grid edits never do — because it is a
        deliberate, user-invoked feature (Auto Grid), not a side effect.
        """
        with self._lock:
            entry = self._entry_or_raise(track_id)
            _, marker = self._markers_or_raise(entry, index)
            if index in beatgrid.companions(entry):
                return None
            used = {
                c.hotcue
                for c in (entry.cue_v2 or [])
                if c.hotcue is not None and c.hotcue >= 0
            }
            slot = next(
                (s for s in [prefer_slot, *range(8)] if 0 <= s <= 7 and s not in used),
                None,
            )
            if slot is None:
                return None
            entry.cue_v2.append(
                CueV2Type(
                    name=marker.name or "AutoGrid",
                    displ_order=0,
                    type=0,
                    start=marker.start,  # copy the float, not start_sec * 1000
                    len=0.0,
                    repeats=-1,
                    hotcue=slot,
                    color=beatgrid.COMPANION_COLOR,
                    grid=None,
                )
            )
            self._sort_cues(entry)
            self._note("grid", "marker-add", track_id)
            self.dirty = True
            return slot

    def delete_grid(self, track_id: str) -> None:
        """Remove EVERY grid marker AND EVERY companion; <TEMPO> is kept.

        Dropping companions is the debris fix: the old code stripped markers
        only, leaving orphan white cues squatting in hotcue slots.
        """
        with self._lock:
            entry = self._entry_or_raise(track_id)
            self._drop_grid(entry)
            self._note("grid", "delete", track_id)
            self.dirty = True

    def set_lock(self, track_id: str, locked: bool) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.lock = 1 if locked else None
            self._note("lock", "on" if locked else "off", track_id)
            self.dirty = True

    # ---- cover art -----------------------------------------------------
    def set_track_art(self, track_id: str, data: bytes, mime: str) -> None:
        with self._lock:
            if track_id not in self._entry_by_key:
                raise PlaylistError(f"Track not found: {track_id}")
            self._track_art[track_id] = (data, mime)
            self.dirty = True

    def set_path_mapping(self, mapping: PathMapping) -> None:
        """Set the active OS-path prefix remapping (applied at resolve time)."""
        with self._lock:
            self._path_mapping = mapping

    def _resolve(self, loc) -> "Path | None":
        """Resolve a LOCATION to an OS path, applying the active path mapping.

        The single FS chokepoint: LOCATION -> `resolve_path` -> prefix remap.
        Falls back to the un-remapped path when the remapped target doesn't
        exist, so a misconfigured mapping never makes a present file unreachable.
        """

        if loc is None:
            return None
        base = resolve_path(loc.volume, loc.dir, loc.file)
        if self._path_mapping.empty:
            return base
        remapped = self._path_mapping.apply(base)
        if remapped == base:
            return base
        return remapped if remapped.exists() else base

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        """Staged replacement if present, else the file's current embedded art."""
        from ...core import audio_tags as file_tags

        with self._lock:
            if track_id in self._track_art:
                return self._track_art[track_id]
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                return None
            path = self._resolve(entry.location)
            return file_tags.read_cover(path) if path else None

    def audio_path(self, track_id: str) -> "Path | None":
        """Resolve a track's audio file to an OS path (for playback streaming)."""
        with self._lock:
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                return None
            return self._resolve(entry.location)

    def path_prefix_suggestions(self) -> dict:
        """Auto-suggest ``from`` prefixes by resolving every track's LOCATION and
        finding the common directory prefix per volume. Returns a ``primary``
        (the largest group's prefix) plus per-group prefixes ranked by track
        count, so the editor can prefill and offer alternatives."""
        from collections import defaultdict


        with self._lock:
            groups: dict[str, list[str]] = defaultdict(list)
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                groups[loc.volume or ""].append(
                    str(resolve_path(loc.volume, loc.dir, loc.file))
                )
            ranked = sorted(
                (
                    {"prefix": common_dir_prefix(paths), "count": len(paths)}
                    for paths in groups.values()
                ),
                key=lambda g: g["count"],
                reverse=True,
            )
            ranked = [g for g in ranked if g["prefix"]]
            return {"primary": ranked[0]["prefix"] if ranked else "", "groups": ranked[:5]}

    def remap_preview(self, mapping: PathMapping) -> dict:
        """How a mapping would affect the collection, without changing anything:
        total tracks, how many match ``from``, and how many exist at ``to``
        (plus a few samples). Powers the mapping editor's validation line."""

        with self._lock:
            total = matched = existing = 0
            samples: list[dict] = []
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                total += 1
                base = resolve_path(loc.volume, loc.dir, loc.file)
                if mapping.matches(base):
                    matched += 1
                    target = mapping.apply(base)
                    ok = target.exists()
                    if ok:
                        existing += 1
                    if len(samples) < 5:
                        samples.append({"from": str(base), "to": str(target), "exists": ok})
            return {"total": total, "matched": matched, "existing": existing, "samples": samples}

    def remap_locations(self, mapping: PathMapping) -> int:
        """Permanently rewrite matching track LOCATIONs to the mapping's ``to``
        prefix — a deliberate library move (write-back).

        Because ``track_id`` IS the LOCATION key (``volume+dir+file``), every
        playlist ``PRIMARYKEY`` that referenced a moved track is rewritten too,
        so playlists keep pointing at their tracks. Non-matching entries are
        untouched. Returns the number of tracks rewritten; the caller saves.
        """

        if mapping.empty:
            return 0
        with self._lock:
            key_remap: dict[str, str] = {}
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                base = resolve_path(loc.volume, loc.dir, loc.file)
                if not mapping.matches(base):
                    continue
                target = mapping.apply(base)
                volume, dir_, file = os_path_to_location(target)
                old_key = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"
                new_key = f"{volume or ''}{dir_ or ''}{file or ''}"
                if old_key == new_key:
                    continue  # no-op (e.g. from == to); leave byte-identical
                loc.volume, loc.dir, loc.file = volume, dir_, file
                key_remap[old_key] = new_key
            if not key_remap:
                return 0
            # Rewrite playlist entry primary keys that referenced moved tracks.
            for node in self._iter_nodes(self._root()):
                pl = node.playlist
                if pl is None or not pl.entry:
                    continue
                for entry in pl.entry:
                    pk = entry.primarykey
                    if pk and pk.key in key_remap:
                        pk.key = key_remap[pk.key]
            # Track ids derive from the LOCATION, so a remap renames them. Carry
            # this session's journal entries across or their file-tag sync is lost.
            for old_key, new_key in key_remap.items():
                self._journal.retarget(old_key, new_key)
            self._note("remap", str(len(key_remap)))
            self.dirty = True
            return len(key_remap)

    def _entry_field_value(self, entry: Entrytype, field: str):
        """Read a single editable field's current value from the model, in the
        shape file_tags expects (rating as 0–5 stars)."""
        if field == "title":
            return entry.title
        if field == "artist":
            return entry.artist
        if field == "album":
            return entry.album.title if entry.album else None
        if field == "rating":
            r = entry.info.ranking if entry.info else None
            return round(r / 51) if r else 0
        return getattr(entry.info, field, None) if entry.info else None

    def count_playlists(self) -> int:
        with self._lock:
            return sum(1 for n in self._iter_nodes(self._root()) if n.playlist is not None)

    # ---- persistence ---------------------------------------------------
    def save(self) -> "SaveOutcome":
        with self._lock:
            new_data = self._render()
            tmp = self.nml_path.with_suffix(self.nml_path.suffix + ".tmp")
            tmp.write_bytes(new_data)
            tmp.replace(self.nml_path)
            # Best-effort: sync the edited fields into each edited track's file.
            tag_results = self._sync_file_tags()
            # Computed before _load() clears the journal. Versioning the bytes is
            # the app's job, not the adapter's — see app_state.AppState.save.
            summary = self._edit_summary()
            self._load()  # clears dirty + the edit journal + staged art
            return SaveOutcome(summary=summary, snapshot=new_data, tag_results=tag_results)

    def _edit_summary(self) -> str:
        """Delegates to the journal; kept as a method because save() and the
        tests reach for it by this name."""
        return self._journal.summary(extra_tracks=set(self._track_art))

    def _sync_file_tags(self) -> list["FileTagResult"]:
        from ...core import audio_tags as file_tags

        results: list[FileTagResult] = []
        # Union of tracks with edited fields and/or replaced art.
        for track_id in self._journal.edited_tracks() | self._track_art.keys():
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                continue
            path = self._resolve(entry.location)
            if path is None:
                continue
            # Report BOTH writes: a failed tag write must not be masked by a
            # successful art write on the same track.
            written = []
            if fields := self._journal.fields_for(track_id):
                meta = {f: self._entry_field_value(entry, f) for f in fields}
                written.append(file_tags.write_tags(path, meta, popm_email=TRAKTOR_POPM_EMAIL))
            if track_id in self._track_art:
                data, mime = self._track_art[track_id]
                written.append(file_tags.write_cover(path, data, mime))
            for r in written:
                results.append(
                    FileTagResult(
                        track_id=track_id, file=str(path), ok=r.ok, status=r.status, detail=r.detail
                    )
                )
        return results

    def _render(self) -> bytes:
        """Render the whole model exactly as the library's save() does.

        The two post-processing steps restore Traktor's precise layout (expanded
        empty tags, 6-decimal floats, newlines), so a no-op render reproduces the
        file byte-for-byte and only the objects we actually edited (playlists
        AND/OR track entries) diff. Verified by backend/test_save_fidelity.py.
        """
        s = XmlSerializer().render(self._nml)
        s = restore_traktor_float_format(s, self._nml)
        s = format_traktor_layout(s)
        return s.encode("utf-8")
