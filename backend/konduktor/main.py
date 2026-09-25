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
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, exporter, exports, history, prefs
from .app_state import STATE
from .core import auto_hotcues as ah
from .core import grid_detect, structure
from . import importer
from .core import export as core_export
from .core import places, registry
from .core.adapter import (
    AdapterError,
    InvalidCommand,
    LibraryAdapter,
    LibraryNotSupported,
    NotFound,
    Unsupported,
)
from .core.capabilities import Capabilities
from .jobs import JOBS
from .core.pathmap import PathMapping
from .schemas import (
    AutoGridBatchRequest,
    AutoGridRequest,
    AutoCueOutcome,
    AutoHotcue,
    AutoHotcuesBatchRequest,
    TrackIds,
    AutoHotcuesRequest,
    AutoHotcuesResult,
    CollectionCandidate,
    CollectionOptions,
    CollectionStatus,
    CreatePlaylist,
    EditState,
    LibraryInfo,
    PlatformOption,
    EditTrack,
    Facets,
    FileTagOutcome,
    CreateExportSet,
    ExportContents,
    ExportPlaylistOut,
    ExportSetMembers,
    ExportSetOut,
    ExportPreview,
    FsEntry,
    FsListing,
    FsPlace,
    UpdateExportSet,
    AddGridMarker,
    GridMarkerEdit,
    ReplaceGrid,
    HistoryEntry,
    ImportRequest,
    JobStatus,
    OpenCollection,
    OpenSource,
    PathMappingInfo,
    PlaylistNode,
    PrefixSuggestions,
    RemapPreview,
    RemapResult,
    RenamePlaylist,
    SaveResult,
    SetEntries,
    SourceCandidate,
    SourceStatus,
    SetCue,
    SetCueType,
    SetGridLock,
    Stats,
    Track,
    TrackCues,
    TrackPage,
)

app = FastAPI(title="Konduktor API", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # Packaged desktop app: the webview's origin when Tauri loads the built
        # frontend (macOS uses tauri://, Windows/Linux use https://tauri.localhost).
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_adapter() -> LibraryAdapter:
    """The loaded library, or a 409. Routes talk to this and nothing else."""
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    assert STATE.adapter is not None
    return STATE.adapter


@app.exception_handler(AdapterError)
def _adapter_error(_request, exc: AdapterError):
    """Map the adapter's error vocabulary onto status codes in one place, so no
    route has to guess whether a given failure is a 400 or a 404."""
    status = {
        NotFound: 404,
        InvalidCommand: 400,
        Unsupported: 422,
        LibraryNotSupported: 400,
    }.get(type(exc), 400)
    return JSONResponse(status_code=status, content={"detail": str(exc)})


# ---- collection selection ---------------------------------------------


@app.get("/api/library", response_model=CollectionStatus)
def collection_status() -> CollectionStatus:
    if not STATE.loaded:
        return CollectionStatus(loaded=False)
    return CollectionStatus(
        loaded=True,
        path=str(STATE.path),
        tracks=len(require_adapter().tracks),
        playlists=require_adapter().playlist_count(),
        library=_library_info(),
    )


@app.post("/api/library/open", response_model=CollectionStatus)
def open_collection(body: OpenCollection) -> CollectionStatus:
    path = Path(body.path).expanduser()
    # Deliberately NOT `is_file()`. A library is a file on some platforms and a
    # DIRECTORY on others — OneLibrary's is the drive, Serato's will be a
    # `_Serato_` folder — and which shapes are valid is the driver's business.
    # This checks only that the path is there at all; `can_open()` decides.
    if not path.exists():
        raise HTTPException(400, f"Not found: {path}")
    # A running analysis writes into the library being replaced; stop it.
    for kind in BATCH_JOBS:
        for job in JOBS.active(kind):
            JOBS.cancel(job.id)
    try:
        STATE.open(path)
    except AdapterError as ex:
        raise HTTPException(400, f"Not a valid collection: {ex}")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, f"Could not open that collection: {ex}")
    prefs.set_last_collection(str(path), require_adapter().capabilities().platform)
    return collection_status()


@app.get("/api/library/options", response_model=CollectionOptions)
def collection_options(platform: str | None = None) -> CollectionOptions:
    """Startup shortcuts for the picker, scoped to one platform.

    Scoped because the picker asks which platform first, and both shortcuts are
    only meaningful inside that answer: unscoped, "auto" meant "whatever the
    first-registered driver happened to find" — so a plugged-in USB stick could
    be offered as the user's collection — and "last opened" could name a library
    of the platform they had just declined.

    `recent` may no longer exist (→ `exists=False`), which the picker shows
    rather than hides: a library that has moved is worth saying so about.
    """
    detected = registry.detect_for(platform) if platform else registry.detect_all()
    candidates = [CollectionCandidate(**d) for d in detected]
    recent = None
    last = prefs.get_last_collection(platform)
    if last:
        recent = CollectionCandidate(**registry.describe(Path(last)))
    return CollectionOptions(
        auto=candidates[0] if candidates else None,
        detected=candidates,
        recent=recent,
    )


@app.get("/api/library/path-mapping", response_model=PathMappingInfo)
def get_path_mapping() -> PathMappingInfo:
    """The current collection's saved OS-path remapping (blank if none)."""
    require_adapter()
    saved = prefs.get_path_mapping(str(STATE.path))
    if saved:
        return PathMappingInfo.model_validate({"from": saved["from"], "to": saved["to"]})
    return PathMappingInfo()


@app.put("/api/library/path-mapping", response_model=PathMappingInfo)
def put_path_mapping(body: PathMappingInfo) -> PathMappingInfo:
    """Set (or, with blank prefixes, clear) the current collection's remapping.
    Persists to userprefs AND updates the live store so playback/analysis
    re-resolve immediately — no reopen needed. Never touches the .nml."""
    a = require_adapter()
    mapping = PathMapping.make(body.from_, body.to)
    prefs.set_path_mapping(
        str(STATE.path), mapping.from_prefix or None, mapping.to_prefix or None
    )
    a.set_path_mapping(mapping)
    return PathMappingInfo.model_validate(
        {"from": mapping.from_prefix, "to": mapping.to_prefix}
    )


