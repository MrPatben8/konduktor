"""Editable playlist store backed by the traktor-nml-utils dataclass model.

Design goals:
  * minimal diff — unedited playlists must serialize byte-for-byte as Traktor
    wrote them (the xsdata serializer used by the library reproduces Traktor's
    exact formatting; lxml does not),
  * surgical — the ~14 MB COLLECTION stays byte-identical (we splice only the
    <PLAYLISTS> span back into the original file bytes),
  * safe — every save is committed to the collection's version history (see
    ``history.py``); the commit is additive and never alters the saved bytes.

We edit the parsed dataclass model in memory (create/rename/delete/set-entries),
then on save render the full model, extract just the freshly-rendered
<PLAYLISTS>…</PLAYLISTS> span, and splice it into the untouched original bytes.

Playlists are identified by their stable UUID; folders by a synthetic path id
(``fld:<name>/<name>``). Smart playlists are read-only.
"""
from __future__ import annotations

import threading
import uuid as uuidlib
from collections import Counter
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

from . import __version__, history
from .path_mapping import PathMapping
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
    commit: str | None  # sha of the version-history commit (None if deduped/failed)
    tag_results: list[FileTagResult]


class PlaylistStore:
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
        # track_id -> set of field names edited this session (drives file-tag sync)
        self._track_edits: dict[str, set[str]] = {}
        # track_id -> (image_bytes, mime) of staged replacement cover art
        self._track_art: dict[str, tuple[bytes, str]] = {}
        # (category, detail) events accumulated this session -> commit summary
        self._edit_events: list[tuple[str, str | None]] = []
        self.dirty = False

    def _note(self, category: str, detail: str | None = None) -> None:
        """Record a human-meaningful edit for the next commit's summary."""
        self._edit_events.append((category, detail))

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
            self._note("playlist-create", name)
            self.dirty = True
            return new_uuid

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
            self._note("hotcue-set")
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
            self._note("hotcue-type")
            self.dirty = True

    def delete_hotcue(self, track_id: str, slot: int) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.cue_v2 = [c for c in (entry.cue_v2 or []) if c.hotcue != slot]
            self._note("hotcue-delete")
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
            self._note("grid-edit")
            self.dirty = True

    def delete_grid(self, track_id: str) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.cue_v2 = [
                c for c in (entry.cue_v2 or []) if getattr(c, "grid", None) is None
            ]
            self._note("grid-delete")
            self.dirty = True

    def set_lock(self, track_id: str, locked: bool) -> None:
        with self._lock:
            entry = self._entry_or_raise(track_id)
            entry.lock = 1 if locked else None
            self._note("lock-toggle")
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
        from . import file_tags

        if loc is None:
            return None
        base = file_tags.resolve_path(loc.volume, loc.dir, loc.file)
        if self._path_mapping.empty:
            return base
        remapped = self._path_mapping.apply(base)
        if remapped == base:
            return base
        return remapped if remapped.exists() else base

    def cover_art(self, track_id: str) -> tuple[bytes, str] | None:
        """Staged replacement if present, else the file's current embedded art."""
        from . import file_tags

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

        from . import file_tags
        from .path_mapping import common_dir_prefix

        with self._lock:
            groups: dict[str, list[str]] = defaultdict(list)
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                groups[loc.volume or ""].append(
                    str(file_tags.resolve_path(loc.volume, loc.dir, loc.file))
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
        from . import file_tags

        with self._lock:
            total = matched = existing = 0
            samples: list[dict] = []
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                total += 1
                base = file_tags.resolve_path(loc.volume, loc.dir, loc.file)
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
        from . import file_tags
        from .path_mapping import os_path_to_location

        if mapping.empty:
            return 0
        with self._lock:
            key_remap: dict[str, str] = {}
            for e in self._nml.collection.entry:
                loc = e.location
                if not loc:
                    continue
                base = file_tags.resolve_path(loc.volume, loc.dir, loc.file)
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
            # Version history: commit the saved bytes (additive — never touches the
            # file we just wrote). Summary is computed before _load() clears state.
            commit = history.commit(self.nml_path, new_data, self._edit_summary(), __version__)
            self._load()  # clears dirty + _track_edits + _edit_events
            return SaveOutcome(commit=commit, tag_results=tag_results)

    def _edit_summary(self) -> str:
        """A one-line, human-readable summary of this session's edits, used as the
        version-history commit message (e.g. "Edited 3 tracks; renamed playlist
        'House'; set 2 hotcues")."""
        parts: list[str] = []
        counts = Counter(cat for cat, _ in self._edit_events)
        details: dict[str, list[str]] = {}
        for cat, detail in self._edit_events:
            if detail:
                details.setdefault(cat, []).append(detail)

        n_tracks = len(self._track_edits.keys() | self._track_art.keys())
        if n_tracks:
            parts.append(f"edited {n_tracks} track{'' if n_tracks == 1 else 's'}")

        for cat, verb in (
            ("playlist-create", "created"),
            ("playlist-rename", "renamed"),
            ("playlist-delete", "deleted"),
            ("playlist-entries", "reordered"),
        ):
            names = details.get(cat, [])
            if not names:
                continue
            if len(names) <= 2:
                joined = ", ".join(f"'{n}'" for n in names)
                parts.append(f"{verb} playlist{'' if len(names) == 1 else 's'} {joined}")
            else:
                parts.append(f"{verb} {len(names)} playlists")

        for cat, verb, sing, plur in (
            ("hotcue-set", "set", "hotcue", "hotcues"),
            ("hotcue-type", "changed", "hotcue type", "hotcue types"),
            ("hotcue-delete", "deleted", "hotcue", "hotcues"),
        ):
            n = counts.get(cat, 0)
            if n:
                parts.append(f"{verb} {n} {sing if n == 1 else plur}")
        if counts.get("grid-edit"):
            parts.append("edited beatgrid")
        if counts.get("grid-delete"):
            parts.append("deleted beatgrid")
        if counts.get("lock-toggle"):
            parts.append("toggled lock")
        for detail in details.get("remap", []):
            parts.append(f"remapped {detail} file paths")

        if not parts:
            return "Saved changes"
        summary = "; ".join(parts)
        return summary[0].upper() + summary[1:]

    def _sync_file_tags(self) -> list["FileTagResult"]:
        from . import file_tags

        results: list[FileTagResult] = []
        # Union of tracks with edited fields and/or replaced art.
        for track_id in self._track_edits.keys() | self._track_art.keys():
            entry = self._entry_by_key.get(track_id)
            if entry is None or entry.location is None:
                continue
            path = self._resolve(entry.location)
            if path is None:
                continue
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
