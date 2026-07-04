"""Editable playlist store backed by the traktor-nml-utils dataclass model.

Design goals:
  * minimal diff — unedited playlists must serialize byte-for-byte as Traktor
    wrote them (the xsdata serializer used by the library reproduces Traktor's
    exact formatting; lxml does not),
  * surgical — the ~14 MB COLLECTION stays byte-identical (we splice only the
    <PLAYLISTS> span back into the original file bytes),
  * safe — a timestamped .bak is written before every save.

We edit the parsed dataclass model in memory (create/rename/delete/set-entries),
then on save render the full model, extract just the freshly-rendered
<PLAYLISTS>…</PLAYLISTS> span, and splice it into the untouched original bytes.

Playlists are identified by their stable UUID; folders by a synthetic path id
(``fld:<name>/<name>``). Smart playlists are read-only.
"""
from __future__ import annotations

import shutil
import threading
import time
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
    Nodetype,
    Playlisttype,
    Primarykeytype,
    Subnodestype,
    Tempotype,
)
from xsdata.formats.dataclass.serializers import XmlSerializer

from .schemas import PlaylistNode


class PlaylistError(Exception):
    pass


@dataclass
class FileTagResult:
    track_id: str
    file: str
    ok: bool
    status: str  # "written" | "file-not-found" | "unsupported-format" | "error"
    detail: str = ""


@dataclass
class SaveOutcome:
    backup: Path
    tag_results: list[FileTagResult]


