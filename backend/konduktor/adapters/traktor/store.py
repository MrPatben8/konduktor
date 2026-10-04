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

import copy
import math
import threading
import uuid as uuidlib
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
    MusicalKeytype,
    Locationtype,
    Nodetype,
    Playlisttype,
    Primarykeytype,
    Stemstype,
    Subnodestype,
    Tempotype,
)
from xsdata.formats.dataclass.serializers import XmlSerializer

from ...core.adapter import FileTagResult, InvalidCommand, SaveOutcome, StemSwapResult
from ...core.edit_journal import EditJournal
from ...core.pathmap import common_dir_prefix
from ...core.pathmap import PathMapping, stored_form
from ...core.relocate import PathGroup
from ...core.stem_file import NML_STEMS_JSON, nml_stems_for
from ...schemas import PlaylistNode
from . import beatgrid, timebase
from .locations import os_path_to_location, resolve_path
from ...core import musical_key
from .projection import iso_date, musical_key_value, traktor_date

# Traktor's POPM frame owner: an ID3 rating is per-owner, so writing under
# this email is what makes the stars show up in Traktor itself.
TRAKTOR_POPM_EMAIL = "traktor@native-instruments.de"


class PlaylistError(InvalidCommand):
    """A command this library cannot accept.

    Subclasses the generic `InvalidCommand` so the HTTP layer maps it without
    knowing Traktor exists; the name is kept because it is what every call site
    and the fidelity tests already catch.
    """