@app.get("/api/library/path-mapping/suggest", response_model=PrefixSuggestions)
def suggest_path_prefix() -> PrefixSuggestions:
    """Auto-detected `from` prefix candidates (common directory per volume),
    so the editor can prefill the stored prefix."""
    return PrefixSuggestions.model_validate(require_adapter().path_prefix_suggestions())


@app.get("/api/library/path-mapping/preview", response_model=RemapPreview)
def preview_path_mapping(
    from_: str = Query("", alias="from"), to: str = Query("")
) -> RemapPreview:
    """Validate a candidate mapping before saving/committing: how many tracks
    match `from` and how many exist at `to`."""
    return RemapPreview.model_validate(
        require_adapter().remap_preview(PathMapping.make(from_, to))
    )


@app.post("/api/library/remap-paths", response_model=RemapResult)
def remap_paths(body: PathMappingInfo) -> RemapResult:
    """Write-back: permanently rewrite matching track LOCATIONs (and the
    playlist references that join to them) to the `to` prefix, then save (a
    version-history commit is written). Destructive and OS-specific — the UI
    warns accordingly."""
    mapping = PathMapping.make(body.from_, body.to)
    if mapping.empty:
        raise HTTPException(400, "Both a `from` and `to` prefix are required")
    adapter = require_adapter()
    # A Traktor track id IS its location, so a remap renames every matching id.
    # Follow them, or every export set referencing a remapped track would
    # dangle — and dangle SILENTLY, still looking curated while quietly
    # exporting fewer tracks than it says. The adapter returns the renames
    # itself: rebuilding them here from display paths (which omit the volume)
    # could pair up two tracks with the same folder on different drives.
    moved = adapter.remap_locations(mapping)
    if not moved:
        return RemapResult(rewritten=0, commit=None)
    if moved and STATE.library_id:
        exports.retarget(STATE.library_id, moved)
    # Through AppState, not the adapter: this is the one route besides /api/save
    # that writes, and skipping it would leave a gap in the version history.
    _outcome, commit = STATE.save()
    return RemapResult(rewritten=len(moved), commit=commit)


@app.get("/api/prefs")
def get_prefs() -> dict:
    """The full persisted user-preferences blob (UI layout, last collection…)."""
    return prefs.load_prefs()


@app.patch("/api/prefs")
def patch_prefs(patch: dict) -> dict:
    """Shallow-merge the given keys into the stored prefs; returns the result."""
    return prefs.update_prefs(patch)


@app.get("/api/fs/places", response_model=list[FsPlace])
def fs_places(platform: str | None = None) -> list[FsPlace]:
    """Sidebar shortcuts for the file browser: the user's folders, then drives.

    Answered by the server because only it can stat a filesystem, and re-asked
    by the client rather than cached: drives come and go while a dialog is open,
    and a browser that exists to find a stick you just plugged in has to show it.

    `platform` adds that platform's own default location, which is the likeliest
    destination when browsing for one of its libraries. Only for platforms whose
    library sits at a fixed path — a removable one's libraries ARE the drives,
    which are already listed.
    """
    out = [FsPlace(**p) for p in places.user_places()]
    out += [FsPlace(**p) for p in places.volume_places()]
    if platform:
        try:
            driver = registry.driver_by_platform(platform)
        except LibraryNotSupported:
            driver = None
        if driver is not None and not getattr(driver, "removable", False):
            seen = {p.path for p in out}
            try:
                found = driver.detect()
            except OSError:
                found = []
            for candidate in found:
                # The place is the folder CONTAINING the library, because a
                # place is somewhere you navigate to, not something you pick.
                folder = Path(candidate["path"]).parent
                if str(folder) in seen or not folder.is_dir():
                    continue
                seen.add(str(folder))
                out.append(
                    FsPlace(kind="library", name=candidate.get("label") or folder.name,
                            path=str(folder))
                )
    return out


@app.get("/api/fs/list", response_model=FsListing)
def fs_list(path: str | None = None, platform: str | None = None) -> FsListing:
    """List directories and openable library files for the in-app file browser.

    `platform` narrows the files to that platform's own: a Traktor browse that
    also listed `master.db` would be offering a file the chosen adapter cannot
    open. A platform whose library is a directory contributes no suffixes at
    all, so its browse shows folders only.
    """
    base = Path(path).expanduser() if path else Path.home()
    if not base.exists() or not base.is_dir():
        base = Path.home()
    base = base.resolve()
    suffixes = registry.browsable_suffixes(platform)
    dirs: list[FsEntry] = []
    files: list[FsEntry] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append(FsEntry(name=entry.name, path=str(entry)))
                elif entry.suffix.lower() in suffixes:
                    files.append(FsEntry(name=entry.name, path=str(entry)))
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(base.parent) if base.parent != base else None
    return FsListing(
        path=str(base), parent=parent, home=str(Path.home()), dirs=dirs, files=files
    )


# ---- export sets -------------------------------------------------------------
#
# Curation, not library data: a set is Konduktor's own record of what to export,
# where to, and for which platform. Every route is scoped to the LOADED library
# by its stable id, so opening a different collection shows a different shelf and
# moving a collection does not orphan the sets built against it.


def _require_library_id() -> str:
    require_adapter()
    if STATE.library_id is None:  # pragma: no cover — loaded implies an id
        raise HTTPException(409, "No library is loaded")
    return STATE.library_id


def _require_set(set_id: str):
    found = exports.get(_require_library_id(), set_id)
    if found is None:
        raise HTTPException(404, f"No export called {set_id}")
    return found


@app.get("/api/exports", response_model=list[ExportSetOut])
def list_exports() -> list[ExportSetOut]:
    return [ExportSetOut(**s.as_dict()) for s in exports.all_sets(_require_library_id())]


@app.post("/api/exports", response_model=ExportSetOut)
def create_export(body: CreateExportSet) -> ExportSetOut:
    if not body.destination.strip():
        raise HTTPException(400, "An export needs a destination folder")
    # Only platforms that can actually be WRITTEN may be targets. Gating here as
    # well as in the UI, because a set persists: one created against a target
    # that later stops being supported would otherwise fail at export time.
    if body.target not in _export_targets():
        raise HTTPException(422, f"Konduktor cannot export to {body.target} yet")
    created = exports.create(
        _require_library_id(),
        name=body.name,
        target=body.target,
        destination=body.destination,
    )
    return ExportSetOut(**created.as_dict())


