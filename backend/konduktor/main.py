"""Konduktor FastAPI app — Phase 1 (read-only collection API)."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .collection_service import CollectionService
from .schemas import Facets, PlaylistNode, Stats, Track, TrackPage

# The collection to read. Defaults to the repo-root collection.nml.
# Reading is safe/non-destructive; writes (later phases) always use a copy.
DEFAULT_NML = Path(__file__).resolve().parents[2] / "collection.nml"
NML_PATH = Path(os.environ.get("KONDUKTOR_NML", DEFAULT_NML))

app = FastAPI(title="Konduktor API", version="0.1.0")

# Vite dev server runs on 5173; allow local dev origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

_service: CollectionService | None = None


def get_service() -> CollectionService:
    global _service
    if _service is None:
        if not NML_PATH.exists():
            raise HTTPException(500, f"NML file not found: {NML_PATH}")
        _service = CollectionService(NML_PATH)
    return _service


@app.get("/api/health")
def health() -> dict:
    exists = NML_PATH.exists()
    return {
        "status": "ok",
        "nml_path": str(NML_PATH),
        "nml_exists": exists,
        "loaded": _service is not None,
    }


@app.post("/api/reload")
def reload_collection() -> dict:
    svc = get_service()
    svc.load()
    return {"status": "reloaded", "tracks": len(svc.tracks)}


@app.get("/api/stats", response_model=Stats)
def stats() -> Stats:
    return get_service().stats()


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
        q=q,
        genre=genre,
        key=key,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        rating_min=rating_min,
        has_cues=has_cues,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@app.get("/api/playlists", response_model=list[PlaylistNode])
def playlists() -> list[PlaylistNode]:
    return get_service().playlist_tree()


@app.get("/api/playlists/{playlist_id}/tracks", response_model=list[Track])
def playlist_tracks(playlist_id: str) -> list[Track]:
    result = get_service().playlist_tracks(playlist_id)
    if result is None:
        raise HTTPException(404, f"Playlist not found: {playlist_id}")
    return result
