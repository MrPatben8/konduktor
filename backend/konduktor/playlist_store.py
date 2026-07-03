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

import re
import shutil
import threading
import time
import uuid as uuidlib
from pathlib import Path

from traktor_nml_utils import (
    TraktorCollection,
    format_traktor_layout,
    restore_traktor_float_format,
)
from traktor_nml_utils.models.collection import (
    Entrytype,
    Nodetype,
    Playlisttype,
    Primarykeytype,
    Subnodestype,
)
from xsdata.formats.dataclass.serializers import XmlSerializer

from .schemas import PlaylistNode

_PLAYLISTS_OPEN = re.compile(rb"<PLAYLISTS[ >]")
_PLAYLISTS_CLOSE = re.compile(rb"</PLAYLISTS>")


class PlaylistError(Exception):
    pass


class PlaylistStore:
    def __init__(self, nml_path: Path):
        self.nml_path = Path(nml_path)
        self._lock = threading.RLock()
        self.dirty = False
        self._load()

    # ---- load ----------------------------------------------------------
    def _load(self) -> None:
        self._raw = self.nml_path.read_bytes()
        mo = _PLAYLISTS_OPEN.search(self._raw)
        mc = _PLAYLISTS_CLOSE.search(self._raw)
        if not mo or not mc:
            raise PlaylistError("Could not locate <PLAYLISTS> section in NML")
        self._span = (mo.start(), mc.end())
        self._collection = TraktorCollection(path=self.nml_path)
        self._nml = self._collection.nml
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

    def count_playlists(self) -> int:
        with self._lock:
            return sum(1 for n in self._iter_nodes(self._root()) if n.playlist is not None)

    # ---- persistence ---------------------------------------------------
    def save(self) -> Path:
        with self._lock:
            backup = self._backup()
            # Render the full model exactly as the library's save() does — the
            # two post-processing steps restore Traktor's precise layout (expanded
            # empty tags, 6-decimal floats, newlines), so unedited playlists come
            # out byte-for-byte identical and only edited ones diff.
            serialized = XmlSerializer().render(self._nml)
            serialized = restore_traktor_float_format(serialized, self._nml)
            serialized = format_traktor_layout(serialized)
            rendered = serialized.encode("utf-8")
            ro = _PLAYLISTS_OPEN.search(rendered)
            rc = _PLAYLISTS_CLOSE.search(rendered)
            if not ro or not rc:
                raise PlaylistError("Rendered output missing PLAYLISTS section")
            new_pl = rendered[ro.start() : rc.end()]

            s, e = self._span
            new_data = self._raw[:s] + new_pl + self._raw[e:]
            tmp = self.nml_path.with_suffix(self.nml_path.suffix + ".tmp")
            tmp.write_bytes(new_data)
            tmp.replace(self.nml_path)
            self._load()
            return backup

    def _backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        backup = self.nml_path.with_name(f"{self.nml_path.name}.{stamp}.bak")
        n = 1
        while backup.exists():
            backup = self.nml_path.with_name(f"{self.nml_path.name}.{stamp}-{n}.bak")
            n += 1
        shutil.copy2(self.nml_path, backup)
        return backup