@app.patch("/api/exports/{set_id}", response_model=ExportSetOut)
def update_export(set_id: str, body: UpdateExportSet) -> ExportSetOut:
    _require_set(set_id)
    if body.target is not None and body.target not in _export_targets():
        raise HTTPException(422, f"Konduktor cannot export to {body.target} yet")
    updated = exports.update(
        _require_library_id(),
        set_id,
        name=body.name,
        target=body.target,
        destination=body.destination,
    )
    return ExportSetOut(**updated.as_dict())


@app.delete("/api/exports/{set_id}")
def delete_export(set_id: str) -> dict:
    """Forget a set. Deliberately does NOT touch anything at its destination."""
    _require_set(set_id)
    return {"deleted": exports.delete(_require_library_id(), set_id)}


@app.post("/api/exports/{set_id}/add", response_model=ExportSetOut)
def add_to_export(set_id: str, body: ExportSetMembers) -> ExportSetOut:
    _require_set(set_id)
    updated = exports.add(
        _require_library_id(), set_id,
        track_ids=body.track_ids, playlist_ids=body.playlist_ids,
    )
    return ExportSetOut(**updated.as_dict())


@app.post("/api/exports/{set_id}/remove", response_model=ExportSetOut)
def remove_from_export(set_id: str, body: ExportSetMembers) -> ExportSetOut:
    """Remove loose tracks and/or WHOLE playlists.

    There is no per-track removal inside a referenced playlist: the reference is
    live, so honouring both would need an exclusion list — hidden state silently
    deciding what a future export contains. For a subset, add the tracks instead.
    """
    _require_set(set_id)
    updated = exports.remove(
        _require_library_id(), set_id,
        track_ids=body.track_ids, playlist_ids=body.playlist_ids,
    )
    return ExportSetOut(**updated.as_dict())


@app.get("/api/exports/{set_id}/contents", response_model=ExportContents)
def export_contents(set_id: str) -> ExportContents:
    """What the set holds right now. Computed every call, never cached."""
    found = _require_set(set_id)
    resolved = exports.resolve(require_adapter(), found)
    return ExportContents(
        playlists=[
            ExportPlaylistOut(id=p.id, name=p.name, missing=p.missing, count=len(p.track_ids))
            for p in resolved.playlists
        ],
        loose=len(resolved.loose_track_ids),
        tracks=len(resolved.track_ids),
        dangling=resolved.dangling_track_ids,
        destination_conflict=exports.destination_conflict(
            _require_library_id(), found.destination, ignore=set_id
        ),
    )


@app.get("/api/exports/{set_id}/tracks", response_model=list[Track])
def export_tracks(set_id: str) -> list[Track]:
    """Every track the export would carry, deduped — the set's root view."""
    found = _require_set(set_id)
    adapter = require_adapter()
    resolved = exports.resolve(adapter, found)
    return [t for t in (adapter.track(tid) for tid in resolved.track_ids) if t is not None]


@app.get("/api/exports/{set_id}/loose", response_model=list[Track])
def export_loose_tracks(set_id: str) -> list[Track]:
    """Only the LOOSE tracks — the ones added individually.

    A separate route rather than a filter on the root view, because the two are
    genuinely different questions: the root is everything this export would ship
    (playlists included, deduped), and this is the "Other" bucket that becomes
    its own playlist in the exported library. A track added loosely AND supplied
    by a referenced playlist appears in both, which is honest — it is in the
    loose list, and that is what the count beside "Other" reports.
    """
    found = _require_set(set_id)
    adapter = require_adapter()
    resolved = exports.resolve(adapter, found)
    return [
        t for t in (adapter.track(tid) for tid in resolved.loose_track_ids) if t is not None
    ]


@app.get("/api/exports/{set_id}/playlists/{playlist_id}/tracks", response_model=list[Track])
def export_playlist_tracks(set_id: str, playlist_id: str) -> list[Track]:
    """One included playlist, as it stands now.

    Read straight from the library rather than from the set, which is the live
    reference doing its job: what ships is whatever the playlist holds today.
    """
    found = _require_set(set_id)
    if playlist_id not in found.playlist_ids:
        raise HTTPException(404, "That playlist is not in this export")
    tracks = require_adapter().playlist_tracks(playlist_id)
    if tracks is None:
        raise HTTPException(404, "That playlist no longer exists")
    return tracks


@app.post("/api/exports/{set_id}/preview", response_model=ExportPreview)
def preview_export(set_id: str) -> ExportPreview:
    """What this export would do, right now. Never cached — the set is live."""
    found = _require_set(set_id)
    built = exporter.plan(require_adapter(), found)
    return ExportPreview(**built.as_dict(exporter.space_for(built)))


@app.post("/api/exports/{set_id}/run", response_model=JobStatus)
def run_export(set_id: str) -> JobStatus:
    """Start the export on a background thread; poll `/api/jobs/{id}`.

    Planned again HERE rather than trusting whatever the preview showed: a
    playlist can be edited, a file deleted or a drive unplugged between looking
    and clicking, and the plan carries the destination-safety check.
    """
    found = _require_set(set_id)
    adapter = require_adapter()
    built = exporter.plan(adapter, found)
    if built.blocked:
        raise HTTPException(409, built.blocked)
    job = JOBS.submit("export", lambda handle: exporter.run(adapter, found, built, handle))
    return JobStatus(**job.as_dict())


def _export_targets() -> set[str]:
    """Platforms Konduktor can write a NEW library for.

    Asked of the exporter registry rather than hard-coded: reading a library and
    creating one from nothing are different capabilities, and a platform can
    have the first without the second. Registering an exporter is what makes a
    platform a target, so this cannot drift from what actually exists.
    """
    return core_export.targets()


@app.get("/api/export-targets", response_model=list[PlatformOption])
def export_targets() -> list[PlatformOption]:
    """Every platform, flagged by whether it can be an export TARGET.

    Returns the unsupported ones too, with `installed=False` standing for "not a
    target yet", so the UI can show them disabled with a reason. A silently
    absent option reads as a missing feature; a disabled one reads as a roadmap.
    """
    supported = _export_targets()
    return [
        PlatformOption(
            platform=d.platform,
            name=d.display_name,
            library_label=getattr(d, "library_label", ""),
            selects="directory",   # an export destination is always a folder
            installed=d.platform in supported,
            found=0,
            removable=bool(getattr(d, "removable", False)),
        )
        for d in registry.drivers()
    ]


