"""Konduktor FastAPI app.

The collection to work on is chosen at runtime (see /api/collection/open) — the
app starts with nothing loaded and the UI gates on that. Setting KONDUKTOR_NML
in the environment auto-loads that file on startup (used by dev and tests).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .collection_service import CollectionService
from .playlist_store import PlaylistError, PlaylistStore
from .schemas import (
    CollectionStatus,
    CreatePlaylist,
    CuePoint,
    EditState,
    EditTrack,
    Facets,
    FileTagOutcome,
    FsEntry,
    FsListing,
    OpenCollection,
    PlaylistNode,
    RenamePlaylist,
    SaveResult,
    SetEntries,
    SetHotcue,
    SetHotcueType,
    Stats,
    Track,
    TrackCues,
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


@app.patch("/api/tracks")
def edit_track(body: EditTrack) -> dict:
    """Edit a single track's safe metadata fields (in-memory; persisted on save)."""
    store = require_store()
    try:
        store.set_track_metadata(body.track_id, body.fields)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    # Keep the read projection in sync so the edit shows immediately.
    entry = store.model_entry(body.track_id)
    if entry is not None:
        require_service().replace_track(body.track_id, entry)
    return {"status": "updated", "id": body.track_id}


@app.get("/api/tracks/art")
def track_art(track_id: str) -> Response:
    """Stream a track's embedded cover art (staged replacement if unsaved)."""
    art = require_store().cover_art(track_id)
    if art is None:
        raise HTTPException(404, "No cover art")
    data, mime = art
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


_AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".ogg": "audio/ogg",
}


@app.get("/api/tracks/audio")
def track_audio(track_id: str) -> FileResponse:
    """Stream a track's audio file for playback (supports HTTP Range/seeking)."""
    path = require_store().audio_path(track_id)
    if path is None:
        raise HTTPException(404, "Track not found")
    if not path.exists():
        raise HTTPException(404, f"Audio file not found: {path}")
    # .stem.m4a and other MP4s serve as audio/mp4; browsers play the first track.
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    # Cacheable so the waveform's decode-fetch and the <audio> element can share
    # the download (FileResponse adds ETag/Last-Modified for revalidation).
    return FileResponse(path, media_type=mime)


def _build_track_cues(entry) -> TrackCues:
    grid_anchor: float | None = None
    grid_bpm: float | None = None
    cues: list[CuePoint] = []
    for c in entry.cue_v2 or []:
        grid = getattr(c, "grid", None)
        start_sec = (c.start or 0.0) / 1000.0  # Traktor stores cue START in ms
        if grid is not None:
            if grid_anchor is None:
                grid_anchor = start_sec
                grid_bpm = grid.bpm
            continue  # grid markers are represented by the beatgrid, not as cues
        cues.append(
            CuePoint(
                name=c.name,
                type=c.type if c.type is not None else 0,
                start=start_sec,
                length=(c.len or 0.0) / 1000.0,
                hotcue=c.hotcue if c.hotcue is not None else -1,
                color=c.color,
            )
        )
    bpm = grid_bpm if grid_bpm is not None else (entry.tempo.bpm if entry.tempo else None)
    return TrackCues(bpm=bpm, grid_anchor=grid_anchor, cues=cues)


def _cues_for(track_id: str) -> TrackCues:
    entry = require_store().model_entry(track_id)
    if entry is None:
        raise HTTPException(404, "Track not found")
    return _build_track_cues(entry)


@app.get("/api/tracks/cues", response_model=TrackCues)
def track_cues(track_id: str) -> TrackCues:
    """Beatgrid + cue/loop markers for a track (read-only, from the NML)."""
    return _cues_for(track_id)


def _sync_cue_edit(track_id: str) -> TrackCues:
    """After a hotcue edit, refresh the read projection and return fresh cues."""
    store = require_store()
    entry = store.model_entry(track_id)
    if entry is not None:
        require_service().replace_track(track_id, entry)
    return _build_track_cues(entry) if entry is not None else TrackCues()


@app.post("/api/tracks/hotcue", response_model=TrackCues)
def create_hotcue(body: SetHotcue) -> TrackCues:
    """Create (or reposition + retype) the hotcue in a slot at a position."""
    try:
        require_store().set_hotcue(body.track_id, body.slot, body.start, body.type)
    except PlaylistError as ex:
        raise HTTPException(400, str(ex))
    return _sync_cue_edit(body.track_id)


@app.patch("/api/tracks/hotcue", response_model=TrackCues)
def edit_hotcue_type(body: SetHotcueType) -> TrackCues:
    """Change the type of an existing hotcue (keeps its position)."""
    try:
        require_store().set_hotcue_type(body.track_id, body.slot, body.type)
    except PlaylistError as ex:
        raise HTTPException(400, str(ex))
    return _sync_cue_edit(body.track_id)


@app.delete("/api/tracks/hotcue", response_model=TrackCues)
def delete_hotcue(track_id: str, slot: int) -> TrackCues:
    """Remove the hotcue in a slot."""
    try:
        require_store().delete_hotcue(track_id, slot)
    except PlaylistError as ex:
        raise HTTPException(400, str(ex))
    return _sync_cue_edit(track_id)


@app.put("/api/tracks/art")
async def set_track_art(track_id: str = Form(...), file: UploadFile = File(...)) -> dict:
    """Stage replacement cover art for a track (written to the file on save)."""
    store = require_store()
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty image")
    mime = file.content_type or "image/jpeg"
    try:
        store.set_track_art(track_id, data, mime)
    except PlaylistError as ex:
        raise HTTPException(404, str(ex))
    return {"status": "staged", "id": track_id, "bytes": len(data)}


@app.post("/api/save", response_model=SaveResult)
def save() -> SaveResult:
    store = require_store()
    if not store.dirty:
        return SaveResult(saved=False, backup=None, playlists=store.count_playlists())
    outcome = store.save()
    return SaveResult(
        saved=True,
        backup=str(outcome.backup),
        playlists=store.count_playlists(),
        file_tags=[FileTagOutcome(**r.__dict__) for r in outcome.tag_results],
    )