class PlaylistStore:
    def __init__(self, nml_path: Path):
        self.nml_path = Path(nml_path)
        self._lock = threading.RLock()
        self.dirty = False
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
        # track_id -> set of field names edited this session (drives file-tag sync)
        self._track_edits: dict[str, set[str]] = {}
        # track_id -> (image_bytes, mime) of staged replacement cover art
        self._track_art: dict[str, tuple[bytes, str]] = {}
        self.dirty = False

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
                type="PLAYLIST",
                uuid=pl.uuid if pl else None,
                count=len(keys),
            )
        if ntype == "SMARTLIST" or node.smartplaylist is not None:
            return PlaylistNode(
                id="sl:" + "/".join(parent_path + [name]), name=name, type="SMARTLIST"
            )
        path = parent_path + [name]
        return PlaylistNode(
            id="fld:" + "/".join(path),
            name=name,
            type="FOLDER",
            children=[self._to_model(c, path) for c in self._children(node)],
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
            self.dirty = True
            return new_uuid

    def rename_playlist(self, playlist_uuid: str, name: str) -> None:
        with self._lock:
            node = self._find_playlist_node(playlist_uuid)
            if node is None:
                raise PlaylistError(f"Playlist not found: {playlist_uuid}")
            node.name = name
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
                self._track_edits.setdefault(track_id, set()).add(k)
            self.dirty = True

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
        self, track_id: str, slot: int, start_sec: float, cue_type: int, length_sec: float = 0.0
    ) -> None:
        """Create (or reposition + retype) the hotcue in `slot` at `start_sec`.

        `length_sec` > 0 makes it a loop (used with cue_type 5)."""
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
                        name="n.n.",  # Traktor's default name for a manual cue
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
            self.dirty = True

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
            self.dirty = True

    def delete_hotcue(self, track_id: str, slot: int) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.cue_v2 = [c for c in (entry.cue_v2 or []) if c.hotcue != slot]
            self.dirty = True

    # ---- beatgrid -----------------------------------------------------
    @staticmethod
    def _grid_marker(entry: Entrytype) -> CueV2Type | None:
        return next(
            (c for c in (entry.cue_v2 or []) if getattr(c, "grid", None) is not None), None
        )

    def set_grid(
        self, track_id: str, bpm: float | None = None, anchor_sec: float | None = None
    ) -> None:
        """Set the beatgrid tempo (TEMPO + grid marker BPM) and/or move the grid
        marker (beat 1). Creates a grid marker if the track has none."""
        with self._lock:
            entry = self._entry_or_raise(track_id)
            marker = self._grid_marker(entry)
            if bpm is not None:
                if bpm <= 0:
                    raise PlaylistError(f"BPM must be positive: {bpm}")
                if entry.tempo is None:
                    entry.tempo = Tempotype(bpm=bpm, bpm_quality=100.0)
                else:
                    entry.tempo.bpm = bpm
                if marker is not None and marker.grid is not None:
                    marker.grid.bpm = bpm
            if anchor_sec is not None:
                start_ms = max(0.0, anchor_sec * 1000.0)
                if marker is not None:
                    marker.start = start_ms
                else:
                    grid_bpm = bpm if bpm is not None else (entry.tempo.bpm if entry.tempo else None)
                    if grid_bpm is None:
                        raise PlaylistError("No BPM available to create a beatgrid")
                    if entry.cue_v2 is None:
                        entry.cue_v2 = []
                    entry.cue_v2.append(
                        CueV2Type(
                            name="AutoGrid",
                            displ_order=0,
                            type=4,
                            start=start_ms,
                            len=0.0,
                            repeats=-1,
                            hotcue=-1,
                            color=None,
                            grid=GridType(bpm=grid_bpm),
                        )
                    )
            self.dirty = True

    def delete_grid(self, track_id: str) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.cue_v2 = [
                c for c in (entry.cue_v2 or []) if getattr(c, "grid", None) is None
            ]
            self.dirty = True

    def set_lock(self, track_id: str, locked: bool) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.lock = 1 if locked else None
            self.dirty = True

    # ---- cover art -----------------------------------------------------
    def set_track_art(self, track_id: str, data: bytes, mime: str) -> None:
        with self._lock:
            if track_id not in self._entry_by_key:
                raise PlaylistError(f"Track not found: {track_id}")
            self._track_art[track_id] = (data, mime)
            self.dirty = True

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        """Staged replacement if present, else the file's current embedded art."""
        from . import file_tags

        with self._lock:
            if track_id in self._track_art:
                return self._track_art[track_id]
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                return None
            loc = entry.location
            return file_tags.read_cover(file_tags.resolve_path(loc.volume, loc.dir, loc.file))

    def audio_path(self, track_id: str) -> "Path | None":
        """Resolve a track's audio file to an OS path (for playback streaming)."""
        from . import file_tags

        with self._lock:
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                return None
            loc = entry.location
            return file_tags.resolve_path(loc.volume, loc.dir, loc.file)

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
            backup = self._backup()
            new_data = self._render()
            tmp = self.nml_path.with_suffix(self.nml_path.suffix + ".tmp")
            tmp.write_bytes(new_data)
            tmp.replace(self.nml_path)
            # Best-effort: sync the edited fields into each edited track's file.
            tag_results = self._sync_file_tags()
            self._load()  # clears dirty + _track_edits
            return SaveOutcome(backup=backup, tag_results=tag_results)

    def _sync_file_tags(self) -> list["FileTagResult"]:
        from . import file_tags

        results: list[FileTagResult] = []
        # Union of tracks with edited fields and/or replaced art.
        for track_id in self._track_edits.keys() | self._track_art.keys():
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                continue
            loc = entry.location
            path = file_tags.resolve_path(loc.volume, loc.dir, loc.file)
            r = None
            if track_id in self._track_edits:
                meta = {f: self._entry_field_value(entry, f) for f in self._track_edits[track_id]}
                r = file_tags.write_tags(path, meta)
            if track_id in self._track_art:
                data, mime = self._track_art[track_id]
                r = file_tags.write_cover(path, data, mime)
            if r is not None:
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

    def _backup(self) -> Path:
        # Backups live in a `backups/` folder next to the collection, not beside
        # the file itself (keeps the collection's directory clean).
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        backup_dir = self.nml_path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{self.nml_path.name}.{stamp}.bak"
        n = 1
        while backup.exists():
            backup = backup_dir / f"{self.nml_path.name}.{stamp}-{n}.bak"
            n += 1
        shutil.copy2(self.nml_path, backup)
        return backup