# ---- health / state ---------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "loaded": STATE.loaded, "path": str(STATE.path) if STATE.path else None}


def _library_info() -> LibraryInfo:
    """Identity of the loaded library, for display and for composing warnings."""
    caps = require_adapter().capabilities()
    path = Path(str(STATE.path))
    # A library's on-screen name is the driver's to give when it cares: a drive
    # has one library reachable by two valid paths, and the filename would name
    # it differently depending on which one the user happened to pick.
    driver = next((d for d in registry.drivers() if d.platform == caps.platform), None)
    namer = getattr(driver, "display_name_for", None)
    try:
        display_name = namer(path) if namer else path.name
    except OSError:
        display_name = path.name
    return LibraryInfo(
        platform=caps.platform,
        name=caps.save.app_name,
        library_label=caps.save.library_label,
        path=str(STATE.path),
        display_name=display_name,
        version=caps.version,
    )


@app.get("/api/platforms", response_model=list[PlatformOption])
def platforms() -> list[PlatformOption]:
    """Every DJ platform Konduktor can open — used by the picker BEFORE a
    library is loaded, where capabilities are not yet available."""
    out = []
    for d in registry.drivers():
        found = []
        try:
            found = d.detect()
        except OSError:
            pass
        out.append(
            PlatformOption(
                platform=d.platform,
                name=d.display_name,
                library_label=getattr(d, "library_label", ""),
                selects=getattr(d, "selects", "file"),
                installed=bool(found),
                found=len(found),
                removable=bool(getattr(d, "removable", False)),
            )
        )
    # Registration order is an import-order accident, and this list is a MENU —
    # it must not reshuffle itself between two launches. Sorted so the platforms
    # that keep a library in a known place come before the ones that are a
    # plugged-in drive, which is the real distinction a chooser is making.
    out.sort(key=lambda o: (o.removable, o.name.lower()))
    return out


@app.get("/api/capabilities", response_model=Capabilities)
def capabilities() -> Capabilities:
    """What the loaded library can persist, so the UI can gate its controls."""
    return require_adapter().capabilities()


@app.get("/api/state", response_model=EditState)
def state() -> EditState:
    return EditState(dirty=require_adapter().dirty, library=_library_info())


@app.post("/api/reload")
def reload_collection() -> dict:
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    STATE.open(STATE.path)  # re-parse current file from disk
    return {"status": "reloaded", "tracks": len(require_adapter().tracks)}


# ---- read: stats / facets / tracks ------------------------------------


@app.get("/api/stats", response_model=Stats)
def stats() -> Stats:
    a = require_adapter()
    return a.stats(playlist_count=a.playlist_count())


@app.get("/api/facets", response_model=Facets)
def facets() -> Facets:
    return require_adapter().facets()


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
    return require_adapter().query_tracks(
        q=q, genre=genre, key=key, bpm_min=bpm_min, bpm_max=bpm_max,
        rating_min=rating_min, has_cues=has_cues, sort=sort, order=order,
        limit=limit, offset=offset,
    )


# ---- read: playlists --------------------------------------------------


@app.get("/api/playlists", response_model=list[PlaylistNode])
def playlists() -> list[PlaylistNode]:
    return require_adapter().playlist_tree()


@app.get("/api/playlists/{playlist_id}/tracks", response_model=list[Track])
def playlist_tracks(playlist_id: str) -> list[Track]:
    tracks = require_adapter().playlist_tracks(playlist_id)
    if tracks is None:
        raise HTTPException(404, f"Playlist not found: {playlist_id}")
    return tracks


# ---- write: playlist editing ------------------------------------------


@app.post("/api/playlists", response_model=PlaylistNode)
def create_playlist(body: CreatePlaylist) -> PlaylistNode:
    new_id = require_adapter().create_playlist(
        body.name.strip() or "New Playlist", body.parent_id
    )
    return PlaylistNode(
        id=new_id,
        name=body.name,
        kind="playlist",
        count=0,
        selectable=True,
        can_add_tracks=True,
        can_reorder=True,
        can_rename=True,
        can_delete=True,
    )


@app.post("/api/playlists/folders", response_model=PlaylistNode)
def create_folder(body: CreatePlaylist) -> PlaylistNode:
    name = body.name.strip() or "New Folder"
    new_id = require_adapter().create_folder(name, body.parent_id)
    return PlaylistNode(
        id=new_id,
        name=name,
        kind="folder",
        can_contain_children=True,
        can_rename=True,
        can_delete=True,
    )


# `:path` for the same reason as delete: a folder id is a path of names.
@app.patch("/api/playlists/{playlist_uuid:path}")
def rename_playlist(playlist_uuid: str, body: RenamePlaylist) -> dict:
    require_adapter().rename_playlist(playlist_uuid, body.name.strip())
    return {"status": "renamed", "id": playlist_uuid, "name": body.name}


# `:path` because a folder id is a path of names (`fld:a/b`), and the slash
# would otherwise end the segment.
@app.delete("/api/playlists/{playlist_uuid:path}")
def delete_playlist(playlist_uuid: str) -> dict:
    require_adapter().delete_playlist(playlist_uuid)
    return {"status": "deleted", "id": playlist_uuid}


@app.put("/api/playlists/{playlist_uuid}/entries")
def set_entries(playlist_uuid: str, body: SetEntries) -> dict:
    n = require_adapter().set_playlist_entries(playlist_uuid, body.track_ids)
    return {"status": "updated", "id": playlist_uuid, "count": n}


@app.post("/api/playlists/{playlist_uuid}/add")
def add_entries(playlist_uuid: str, body: SetEntries) -> dict:
    """Append tracks to a playlist (skips ids already present)."""
    a = require_adapter()
    current = a.playlist_entries(playlist_uuid)
    if current is None:
        raise HTTPException(404, f"Playlist not found: {playlist_uuid}")
    have = set(current)
    added = [tid for tid in body.track_ids if tid not in have]
    n = a.set_playlist_entries(playlist_uuid, current + added)
    return {"status": "added", "id": playlist_uuid, "added": len(added), "count": n}


