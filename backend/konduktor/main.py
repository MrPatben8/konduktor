"""Konduktor FastAPI app.

Phase 1-2: read-only collection API. Phase 3: playlist editing with
surgical, backup-first saves (see playlist_store.py).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .collection_service import CollectionService
from .playlist_store import PlaylistError, PlaylistStore
from .schemas import (
    CreatePlaylist,
    EditState,
    Facets,
    PlaylistNode,
    RenamePlaylist,
    SaveResult,
    SetEntries,
    Stats,
    Track,
    TrackPage,
)

# The collection to read/edit. Defaults to the repo-root collection.nml.
DEFAULT_NML = Path(__file__).resolve().parents[2] / "collection.nml"
NML_PATH = Path(os.environ.get("KONDUKTOR_NML", DEFAULT_NML))

app = FastAPI(title="Konduktor API", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_service: CollectionService | None = None
_store: PlaylistStore | None = None


def get_service() -> CollectionService:
    global _service
    if _service is None:
        if not NML_PATH.exists():
            raise HTTPException(500, f"NML file not found: {NML_PATH}")
        _service = CollectionService(NML_PATH)
    return _service


def get_store() -> PlaylistStore:
    global _store
    if _store is None:
        if not NML_PATH.exists():
            raise HTTPException(500, f"NML file not found: {NML_PATH}")
        _store = PlaylistStore(NML_PATH)
    return _store


# ---- health / state ---------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "nml_path": str(NML_PATH),
        "nml_exists": NML_PATH.exists(),
        "loaded": _service is not None,
    }


@app.get("/api/state", response_model=EditState)
def state() -> EditState:
    return EditState(dirty=get_store().dirty, nml_path=str(NML_PATH))


@app.post("/api/reload")
def reload_collection() -> dict:
    get_service().load()
    get_store()._load()
    return {"status": "reloaded", "tracks": len(get_service().tracks)}


# ---- read: stats / facets / tracks ------------------------------------


@app.get("/api/stats", response_model=Stats)
def stats() -> Stats:
    return get_service().stats(playlist_count=get_store().count_playlists())


@app.get("/api/facets", response_model=Facets)
def facets() -> Facets:
    return get_service().facets()


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
    return get_service().query_tracks(
        q=q, genre=genre, key=key, bpm_min=bpm_min, bpm_max=bpm_max,
        rating_min=rating_min, has_cues=has_cues, sort=sort, order=order,
        limit=limit, offset=offset,
    )


# ---- read: playlists (served from the editable store) -----------------


@app.get("/api/playlists", response_model=list[PlaylistNode])
def playlists() -> list[PlaylistNode]:
    return get_store().tree()


@app.get("/api/playlists/{playlist_id}/tracks", response_model=list[Track])
def playlist_tracks(playlist_id: str) -> list[Track]:
    keys = get_store().entry_keys(playlist_id)
    if keys is None:
        raise HTTPException(404, f"Playlist not found: {playlist_id}")
    svc = get_service()
    return [svc.by_key[k] for k in keys if k in svc.by_key]


# ---- write: playlist editing ------------------------------------------


@app.post("/api/playlists", response_model=PlaylistNode)
def create_playlist(body: CreatePlaylist) -> PlaylistNode:
    store = get_store()
    try:
        new_uuid = store.create_playlist(body.name.strip() or "New Playlist", body.parent_id)
    except PlaylistError as ex:
        raise HTTPException(400, str(ex))
    return PlaylistNode(id=new_uuid, name=body.name, type="PLAYLIST", uuid=new_uuid, count=0)


@app.patch("/api/playlists/{playlist_uuid}")
def rename_playlist(playlist_uuid: str, body: RenamePlaylist) -> dict:
    try:
        get_store().rename_playlist(playlist_uuid, body.name.strip())
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "renamed", "id": playlist_uuid, "name": body.name}


@app.delete("/api/playlists/{playlist_uuid}")
def delete_playlist(playlist_uuid: str) -> dict:
    try:
        get_store().delete_playlist(playlist_uuid)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "deleted", "id": playlist_uuid}


@app.put("/api/playlists/{playlist_uuid}/entries")
def set_entries(playlist_uuid: str, body: SetEntries) -> dict:
    store = get_store()
    entries = get_service().entries_for(body.track_ids)
    try:
        store.set_entries(playlist_uuid, entries)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "updated", "id": playlist_uuid, "count": len(entries)}


@app.post("/api/playlists/{playlist_uuid}/add")
def add_entries(playlist_uuid: str, body: SetEntries) -> dict:
    """Append tracks to a playlist (skips ids already present)."""
    store = get_store()
    current = store.entry_keys(playlist_uuid)
    if current is None:
        raise HTTPException(404, f"Playlist not found: {playlist_uuid}")
    have = set(current)
    added = [tid for tid in body.track_ids if tid not in have]
    entries = get_service().entries_for(current + added)
    store.set_entries(playlist_uuid, entries)
    return {"status": "added", "id": playlist_uuid, "added": len(added), "count": len(entries)}


@app.post("/api/save", response_model=SaveResult)
def save() -> SaveResult:
    store = get_store()
    if not store.dirty:
        return SaveResult(saved=False, backup=None, playlists=store.count_playlists())
    backup = store.save()
    return SaveResult(saved=True, backup=str(backup), playlists=store.count_playlists())
