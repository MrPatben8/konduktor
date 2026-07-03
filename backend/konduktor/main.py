"""Konduktor FastAPI app.

The collection to work on is chosen at runtime (see /api/collection/open) — the
app starts with nothing loaded and the UI gates on that. Setting KONDUKTOR_NML
in the environment auto-loads that file on startup (used by dev and tests).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .collection_service import CollectionService
from .playlist_store import PlaylistError, PlaylistStore
from .schemas import (
    CollectionStatus,
    CreatePlaylist,
    EditState,
    Facets,
    FsEntry,
    FsListing,
    OpenCollection,
    PlaylistNode,
    RenamePlaylist,
    SaveResult,
    SetEntries,
    Stats,
    Track,
    TrackPage,
)

app = FastAPI(title="Konduktor API", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AppState:
    """Holds the currently-loaded collection (selectable at runtime)."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.service: CollectionService | None = None
        self.store: PlaylistStore | None = None

    @property
    def loaded(self) -> bool:
        return self.service is not None

    def open(self, path: Path) -> None:
        # Both raise on an invalid/parse-incompatible or playlist-less file;
        # only commit to the new collection if BOTH succeed.
        service = CollectionService(path)
        store = PlaylistStore(path)
        self.path, self.service, self.store = path, service, store


STATE = AppState()

# Optional auto-load for dev/tests.
_env_nml = os.environ.get("KONDUKTOR_NML")
if _env_nml and Path(_env_nml).exists():
    try:
        STATE.open(Path(_env_nml))
    except Exception:  # noqa: BLE001 — bad env path shouldn't crash startup
        pass


def require_service() -> CollectionService:
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    assert STATE.service is not None
    return STATE.service


def require_store() -> PlaylistStore:
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    assert STATE.store is not None
    return STATE.store


# ---- collection selection ---------------------------------------------


@app.get("/api/collection", response_model=CollectionStatus)
def collection_status() -> CollectionStatus:
    if not STATE.loaded:
        return CollectionStatus(loaded=False)
    return CollectionStatus(
        loaded=True,
        path=str(STATE.path),
        tracks=len(require_service().tracks),
        playlists=require_store().count_playlists(),
    )


@app.post("/api/collection/open", response_model=CollectionStatus)
def open_collection(body: OpenCollection) -> CollectionStatus:
    path = Path(body.path).expanduser()
    if not path.exists() or not path.is_file():
        raise HTTPException(400, f"File not found: {path}")
    try:
        STATE.open(path)
    except PlaylistError as ex:
        raise HTTPException(400, f"Not a valid Traktor collection: {ex}")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, f"Could not parse as a Traktor collection: {ex}")
    return collection_status()


@app.get("/api/fs/list", response_model=FsListing)
def fs_list(path: str | None = None) -> FsListing:
    """List directories and .nml files for the in-app file browser."""
    base = Path(path).expanduser() if path else Path.home()
    if not base.exists() or not base.is_dir():
        base = Path.home()
    base = base.resolve()
    dirs: list[FsEntry] = []
    files: list[FsEntry] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append(FsEntry(name=entry.name, path=str(entry)))
                elif entry.suffix.lower() == ".nml":
                    files.append(FsEntry(name=entry.name, path=str(entry)))
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(base.parent) if base.parent != base else None
    return FsListing(
        path=str(base), parent=parent, home=str(Path.home()), dirs=dirs, files=files
    )


# ---- health / state ---------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "loaded": STATE.loaded, "path": str(STATE.path) if STATE.path else None}


@app.get("/api/state", response_model=EditState)
def state() -> EditState:
    store = require_store()
    return EditState(dirty=store.dirty, nml_path=str(STATE.path))


@app.post("/api/reload")
def reload_collection() -> dict:
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    STATE.open(STATE.path)  # re-parse current file from disk
    return {"status": "reloaded", "tracks": len(require_service().tracks)}


# ---- read: stats / facets / tracks ------------------------------------


@app.get("/api/stats", response_model=Stats)
def stats() -> Stats:
    return require_service().stats(playlist_count=require_store().count_playlists())


@app.get("/api/facets", response_model=Facets)
def facets() -> Facets:
    return require_service().facets()


@app.get("/api/tracks", response_model=TrackPage)
def tracks(
    q: str | None = None,
    genre: str | None = None,
    key: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    rating_min: int | None = Query(None, ge=0, le=5),
    has_cues: bool | None = None,
    sort: str = "artist",
    order: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=20000),
    offset: int = Query(0, ge=0),
) -> TrackPage:
    return require_service().query_tracks(
        q=q, genre=genre, key=key, bpm_min=bpm_min, bpm_max=bpm_max,
        rating_min=rating_min, has_cues=has_cues, sort=sort, order=order,
        limit=limit, offset=offset,
    )


# ---- read: playlists --------------------------------------------------


@app.get("/api/playlists", response_model=list[PlaylistNode])
def playlists() -> list[PlaylistNode]:
    return require_store().tree()


@app.get("/api/playlists/{playlist_id}/tracks", response_model=list[Track])
def playlist_tracks(playlist_id: str) -> list[Track]:
    keys = require_store().entry_keys(playlist_id)
    if keys is None:
        raise HTTPException(404, f"Playlist not found: {playlist_id}")
    svc = require_service()
    return [svc.by_key[k] for k in keys if k in svc.by_key]


# ---- write: playlist editing ------------------------------------------


@app.post("/api/playlists", response_model=PlaylistNode)
def create_playlist(body: CreatePlaylist) -> PlaylistNode:
    store = require_store()
    try:
        new_uuid = store.create_playlist(body.name.strip() or "New Playlist", body.parent_id)
    except PlaylistError as ex:
        raise HTTPException(400, str(ex))
    return PlaylistNode(id=new_uuid, name=body.name, type="PLAYLIST", uuid=new_uuid, count=0)


@app.patch("/api/playlists/{playlist_uuid}")
def rename_playlist(playlist_uuid: str, body: RenamePlaylist) -> dict:
    try:
        require_store().rename_playlist(playlist_uuid, body.name.strip())
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "renamed", "id": playlist_uuid, "name": body.name}


@app.delete("/api/playlists/{playlist_uuid}")
def delete_playlist(playlist_uuid: str) -> dict:
    try:
        require_store().delete_playlist(playlist_uuid)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "deleted", "id": playlist_uuid}


@app.put("/api/playlists/{playlist_uuid}/entries")
def set_entries(playlist_uuid: str, body: SetEntries) -> dict:
    store = require_store()
    entries = require_service().entries_for(body.track_ids)
    try:
        store.set_entries(playlist_uuid, entries)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "updated", "id": playlist_uuid, "count": len(entries)}


@app.post("/api/playlists/{playlist_uuid}/add")
def add_entries(playlist_uuid: str, body: SetEntries) -> dict:
    """Append tracks to a playlist (skips ids already present)."""
    store = require_store()
    current = store.entry_keys(playlist_uuid)
    if current is None:
        raise HTTPException(404, f"Playlist not found: {playlist_uuid}")
    have = set(current)
    added = [tid for tid in body.track_ids if tid not in have]
    entries = require_service().entries_for(current + added)
    store.set_entries(playlist_uuid, entries)
    return {"status": "added", "id": playlist_uuid, "added": len(added), "count": len(entries)}


@app.post("/api/save", response_model=SaveResult)
def save() -> SaveResult:
    store = require_store()
    if not store.dirty:
        return SaveResult(saved=False, backup=None, playlists=store.count_playlists())
    backup = store.save()
    return SaveResult(saved=True, backup=str(backup), playlists=store.count_playlists())