@app.patch("/api/tracks")
def edit_track(body: EditTrack) -> dict:
    """Edit a single track's safe metadata fields (in-memory; persisted on save)."""
    require_adapter().set_track_metadata(body.track_id, body.fields)
    return {"status": "updated", "id": body.track_id}


@app.get("/api/tracks/art")
def track_art(track_id: str) -> Response:
    """Stream a track's embedded cover art (staged replacement if unsaved)."""
    art = require_adapter().cover_art(track_id)
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
    path = require_adapter().audio_path(track_id)
    if path is None:
        raise HTTPException(404, "Track not found")
    if not path.exists():
        raise HTTPException(404, f"Audio file not found: {path}")
    # .stem.m4a and other MP4s serve as audio/mp4; browsers play the first track.
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    # Cacheable so the waveform's decode-fetch and the <audio> element can share
    # the download (FileResponse adds ETag/Last-Modified for revalidation).
    return FileResponse(path, media_type=mime)


def _cues_for(track_id: str) -> TrackCues:
    cues = require_adapter().track_cues(track_id)
    if cues is None:
        raise HTTPException(404, "Track not found")
    return cues


@app.get("/api/tracks/cues", response_model=TrackCues)
def track_cues(track_id: str) -> TrackCues:
    """Beatgrid + cue/loop markers for a track (read-only, from the NML)."""
    return _cues_for(track_id)


@app.post("/api/tracks/cue", response_model=TrackCues)
def create_cue(body: SetCue) -> TrackCues:
    """Create (or reposition + retype) the cue in a slot at a position."""
    return require_adapter().set_cue(
        body.track_id,
        slot=body.slot,
        start_sec=body.start,
        cue_type=body.type,
        role=body.role,
        length_sec=body.length,
        name=body.name,
    )


@app.post("/api/tracks/cue/auto", response_model=AutoHotcuesResult)
def auto_hotcues(body: AutoHotcuesRequest) -> AutoHotcuesResult:
    """Analyse the track's structure on its beatgrid and place the template.

    Each requested slot is bound to an event (``drop_1``, ``outro``, …) plus an
    offset in beats. Every slot reports an outcome — placed, not found, out of
    range, occupied (a cue is there and overwrite was not asked for), protected
    (a cue the adapter will not replace) or duplicate (that beat already has a
    cue — a kept one, or a new one in a lower slot) — so the UI can say which events this track does not have.
    Requires a beatgrid.
    """
    a = require_adapter()
    cues = a.track_cues(body.track_id)
    if cues is None:
        raise HTTPException(404, "Track not found")
    if not cues.grid_markers:
        raise HTTPException(400, "Set a beatgrid before using Auto Hotcues")
    _check_cue_slots(a, body.slots)
    try:
        return _place_auto_cues(a, body.track_id, cues, body.slots)
    except _AnalysisError as ex:
        raise HTTPException(400, str(ex))


def _check_cue_slots(a, slots: list) -> None:
    """Refuse a template the bank cannot hold — once, before any track is touched."""
    n = a.capabilities().cues.hotcue_slots
    seen: set[int] = set()
    for r in slots:
        if not 0 <= r.slot < n:
            raise HTTPException(400, f"Slot {r.slot} is outside this library's {n} hotcues")
        if r.slot in seen:
            raise HTTPException(400, f"Slot {r.slot} is requested twice")
        seen.add(r.slot)


def _place_auto_cues(a, track_id: str, cues: TrackCues, slots: list) -> AutoHotcuesResult:
    """Analyse one gridded track's structure and place the template — shared by
    the deck's Auto button and the batch job, so the two cannot drift."""
    path = a.audio_path(track_id)
    if path is None or not path.exists():
        raise _AnalysisError("Audio file not found (is the drive mounted?)")
    try:
        found = structure.analyse(str(path), [(m.start, m.bpm) for m in cues.grid_markers])
    except Exception as ex:  # analysis is best-effort; never 500 the UI
        raise _AnalysisError(f"Analysis failed: {ex}") from ex

    existing = {
        c.slot: ah.ExistingCue(c.editable, c.start)
        for c in cues.cues
        if c.role == "hotcue" and c.slot is not None
    }
    outcomes = ah.plan(
        found,
        [ah.SlotRequest(r.slot, r.event, r.offset_beats, r.overwrite) for r in slots],
        existing,
    )
    placed = [o for o in outcomes if o.status == ah.PLACED]
    if placed:
        # overwrite=True is safe: `plan` only passes an occupied slot when the
        # user ticked it, and the adapter still refuses a protected one.
        cues = a.place_cues(
            track_id,
            [AutoHotcue(slot=o.slot, start=o.start, name=o.name) for o in placed],
            overwrite=True,
        )
    return AutoHotcuesResult(
        cues=cues,
        outcomes=[AutoCueOutcome(**vars(o)) for o in outcomes],
    )


@app.post("/api/tracks/grid/auto", response_model=TrackCues)
def auto_grid(body: AutoGridRequest) -> TrackCues:
    """Detect tempo + first beat and build a beatgrid (see `core/grid_detect`).

    One constant-tempo marker, written the way the platform's own analyser would
    (on Traktor that also sets hotcue 1 to the first beat). Octave (half/double)
    ambiguity in halftime genres is left for the ×2 / ÷2 controls."""
    a = require_adapter()
    if a.track(body.track_id) is None:
        raise HTTPException(404, "Track not found")
    try:
        return _analyse_grid(a, body.track_id)
    except _AnalysisError as ex:
        raise HTTPException(400, str(ex))


class _AnalysisError(Exception):
    """A track that could not be analysed; the message is user-facing."""


# Batch analysis jobs. ONE at a time across both kinds: a grid run replacing a
# track's grid while a hotcue run places cues on the old one would put cues on
# beats that no longer exist.
GRID_JOB = "grid-analysis"
CUE_JOB = "auto-hotcues"
BATCH_JOBS = (GRID_JOB, CUE_JOB)


def _require_no_batch() -> None:
    if any(JOBS.active(k) for k in BATCH_JOBS):
        raise HTTPException(409, "A batch analysis is already running")