class TraktorStore:
    def __init__(self, nml_path: Path):
        self.nml_path = Path(nml_path)
        self._lock = threading.RLock()
        self.dirty = False
        # Active OS-path prefix remapping (empty = identity). Survives _load().
        self._path_mapping = PathMapping()
        # Write a file added through a mapping in the library's STORED form
        # (`pathmap.stored_form`) — a Konduktor server sets this.
        self._write_stored_paths = False
        # Mappings the user confirmed in the open-time missing-files check.
        # Session-only: never saved, re-derived on every open.
        self._session_mappings: list[PathMapping] = []
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
        self._notation: str | None = None
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
            can_rename=True,
            can_delete=True,
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
        self._check_folder_name(name)
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

    @staticmethod
    def _check_folder_name(name: str) -> None:
        # A folder is addressed by its path of names, so a "/" in one would
        # split it into two path segments and the folder could never be found.
        if not name:
            raise PlaylistError("A folder needs a name")
        if "/" in name:
            raise PlaylistError('A folder name cannot contain "/"')

    def _rename_folder(self, folder_id: str, name: str) -> None:
        """Rename a folder. Its id is its path, so the id changes with it —
        and a sibling folder of the same name would make both unaddressable,
        so that is refused rather than silently merged."""
        self._check_folder_name(name)
        node = self._find_folder(folder_id)
        if node is self._root():
            raise PlaylistError("Cannot rename the root folder")
        parent = self._find_parent_of(node)
        if any(
            c is not node and (c.type or "FOLDER") == "FOLDER" and c.name == name
            for c in self._children(parent)
        ):
            raise PlaylistError(f'There is already a folder called "{name}" here')
        node.name = name
        self._note("playlist-rename", name)
        self.dirty = True

    def rename_playlist(self, playlist_uuid: str, name: str) -> None:
        with self._lock:
            if playlist_uuid.startswith("fld:"):
                self._rename_folder(playlist_uuid, name)
                return
            node = self._find_playlist_node(playlist_uuid)
            if node is None:
                raise PlaylistError(f"Playlist not found: {playlist_uuid}")
            node.name = name
            self._note("playlist-rename", name)
            self.dirty = True

    def delete_playlist(self, playlist_uuid: str) -> None:
        """Delete a playlist, or a FOLDER and everything nested in it.

        A folder is addressed by its synthetic `fld:` path id. Removing its node
        removes its subtree with it — the NML nests children inside the parent's
        SUBNODES, so there is nothing elsewhere to clean up; the tracks stay in
        the collection.
        """
        with self._lock:
            if playlist_uuid.startswith("fld:"):
                if playlist_uuid == "fld:":
                    raise PlaylistError("Cannot delete the root folder")
                node = self._find_folder(playlist_uuid)
            else:
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
    EDITABLE_FIELDS = {"title", "artist", "album", "rating", "comment2"} | _INFO_FIELDS
    # Fields that live only in the collection: Traktor itself writes no file tag
    # for "Comment 2" (verified on an m4a it had just edited), so neither do we.
    _COLLECTION_ONLY_FIELDS = {"comment2"}

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
                elif k == "release_date":
                    # Edited as ISO (that is what the projection shows); stored
                    # as Traktor's "YYYY/M/D".
                    entry.info.release_date = traktor_date(v)
                elif k in self._INFO_FIELDS:
                    setattr(entry.info, k, v or None)
                elif k == "comment2":
                    # Traktor's "Comment 2" is INFO@RATING — a free-text
                    # attribute, unrelated to the star rating (RANKING).
                    entry.info.rating = v or None
                elif k == "rating":
                    stars = max(0, min(5, int(v))) if v is not None else 0
                    # Traktor RANKING = stars * 51; unrated has no RANKING attr.
                    entry.info.ranking = stars * 51 or None
                else:
                    continue  # unknown / read-only fields are ignored
                self._journal.record("track", "set", track_id, k)
            self.dirty = True

    # ---- musical key ----------------------------------------------------
    def _collection_notation(self) -> str | None:
        """The notation this collection's INFO@KEY values are written in — a
        Traktor preference (Open Key / Camelot / musical) the NML does not
        record, so it is read off the keys themselves; the majority wins. None
        when the collection holds no keys — never cached, since adding entries
        to an empty collection (an export) is exactly when it changes. Once
        known it is kept for the session: every key Konduktor writes is in it,
        so it can only move if one could out-vote thousands."""
        from collections import Counter

        if self._notation is not None:
            return self._notation
        seen = Counter(
            musical_key.notation_of(e.info.key)
            for e in self._nml.collection.entry
            if e.info is not None and e.info.key
        )
        seen.pop(None, None)
        self._notation = seen.most_common(1)[0][0] if seen else None
        return self._notation

    def key_notation(self) -> str:
        """`_collection_notation`, or Open Key (Traktor's default) when there
        are no keys to go by. A key in another notation would display fine but
        read as foreign beside its neighbours."""
        return self._collection_notation() or "open_key"

    def _added_key_text(self, track) -> str | None:
        """INFO@KEY for an entry `add_entry` creates: rendered from the wheel in
        this collection's notation, never copied — a Pioneer source says "Abm",
        which reads as foreign beside "9m". A collection with no keys yet (an
        export's fresh skeleton) takes a Traktor-native source's own notation,
        so Traktor -> Traktor keeps the user's (the export decision). Text that
        names no key is kept as it was: dropping it would lose information."""
        text = getattr(track, "key", None) or None
        wheel, mode = getattr(track, "key_wheel", None), getattr(track, "key_mode", None)
        if not (wheel and mode):
            return text
        notation = self._collection_notation()
        if notation is None:
            own = musical_key.notation_of(text)
            notation = own if own in ("open_key", "camelot") else "open_key"
        return musical_key.render(wheel, mode, notation)

    def set_key(self, track_id: str, wheel: int, mode: str) -> None:
        """Set a track's key as Traktor's analysis writes one: the display
        text in INFO@KEY (in the collection's notation; also what reaches the
        file's tag at Save) AND the analysed value in `<MUSICAL_KEY>`."""
        if not (isinstance(wheel, int) and 1 <= wheel <= 12) or mode not in ("major", "minor"):
            raise InvalidCommand(f"Not a key: wheel {wheel!r}, mode {mode!r}")
        with self._lock:
            entry = self._entry_by_key.get(track_id)
            if entry is None:
                raise PlaylistError(f"Track not found: {track_id}")
            if entry.info is None:
                entry.info = Infotype()
            before = entry.info.key
            entry.info.key = musical_key.render(wheel, mode, self.key_notation())
            entry.musical_key = MusicalKeytype(value_attribute=musical_key_value(wheel, mode))
            self._journal.record("track", "set", track_id, "key", before, entry.info.key)
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

        A stem FILE gets ``<STEMS>``, rendered from its own ``stem`` box as
        Traktor renders it: that element is what makes the entry a stem track
        (its playlist keys ``TYPE="STEM"``, the Type column), and without it a
        stem file added here read as plain audio.
        """
        # Read before the lock: it opens the file.
        stems = nml_stems_for(audio_path)
        with self._lock:
            volume, dir_, file = os_path_to_location(self._stored(Path(audio_path)))
            key = f"{volume}{dir_}{file}"
            if key in self._entry_by_key:
                raise PlaylistError(
                    f"The collection already has an entry for {audio_path}"
                )

            info = Infotype(
                genre=getattr(track, "genre", None) or None,
                label=getattr(track, "label", None) or None,
                comment=getattr(track, "comment", None) or None,
                rating=getattr(track, "comment2", None) or None,  # "Comment 2"
                remixer=getattr(track, "remixer", None) or None,
                producer=getattr(track, "producer", None) or None,
                mix=getattr(track, "mix", None) or None,
                key=self._added_key_text(track),
                bitrate=getattr(track, "bitrate", None) or None,
                playcount=getattr(track, "playcount", None) or None,
                # The generic model carries ISO dates; Traktor writes "YYYY/M/D".
                release_date=traktor_date(getattr(track, "release_date", None)),
                import_date=traktor_date(getattr(track, "import_date", None)),
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
                # The analysed key, as Traktor's own analysis stores it.
                musical_key=(
                    MusicalKeytype(value_attribute=musical_key_value(track.key_wheel, track.key_mode))
                    if getattr(track, "key_wheel", None) and getattr(track, "key_mode", None)
                    else None
                ),
                cue_v2=[],
                stems=Stemstype(stems=stems) if stems else None,
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

    def remove_entries(self, track_ids: list[str]) -> int:
        """Remove tracks from the collection AND from every playlist; return
        how many ENTRYs went.

        The mirror of `add_entry`, with the same care about bytes: only the
        removed ENTRYs, the `<COLLECTION ENTRIES>` count and the playlists that
        actually referenced a removed track change — any other playlist is left
        alone rather than rebuilt, so it still renders what it was parsed from.

        Playlists must be purged too: a PRIMARYKEY naming a track that is no
        longer in COLLECTION is a dangling reference, not an empty slot. The
        audio file is never touched — this is the library's record of the track,
        like Traktor's own "Delete from Collection".

        Matches by primary key in DOCUMENT order, so two ENTRYs sharing a key
        (which `iter_entries` warns can happen) both go, where the key→entry
        dict would have found only one.
        """
        gone = set(track_ids)
        with self._lock:
            before = self._nml.collection.entry
            kept = [e for e in before if self._key_of(e) not in gone]
            removed = len(before) - len(kept)
            if removed == 0:
                return 0
            self._nml.collection.entry = kept
            self._nml.collection.entries = len(kept)
            for key in gone:
                if self._entry_by_key.pop(key, None) is not None:
                    self._track_art.pop(key, None)
                    self._note("track", "remove", key)
            for n in self._iter_nodes(self._root()):
                pl = n.playlist
                if pl is None or not pl.entry:
                    continue
                entries = [
                    e for e in pl.entry
                    if not (e.primarykey is not None and e.primarykey.key in gone)
                ]
                if len(entries) != len(pl.entry):
                    pl.entry = entries
                    pl.entries = len(entries)
            self.dirty = True
            return removed

    @staticmethod
    def _key_of(entry) -> str | None:
        loc = entry.location
        return f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}" if loc else None

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
        label: on create it falls back to Traktor's "n.n."; on a replace, None
        keeps the old label (a hand move is still the same cue) while a name
        renames it (Auto Hotcues' Replace puts a DIFFERENT cue in the slot, and
        keeping "Drop 1" on a cue now at "Drop 1 -16" would mislabel it)."""
        if not 0 <= slot <= 7:
            raise PlaylistError(f"Invalid hotcue slot: {slot}")
        if cue_type not in self.CREATABLE_TYPES:
            raise PlaylistError(f"Unsupported cue type: {cue_type}")
        with self._lock:
            entry = self._entry_or_raise(track_id)
            # Traktor stores START/LEN in ms, on its own clock (see `timebase`).
            # A length is a duration, so the offset does not apply to it.
            start_ms = timebase.to_traktor_ms(start_sec, self.time_offset_ms(entry))
            len_ms = max(0.0, length_sec * 1000.0)
            existing = next(
                (c for c in (entry.cue_v2 or []) if c.hotcue == slot), None
            )
            if existing is not None:
                existing.start = start_ms
                existing.type = cue_type
                existing.len = len_ms
                if name is not None:
                    existing.name = name
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
            start_ms = timebase.to_traktor_ms(start_sec, self.time_offset_ms(entry))
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
            start_ms = timebase.to_traktor_ms(start_sec, self.time_offset_ms(entry))
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
            off_ms = self.time_offset_ms(entry)
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
                        start=timebase.to_traktor_ms(start_sec, off_ms),
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

    def set_write_stored_paths(self, enabled: bool) -> None:
        self._write_stored_paths = bool(enabled)

    def _stored(self, os_path: Path) -> Path:
        """Where a NEW entry points, as the collection stores paths."""
        if not self._write_stored_paths:
            return Path(os_path)
        return stored_form(Path(os_path), [self._path_mapping, *self._session_mappings])

    def set_session_mappings(self, mappings: list[PathMapping]) -> None:
        """Set the mappings confirmed for this session (applied at resolve time)."""
        with self._lock:
            self._session_mappings = [m for m in mappings if not m.empty]

    def _resolve(self, loc) -> "Path | None":
        """Resolve a LOCATION to an OS path, applying the active path mappings.

        The single FS chokepoint: LOCATION -> `resolve_path` -> prefix remap.
        The saved mapping is tried first, then — only for a file still not
        found — the session's. Falls back to the un-remapped path when no
        remapped target exists, so a misconfigured mapping never makes a
        present file unreachable.
        """

        if loc is None:
            return None
        base = resolve_path(loc.volume, loc.dir, loc.file)
        if not self._path_mapping.empty:
            remapped = self._path_mapping.apply(base)
            if remapped != base and remapped.exists():
                return remapped
        if self._session_mappings and not base.exists():
            for mapping in self._session_mappings:
                remapped = mapping.apply(base)
                if remapped != base and remapped.exists():
                    return remapped
        return base

    def unresolved_path_groups(self) -> list[PathGroup]:
        """Each stored VOLUME in which not one track resolves, with its tracks'
        stored paths — the input to the open-time missing-files search.

        "Not one" is the threshold: a volume missing a few deleted files is a
        library with a few deleted files, not a library that needs remapping.
        The stats happen outside the lock; checking stops at a volume's first
        present file, and a missing path fails fast, so this is cheap either way.
        """
        with self._lock:
            by_volume: dict[str, list] = {}
            for e in self._nml.collection.entry:
                if e.location is not None and e.location.file:
                    by_volume.setdefault(e.location.volume or "", []).append(e.location)
        groups = []
        for volume, locs in by_volume.items():
            if any(self._resolve(loc).exists() for loc in locs):
                continue
            groups.append(
                PathGroup(
                    label=volume,
                    root=str(resolve_path(volume, "/:", "")),
                    paths=[str(resolve_path(l.volume, l.dir, l.file)) for l in locs],
                )
            )
        return groups

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

    def time_offset_ms(self, entry: Entrytype) -> float:
        """How far this entry's positions sit behind the decoded audio, in ms
        (see `timebase`). Every seconds <-> START conversion goes through it:
        the four writes here and `projection.to_track_cues` on the way out."""
        return timebase.offset_ms(self._resolve(entry.location) if entry.location else None)

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

    def _plan_remap(self, mapping: PathMapping):
        """Where a mapping would move each matching entry, and what that clashes
        with — computed before anything changes. Caller holds the lock.

        Returns ``(moves, clashes)``: ``moves`` is ``[(entry, old_key, new_key,
        (volume, dir, file))]`` for every entry whose key would change, and
        ``clashes`` maps each clashing NEW key to the old keys landing on it.

        A key is a Traktor primary key, so two ENTRYs sharing one is a corrupt
        collection (`add_entry` refuses the same thing). A new key clashes when
        tracks with DIFFERENT old keys land on it, or when it is the key of an
        entry that stays where it is. A chain or swap — A onto B's old path
        while B moves on — is fine, and so is a duplicate the collection already
        had: two entries sharing one key move together, no worse than before.
        """
        moves = []
        for e in self._nml.collection.entry:
            loc = e.location
            if not loc:
                continue
            base = resolve_path(loc.volume, loc.dir, loc.file)
            if not mapping.matches(base):
                continue
            volume, dir_, file = os_path_to_location(mapping.apply(base))
            old_key = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"
            new_key = f"{volume or ''}{dir_ or ''}{file or ''}"
            if old_key != new_key:  # a no-op (e.g. from == to) stays byte-identical
                moves.append((e, old_key, new_key, (volume, dir_, file)))
        moving = {id(e) for e, *_ in moves}
        staying = {self._key_of(e) for e in self._nml.collection.entry
                   if e.location and id(e) not in moving}
        landing: dict[str, set[str]] = {}
        for _e, old_key, new_key, _loc in moves:
            landing.setdefault(new_key, set()).add(old_key)
        clashes = {
            new_key: sorted(olds) for new_key, olds in landing.items()
            if len(olds) > 1 or new_key in staying
        }
        return moves, clashes

    def remap_preview(self, mapping: PathMapping) -> dict:
        """How a mapping would affect the collection, without changing anything:
        total tracks, how many match ``from``, how many exist at ``to``, and how
        many would CLASH with another track's path (which `remap_locations`
        refuses) — plus a few samples. Powers the mapping editor's validation."""

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
            _moves, clashes = self._plan_remap(mapping)
            return {
                "total": total, "matched": matched, "existing": existing, "samples": samples,
                "collisions": len(clashes),
                "collision_samples": [self._display_key(k) for k in sorted(clashes)[:5]],
            }

    @staticmethod
    def _display_key(key: str) -> str:
        """A primary key as a readable path ("Volume/:dir/:file" -> "Volume/dir/file")."""
        return key.replace("/:", "/")

    def remap_locations(self, mapping: PathMapping) -> dict[str, str]:
        """Permanently rewrite matching track LOCATIONs to the mapping's ``to``
        prefix — a deliberate library move (write-back).

        Because ``track_id`` IS the LOCATION key (``volume+dir+file``), every
        playlist ``PRIMARYKEY`` that referenced a moved track is rewritten too,
        so playlists keep pointing at their tracks. Non-matching entries are
        untouched. Returns ``{old track id: new track id}`` for every track
        rewritten — callers holding ids (export sets) follow them with it, and
        must not rebuild it from paths; the caller saves.

        **Refused, with nothing changed, if any track would land on another
        track's path** (see `_plan_remap`): the collection would then hold two
        ENTRYs sharing a primary key. Every re-key below is applied AT ONCE, not
        track by track, so a chain (A onto B's old path, B onward) cannot carry
        A's staged art or edits along to B's destination.
        """

        if mapping.empty:
            return {}
        with self._lock:
            moves, clashes = self._plan_remap(mapping)
            if clashes:
                paths = [self._display_key(k) for k in sorted(clashes)]
                shown = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
                raise PlaylistError(
                    f"Remapping would give {len(paths)} path{'s' if len(paths) != 1 else ''} to more "
                    f"than one track ({shown}). Two entries sharing a path corrupt the collection, "
                    "so nothing was changed."
                )
            if not moves:
                return {}
            key_remap = {old_key: new_key for _e, old_key, new_key, _loc in moves}
            for e, _old, _new, (volume, dir_, file) in moves:
                e.location.volume, e.location.dir, e.location.file = volume, dir_, file
            # Re-key the index too. Everything that finds an ENTRY by track id
            # goes through it — later edits, and the file-tag sync on save,
            # which would otherwise silently skip every edit made to this track
            # before the remap. Drop every old key first, then add every new
            # one, so a chain cannot delete an entry another has just claimed.
            for e, old_key, _new, _loc in moves:
                if self._entry_by_key.get(old_key) is e:
                    del self._entry_by_key[old_key]
            for e, _old, new_key, _loc in moves:
                self._entry_by_key[new_key] = e
            art = {old: self._track_art.pop(old) for old in key_remap if old in self._track_art}
            for old, staged in art.items():
                self._track_art[key_remap[old]] = staged
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
            self._journal.retarget_many(key_remap)
            self._note("remap", str(len(key_remap)))
            self.dirty = True
            return key_remap

    # ---- stem conversion ---------------------------------------------------
    #: Whether a converted entry keeps Traktor's analysis fingerprint (AUDIO_ID).
    #: KEPT, as measured in Traktor 4.5 (kit 5, 2026-09-30): with it kept, Traktor
    #: loads the stem entry as it is — cues, grid and fingerprint untouched. With
    #: it cleared, Traktor re-analyses on load: it keeps grid POSITIONS (even a
    #: hand-nudged, unlocked one) but re-measures the grid's BPM (125.000023 ->
    #: 125.00042), i.e. it rewrites prep the user set. The stem file's mix is the
    #: same audio as the original, so the old fingerprint still describes it.
    _KEEP_AUDIO_ID_ON_STEM = True

    def apply_stem_swaps(self, swaps: list, *, add_to_playlist: str | None = None) -> StemSwapResult:
        """Point entries at converted stem files ("repoint"), or add entries for
        them ("add"), ALL AT ONCE — every swap validated before any is applied,
        so a clash leaves the collection untouched.

        What a stem entry is, measured on the real collection: the ordinary
        ENTRY plus a `<STEMS>` element (after CUE_V2), and playlist PRIMARYKEYs of
        `TYPE="STEM"`. INFO's bitrate / playtime / playtime_float / filesize (KB)
        describe the new file.

        **Positions move with the time base.** An MP3 with a header sits
        `timebase.offset_ms` later on Traktor's clock than the decoded audio; the
        stem file (an MP4 whose edit list Traktor honours) sits at 0. So every
        START shifts by (new offset - old offset), with the old offset read from
        `original_audio` — where the original's bytes are NOW (its parked name in
        Replace mode) — under the original's own suffix. Read from the old path
        instead, the file would be gone, the offset 0, and every cue ~51 ms late.
        A shifted grid anchor that would fall before 0 moves forward by whole
        beats (its companion with it), which leaves the grid itself unchanged; a
        hotcue that would is clamped to 0 and reported.
        """
        with self._lock:
            # ---- validate everything first ----
            seen_ids: set[str] = set()
            planned: list[tuple[object, Entrytype, str, tuple[str, str, str]]] = []
            new_keys: set[str] = set()
            for swap in swaps:
                entry = self._entry_by_key.get(swap.track_id)
                if entry is None or entry.location is None:
                    raise PlaylistError(f"Track not found: {swap.track_id}")
                if swap.track_id in seen_ids:
                    raise PlaylistError(f"Track listed twice: {swap.track_id}")
                if swap.mode not in ("repoint", "add"):
                    raise PlaylistError(f"Unknown stem swap mode: {swap.mode}")
                seen_ids.add(swap.track_id)
                loc = os_path_to_location(self._stored(Path(swap.stem_path)))
                new_key = "".join(x or "" for x in loc)
                if new_key in self._entry_by_key or new_key in new_keys:
                    raise PlaylistError(
                        f"The collection already has an entry for {swap.stem_path}"
                    )
                new_keys.add(new_key)
                planned.append((swap, entry, new_key, loc))
            playlist = None
            if add_to_playlist is not None:
                playlist = self._find_playlist_node(add_to_playlist)
                if playlist is None or playlist.playlist is None:
                    raise PlaylistError(f"Playlist not found: {add_to_playlist}")

            # ---- apply ----
            result = StemSwapResult()
            renames: dict[str, str] = {}
            for swap, entry, new_key, loc in planned:
                old_key = swap.track_id
                old_suffix = Path(entry.location.file or "").suffix
                shift = timebase.offset_ms(swap.stem_path) - timebase.offset_ms(
                    swap.original_audio, suffix=old_suffix)
                target = entry if swap.mode == "repoint" else copy.deepcopy(entry)
                clamped = self._retime(target, shift)
                self._point_at_stem(target, loc, swap)
                if swap.mode == "repoint":
                    renames[old_key] = new_key
                    if self._entry_by_key.get(old_key) is entry:
                        del self._entry_by_key[old_key]
                    self._entry_by_key[new_key] = entry
                    if old_key in self._track_art:
                        self._track_art[new_key] = self._track_art.pop(old_key)
                else:
                    self._nml.collection.entry.append(target)
                    self._entry_by_key[new_key] = target
                    # Unsaved tag edits and staged art must reach the NEW file
                    # too on Save; the original keeps its own.
                    for field in self._journal.fields_for(old_key):
                        self._journal.record("track", "set", new_key, field)
                    if old_key in self._track_art:
                        self._track_art[new_key] = self._track_art[old_key]
                result.renamed[old_key] = new_key
                if clamped:
                    result.clamped[new_key] = clamped
                self._journal.record("stem", swap.mode, new_key, old_key)
            if any(s.mode == "add" for s, *_ in planned):
                self._nml.collection.entries = len(self._nml.collection.entry)
            # Playlists follow a repointed track — and it is a STEM now.
            if renames:
                for node in self._iter_nodes(self._root()):
                    pl = node.playlist
                    if pl is None or not pl.entry:
                        continue
                    for pe in pl.entry:
                        pk = pe.primarykey
                        if pk and pk.key in renames:
                            pk.key = renames[pk.key]
                            pk.type = "STEM"
                self._journal.retarget_many(renames)
            if playlist is not None:
                added = [new for (s, _e, new, _l) in planned if s.mode == "add"]
                pl = playlist.playlist
                pl.entry = list(pl.entry or []) + [
                    Entrytype(primarykey=Primarykeytype(type="STEM", key=k)) for k in added
                ]
                pl.entries = len(pl.entry)
                self._note("playlist-entries", playlist.name)
            self.dirty = True
            return result

    def _point_at_stem(self, entry: Entrytype, loc: tuple[str, str, str], swap) -> None:
        """LOCATION, `<STEMS>` and the file-describing INFO fields, for the stem file."""
        volume, dir_, file = loc
        old_volume = entry.location.volume if entry.location else None
        volumeid = entry.location.volumeid if entry.location else None
        if volume != old_volume:
            # VOLUMEID names a volume, so it cannot follow the file to another one;
            # borrow it from any entry already on that volume, else leave it for
            # Traktor to fill in (as `add_entry` does).
            volumeid = next(
                (e.location.volumeid for e in self._nml.collection.entry
                 if e.location and e.location.volume == volume and e.location.volumeid),
                None,
            )
        entry.location = Locationtype(volume=volume, dir=dir_, file=file, volumeid=volumeid)
        entry.stems = Stemstype(stems=NML_STEMS_JSON)
        if entry.info is None:
            entry.info = Infotype()
        entry.info.bitrate = int(swap.bit_rate)
        entry.info.playtime = int(round(swap.duration))
        entry.info.playtime_float = float(swap.duration)
        entry.info.filesize = int(round(swap.size / 1024))
        if not self._KEEP_AUDIO_ID_ON_STEM:
            entry.audio_id = None

    def _retime(self, entry: Entrytype, shift_ms: float) -> list[int]:
        """Shift every cue and grid marker by `shift_ms`; returns the hotcue
        slots clamped at 0. Companions move WITH their marker, as the same float,
        so the pairing survives exactly."""
        if not shift_ms or not entry.cue_v2:
            return []
        markers = beatgrid.grid_markers(entry)
        comps = beatgrid.companions(entry)
        comp_ids = {id(c) for c in comps.values()}
        clamped: list[int] = []
        for c in entry.cue_v2:
            if c.grid is not None or id(c) in comp_ids:
                continue
            start = (c.start or 0.0) + shift_ms
            if start < 0:
                if c.hotcue is not None and c.hotcue >= 0:
                    clamped.append(c.hotcue)
                start = 0.0
            c.start = start
        for i, m in enumerate(markers):
            start = (m.start or 0.0) + shift_ms
            if start < 0:
                bpm = m.grid.bpm if m.grid and m.grid.bpm else 0.0
                if bpm > 0:
                    beat = 60000.0 / bpm
                    start += math.ceil(-start / beat) * beat
                    nxt = markers[i + 1].start + shift_ms if i + 1 < len(markers) else None
                    if nxt is not None and start >= nxt:
                        start = 0.0  # a pathological grid: keep the order, lose the phase
                else:
                    start = 0.0
            m.start = start
            if i in comps:
                comps[i].start = m.start
        self._sort_cues(entry)
        return clamped

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
        if field == "comment2":
            return entry.info.rating if entry.info else None
        if field == "release_date":
            # A file tag wants ISO, not the NML's "YYYY/M/D".
            return iso_date(entry.info.release_date) if entry.info else None
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
            fields = self._journal.fields_for(track_id) - self._COLLECTION_ONLY_FIELDS
            if fields:
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