def _analyse_grid(a, track_id: str) -> TrackCues:
    """Detect and write one track's grid — shared by the deck's Analyze and the
    batch job, so the two cannot come to mean different things."""
    path = a.audio_path(track_id)
    if path is None or not path.exists():
        raise _AnalysisError("Audio file not found (is the drive mounted?)")
    try:
        found = grid_detect.detect_grid(str(path))
    except Exception as ex:  # analysis is best-effort; never 500 the UI
        raise _AnalysisError(f"Analysis failed: {ex}") from ex
    # "Analysed", not "replace": each platform writes an analysis result in its
    # own shape (Traktor pairs the first marker with a beat-1 cue).
    return a.set_analysed_grid(track_id, [(found.anchor, found.bpm)])



@app.post("/api/tracks/grid/auto-batch", response_model=JobStatus)
def auto_grid_batch(body: AutoGridBatchRequest) -> JobStatus:
    """Analyse many tracks' grids as a JOB; poll `/api/jobs/{id}`.

    Each track is written as it finishes, into the in-memory model like any
    other edit — so a cancel keeps what is done (it is all unsaved until Save),
    and the result is returned even from a cancelled run. A track that fails is
    reported and skipped, never fatal: one unmounted drive must not stop 400
    other tracks."""
    a = require_adapter()
    if not a.capabilities().grid.editable:
        raise HTTPException(422, "This library's beatgrids cannot be edited")
    _require_no_batch()
    ids = list(dict.fromkeys(body.track_ids))

    def run(handle) -> dict:
        result = {"analysed": [], "locked": 0, "existing": 0, "failed": []}
        handle.progress(done=0, total=len(ids))
        for i, track_id in enumerate(ids):
            if handle.cancelled:
                break  # keep what is done; return it rather than raise
            track = a.track(track_id)
            title = (track.title or track_id) if track else track_id
            handle.progress(message=title)
            if track is None:
                result["failed"].append({"title": title, "reason": "Track not found"})
            elif track.grid_locked:
                result["locked"] += 1
            elif track.grid_marker_count > 0 and not body.replace_existing:
                result["existing"] += 1
            else:
                try:
                    _analyse_grid(a, track_id)
                    result["analysed"].append(track_id)
                except (_AnalysisError, AdapterError) as ex:
                    result["failed"].append({"title": title, "reason": str(ex)})
            handle.progress(done=i + 1)
        return result

    job = JOBS.submit(GRID_JOB, run)
    return JobStatus(**job.as_dict())


@app.post("/api/tracks/cue/auto-batch", response_model=JobStatus)
def auto_hotcues_batch(body: AutoHotcuesBatchRequest) -> JobStatus:
    """Place one Auto Hotcues template on many tracks, as a JOB.

    A track with no beatgrid gets one analysed first (as "Analyze Grid & BPM"
    would), since the events are found on the grid; a track that has one keeps
    it untouched. Same contract as the grid batch: written as each track
    finishes, a cancel keeps what is done, a failing track is reported and
    skipped."""
    a = require_adapter()
    caps = a.capabilities()
    if not caps.writable or caps.cues.hotcue_slots == 0:
        raise HTTPException(422, "This library's hotcues cannot be edited")
    _check_cue_slots(a, body.slots)
    if not body.slots:
        raise HTTPException(400, "The template places no cues")
    _require_no_batch()
    ids = list(dict.fromkeys(body.track_ids))

    def run(handle) -> dict:
        result = {"tracks": [], "cues_placed": 0, "grids_created": 0, "failed": []}
        handle.progress(done=0, total=len(ids))
        for i, track_id in enumerate(ids):
            if handle.cancelled:
                break  # keep what is done; return it rather than raise
            track = a.track(track_id)
            title = (track.title or track_id) if track else track_id
            handle.progress(message=title)
            try:
                cues = a.track_cues(track_id) if track else None
                if cues is None:
                    raise _AnalysisError("Track not found")
                if not cues.grid_markers:
                    if track.grid_locked:
                        raise _AnalysisError("No beatgrid, and the grid is locked")
                    if not caps.grid.editable:
                        raise _AnalysisError("No beatgrid")
                    cues = _analyse_grid(a, track_id)
                    result["grids_created"] += 1
                placed = _place_auto_cues(a, track_id, cues, body.slots)
                result["tracks"].append(track_id)
                result["cues_placed"] += sum(o.status == ah.PLACED for o in placed.outcomes)
            except (_AnalysisError, AdapterError) as ex:
                result["failed"].append({"title": title, "reason": str(ex)})
            handle.progress(done=i + 1)
        return result

    job = JOBS.submit(CUE_JOB, run)
    return JobStatus(**job.as_dict())


@app.patch("/api/tracks/cue", response_model=TrackCues)
def edit_cue_type(body: SetCueType) -> TrackCues:
    """Change the type of an existing cue (keeps its position)."""
    return require_adapter().set_cue_type(body.track_id, body.slot, body.type)


@app.delete("/api/tracks/cue", response_model=TrackCues)
def delete_cue(track_id: str, slot: int) -> TrackCues:
    """Remove the hotcue in a slot."""
    return require_adapter().delete_cue(track_id, slot)


@app.post("/api/tracks/grid/marker", response_model=TrackCues)
def add_grid_marker(body: AddGridMarker) -> TrackCues:
    """Add a beatgrid marker. Omitting `bpm` inherits the governing tempo."""
    return require_adapter().add_grid_marker(body.track_id, body.start, body.bpm)


@app.patch("/api/tracks/grid/marker", response_model=TrackCues)
def edit_grid_marker(body: GridMarkerEdit) -> TrackCues:
    """Retempo and/or move one grid marker.

    BPM is applied before the move: a tempo change can never reorder markers, so
    `index` is still valid for the move afterwards.
    """
    a = require_adapter()
    cues = a.track_cues(body.track_id)
    if body.bpm is not None:
        cues = a.set_grid_marker_bpm(body.track_id, body.index, body.bpm)
    if body.start is not None:
        cues = a.move_grid_marker(body.track_id, body.index, body.start)
    if cues is None:
        raise HTTPException(404, "Track not found")
    return cues


@app.delete("/api/tracks/grid/marker", response_model=TrackCues)
def remove_grid_marker(track_id: str, index: int) -> TrackCues:
    """Delete one grid marker (and its companion cue)."""
    return require_adapter().delete_grid_marker(track_id, index)


@app.put("/api/tracks/grid", response_model=TrackCues)
def replace_grid(body: ReplaceGrid) -> TrackCues:
    """Replace the whole beatgrid (the deck's Reset). `markers: []` clears it."""
    return require_adapter().replace_grid(
        body.track_id, [(m.start, m.bpm) for m in body.markers]
    )


@app.delete("/api/tracks/grid", response_model=TrackCues)
def remove_grid(track_id: str) -> TrackCues:
    """Delete every grid marker and its companion cue. <TEMPO> is kept."""
    return require_adapter().delete_grid(track_id)


# ---- bulk Remove (the context menu's Remove ▸ submenu) --------------------
#
# Quick in-memory edits, so requests rather than jobs — but refused while a
# batch analysis runs: it would write cues/grids onto a track removed under it.


@app.post("/api/tracks/grid/clear")
def clear_grids(body: TrackIds) -> dict:
    """Delete the beatgrid of every given track. Locked grids are skipped —
    a lock is the user saying "leave this grid alone" — and reported."""
    a = require_adapter()
    if not a.capabilities().grid.editable:
        raise HTTPException(422, "This library's beatgrids cannot be edited")
    _require_no_batch()
    out = {"cleared": 0, "locked": 0, "empty": 0}
    for tid in dict.fromkeys(body.track_ids):
        track = a.track(tid)
        if track is None:
            continue
        if track.grid_locked:
            out["locked"] += 1
        elif not (a.track_cues(tid) or TrackCues()).grid_markers:
            out["empty"] += 1  # counted from the cues: the projection's count can be approximate
        else:
            a.delete_grid(tid)
            out["cleared"] += 1
    return out


@app.post("/api/tracks/cue/clear")
def clear_hotcues(body: TrackIds) -> dict:
    """Empty the whole hotcue bank of every given track — point cues, loops
    and the grid's paired beat-1 cue alike (the grid itself is untouched).
    Cues the adapter will not edit — Rekordbox memory cues — are kept."""
    a = require_adapter()
    caps = a.capabilities()
    if not caps.writable or caps.cues.hotcue_slots == 0:
        raise HTTPException(422, "This library's hotcues cannot be edited")
    _require_no_batch()
    out = {"tracks": 0, "cues": 0}
    for tid in dict.fromkeys(body.track_ids):
        cues = a.track_cues(tid)
        slots = sorted({
            c.slot for c in (cues.cues if cues else [])
            if c.role == "hotcue" and c.slot is not None and c.editable
        })
        for slot in slots:
            a.delete_cue(tid, slot)
        if slots:
            out["tracks"] += 1
            out["cues"] += len(slots)
    return out


@app.post("/api/tracks/remove")
def remove_tracks(body: TrackIds) -> dict:
    """Remove tracks from the library and every playlist. Audio files are
    never touched. An export set referencing one will report it as missing,
    which is what export sets do with any track that has gone."""
    a = require_adapter()
    if not a.capabilities().tracks.removable:
        raise HTTPException(422, "Tracks cannot be removed from this library")
    _require_no_batch()
    return {"removed": a.remove_tracks(list(dict.fromkeys(body.track_ids)))}


@app.patch("/api/tracks/grid/lock", response_model=TrackCues)
def edit_grid_lock(body: SetGridLock) -> TrackCues:
    """Toggle Traktor's LOCK flag on a track."""
    return require_adapter().set_grid_lock(body.track_id, body.locked)


@app.put("/api/tracks/art")
async def set_track_art(track_id: str = Form(...), file: UploadFile = File(...)) -> dict:
    """Stage replacement cover art for a track (written to the file on save)."""
    a = require_adapter()
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty image")
    a.set_cover_art(track_id, data, file.content_type or "image/jpeg")
    return {"status": "staged", "id": track_id, "bytes": len(data)}


@app.post("/api/save", response_model=SaveResult)
def save() -> SaveResult:
    a = require_adapter()
    if not a.dirty:
        return SaveResult(saved=False, commit=None, playlists=a.playlist_count())
    outcome, commit = STATE.save()
    return SaveResult(
        saved=True,
        commit=commit,
        playlists=a.playlist_count(),
        file_tags=[FileTagOutcome(**r.__dict__) for r in outcome.tag_results],
    )


# ---- version history --------------------------------------------------


@app.get("/api/history", response_model=list[HistoryEntry])
def get_history() -> list[HistoryEntry]:
    """All saved versions of the current collection, newest first."""
    require_adapter()
    return [HistoryEntry(**e.__dict__) for e in history.list_history(STATE.path)]


@app.post("/api/history/{commit_id}/restore", response_model=CollectionStatus)
def restore_version(commit_id: str) -> CollectionStatus:
    """Restore the collection to a past version. Writes that version back as a
    NEW forward save (a fresh commit on top of history — never a rewind), then
    reloads. The user should close Traktor first (it overwrites on exit)."""
    require_adapter()
    data = history.read_version(STATE.path, commit_id)
    if data is None:
        raise HTTPException(404, f"Version not found: {commit_id}")
    path = STATE.path
    # The adapter's driver owns writing its own format back, even for a restore.
    registry.driver_for(path).restore(path, data)
    history.commit(path, data, f"Restored version {commit_id[:8]}", __version__)
    STATE.open(path)  # rebuild read + edit models from the restored file
    return collection_status()


@app.delete("/api/history")
def clear_history() -> dict:
    """Permanently delete ALL version history for the current collection."""
    require_adapter()
    history.clear_history(STATE.path)
    return {"status": "cleared"}


# ---- import sources ---------------------------------------------------
#
# A SOURCE is a library being read FROM, open alongside the loaded one. Its
# routes are read-only by construction: there is no source equivalent of any
# command, so no route here can be confused about which library it targets.


def require_source() -> LibraryAdapter:
    """The open source, or a 409."""
    if not STATE.source_loaded:
        raise HTTPException(409, "No source library open")
    assert STATE.source is not None
    return STATE.source


def _source_status() -> SourceStatus:
    if not STATE.source_loaded:
        return SourceStatus(loaded=False)
    source = STATE.source
    caps = source.capabilities()
    return SourceStatus(
        loaded=True,
        path=str(STATE.source_path),
        label=caps.save.library_label,
        platform=caps.platform,
        tracks=len(source.tracks),
        playlists=source.playlist_count(),
    )


@app.get("/api/sources", response_model=list[SourceCandidate])
def sources() -> list[SourceCandidate]:
    """Every removable library plugged in right now.

    Unlike `/api/library/options`, this genuinely changes between two calls a
    second apart — a stick is whatever is mounted — so the UI is expected to
    poll it rather than read it once at startup.

    Only read-only platforms are offered. A source is a thing to import FROM,
    and a writable library appearing here would invite someone to open their own
    collection as a source of itself.
    """
    out: list[SourceCandidate] = []
    for driver in registry.drivers():
        try:
            found = driver.detect()
        except OSError:
            continue
        for candidate in found:
            path = Path(candidate["path"])
            # Removable libraries are the ones discovery finds by scanning mount
            # points. Ask the driver rather than hard-coding which platform that
            # is, so a future Serato-on-a-stick needs no change here.
            if not getattr(driver, "removable", False):
                continue
            out.append(
                SourceCandidate(
                    path=str(path),
                    label=candidate.get("label") or path.name,
                    platform=driver.platform,
                    modified=candidate.get("modified"),
                )
            )
    return out


@app.get("/api/source", response_model=SourceStatus)
def source_status() -> SourceStatus:
    return _source_status()


@app.post("/api/source/open", response_model=SourceStatus)
def open_source(body: OpenSource) -> SourceStatus:
    path = Path(body.path).expanduser()
    # NOT `is_file()`, unlike opening a collection: a OneLibrary source is a
    # DRIVE, so the thing the user picks is a directory.
    if not path.exists():
        raise HTTPException(400, f"Not found: {path}")
    try:
        STATE.open_source(path)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except AdapterError as ex:
        raise HTTPException(400, f"Not a library Konduktor can read: {ex}")
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, f"Could not open that source: {ex}")
    return _source_status()


@app.delete("/api/source", response_model=SourceStatus)
def close_source() -> SourceStatus:
    """Close the source and release its file handle.

    Worth calling rather than leaving to chance: the source is on a removable
    drive, and a held handle is what stops a stick ejecting.
    """
    STATE.close_source()
    return _source_status()


@app.get("/api/source/capabilities", response_model=Capabilities)
def source_capabilities() -> Capabilities:
    return require_source().capabilities()


@app.get("/api/source/tracks", response_model=TrackPage)
def source_tracks(
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
    return require_source().query_tracks(
        q=q, genre=genre, key=key, bpm_min=bpm_min, bpm_max=bpm_max,
        rating_min=rating_min, has_cues=has_cues, sort=sort, order=order,
        limit=limit, offset=offset,
    )


@app.get("/api/source/playlists", response_model=list[PlaylistNode])
def source_playlists() -> list[PlaylistNode]:
    return require_source().playlist_tree()


@app.get("/api/source/playlists/{playlist_id}/tracks", response_model=list[Track])
def source_playlist_tracks(playlist_id: str) -> list[Track]:
    tracks = require_source().playlist_tracks(playlist_id)
    if tracks is None:
        raise HTTPException(404, f"Playlist not found: {playlist_id}")
    return tracks


@app.get("/api/source/tracks/cues", response_model=TrackCues)
def source_track_cues(track_id: str) -> TrackCues:
    cues = require_source().track_cues(track_id)
    if cues is None:
        raise HTTPException(404, "Track not found")
    return cues


@app.get("/api/source/tracks/audio")
def source_track_audio(track_id: str) -> FileResponse:
    """Stream a track's audio straight off the source drive.

    So the deck can audition a track BEFORE importing it, which is most of the
    reason the stick is browsable in the ordinary table rather than in a picker
    dialog.
    """
    path = require_source().audio_path(track_id)
    if path is None:
        raise HTTPException(404, "Track not found")
    if not path.exists():
        raise HTTPException(404, f"Audio file not found: {path}")
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)


# ---- import -----------------------------------------------------------
#
# The only operation in the app that can run for minutes, so it is the only one
# that is a JOB rather than a request. See `jobs.py` for why that is polling and
# threads rather than something fancier.


def _import_plan(body: ImportRequest):
    source, dest = require_source(), require_adapter()
    destination = Path(body.destination).expanduser()
    return source, dest, destination, importer.plan(
        source,
        dest,
        destination,
        track_ids=body.track_ids,
        playlist_ids=body.playlist_ids,
    )


@app.post("/api/import/preview")
def import_preview(body: ImportRequest) -> dict:
    """What an import would do: counts, size, missing files, duplicates, space.

    Computed fresh on every call and never stored — a stick can be re-exported
    between the preview and the import, and a stale preview is worse than none.
    """
    _source, _dest, destination, plan = _import_plan(body)
    return plan.as_dict(free_bytes=importer.free_bytes(destination))


@app.post("/api/import", response_model=JobStatus)
def start_import(body: ImportRequest) -> JobStatus:
    """Start an import. Returns immediately; poll the job for progress."""
    source, dest, destination, plan = _import_plan(body)
    if not plan.importable:
        raise HTTPException(400, "Nothing to import (no tracks, or none of their files exist)")
    free = importer.free_bytes(destination)
    if free is not None and free < plan.total_bytes + importer.SPACE_HEADROOM:
        raise HTTPException(
            400,
            f"Not enough space: {plan.total_bytes / 1e9:.1f} GB needed, "
            f"{free / 1e9:.1f} GB free at {destination}",
        )
    # One at a time: two concurrent imports would race on filename collisions and
    # on the destination library's save.
    if JOBS.active("import"):
        raise HTTPException(409, "An import is already running")

    folder_name = body.folder_name
    if folder_name is None and STATE.source is not None:
        folder_name = STATE.source.capabilities().save.library_label

    job = JOBS.submit(
        "import",
        lambda handle: importer.run(source, dest, plan, handle, folder_name=folder_name),
    )
    return JobStatus(**job.as_dict())


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str) -> JobStatus:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    return JobStatus(**job.as_dict())


@app.post("/api/jobs/{job_id}/cancel", response_model=JobStatus)
def cancel_job(job_id: str) -> JobStatus:
    """Ask a job to stop.

    A REQUEST, not a kill: the job stops at its next checkpoint and cleans up
    after itself, so the status stays "running" until it has actually done so.
    """
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    JOBS.cancel(job_id)
    return JobStatus(**job.as_dict())
