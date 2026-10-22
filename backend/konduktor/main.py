"""Konduktor FastAPI app.

The collection to work on is chosen at runtime (see /api/collection/open) — the
app starts with nothing loaded and the UI gates on that. Setting KONDUKTOR_NML
in the environment auto-loads that file on startup (used by dev and tests).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, exporter, exports, local_push, loudness_store, prefs
from .app_state import STATE
from .core import auto_hotcues as ah
from .core import grid_detect, key_detect, loudness, structure
from .core.grid_plan import GridStep, plan_grid
from . import importer
from .core import export as core_export
from .core import places, registry
from .core.folder import FolderScanner, FolderSource
from .core.adapter import (
    AdapterError,
    InvalidCommand,
    LibraryAdapter,
    LibraryNotSupported,
    NotFound,
    Unavailable,
    Unsupported,
)
from .core.capabilities import Capabilities
from .jobs import JOBS, Job, JobBusy
from .core.pathmap import PathMapping
from .schemas import (
    Relocation,
    RelocationApplied,
    RelocationApply,
    RelocationCandidate,
    RelocationVolume,
    AnalyzePreview,
    AnalyzeRequest,
    LoudnessRequest,
    LoudnessValue,
    LoudnessValues,
    MeasureLoudnessRequest,
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
    SetTrackColor,
    Facets,
    FileTagOutcome,
    CreateExportSet,
    ExportContents,
    ExportPlaylistOut,
    ExportSetMembers,
    ExportSetOut,
    ExportPreview,
    LocalLibrariesOut,
    PushPreview,
    Drive,
    DroppedFiles,
    DroppedPaths,
    FolderAddRequest,
    FolderTracks,
    FsEntry,
    FsListing,
    FsPlace,
    UpdateExportSet,
    RunExport,
    AddGridMarker,
    GridMarkerEdit,
    ReplaceGrid,
    HistoryEntry,
    ImportRequest,
    JobStatus,
    OpenCollection,
    OpenRemote,
    OpenSource,
    RemoteIn,
    PathMappingInfo,
    PlaylistNode,
    PrefixSuggestions,
    RemapPreview,
    RemapResult,
    RenamePlaylist,
    SaveResult,
    AddEntries,
    RemoveEntries,
    MovePlaylist,
    SetEntries,
    SourceCandidate,
    SourceStatus,
    SetCue,
    SetCueColor,
    SetCueType,
    AddMemoryCue,
    RenameMemoryCue,
    SetMemoryCueColor,
    SetMemoryCueType,
    SetGridLock,
    Stats,
    Track,
    TrackCues,
    TrackPage,
    StemEngineInstall,
    StemSideLoad,
    StemConvert,
    NormalizeRequest,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Konduktor API", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # Packaged desktop app: the webview's origin when Tauri loads the built
        # frontend. macOS uses tauri://; Windows/Linux default to
        # https://tauri.localhost, but lib.rs forces the http scheme
        # (use_https_scheme(false)) so the sidecar is not mixed content —
        # without the http origin here, the Windows app reaches no API at all.
        "tauri://localhost",
        "http://tauri.localhost",
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
    }.get(type(exc), 503 if isinstance(exc, Unavailable) else 400)
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
    _stop_batches()
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


# ---- remote libraries ----------------------------------------------------------
# A library held by a Konduktor server (a container on the user's NAS), opened
# from the picker's "Remote" step. The saved remotes are this computer's; the
# password is in the OS keychain (`adapters/remote/config.py`).


def _remote_out(remote) -> dict:
    out = remote.public()
    out["has_password"] = bool(remote_cfg().password(remote.id))
    return out


def remote_cfg():
    from .adapters.remote import config

    return config


def _checked_remote(body: RemoteIn, remote_id: str | None):
    """Build the remote from the form and handshake every address. Refuses —
    nothing saved — when an address answers but refuses (wrong password, a
    version that cannot talk), or when none answers at all; an unreachable
    FALLBACK alone is fine (a VPN address is often unreachable at home)."""
    from .adapters import remote as remote_lib

    config = remote_cfg()
    host, port = config.split_host(body.host)
    fallback, fallback_port = config.split_host(body.fallback_host or "")
    previous = config.get(remote_id) if remote_id else None
    password = body.password if body.password else (config.password(remote_id) if remote_id else None)
    if not body.name.strip() or not host or not body.username.strip():
        raise HTTPException(400, "A name, an address and a username are all needed")
    if not password:
        raise HTTPException(400, "Enter the server's password")
    remote = config.Remote(
        id=previous.id if previous else config.new_id(), name=body.name.strip(), host=host,
        port=body.port or port, fallback_host=fallback or None,
        fallback_port=(body.fallback_port or fallback_port) if fallback else None,
        username=body.username.strip(),
    )
    results = remote_lib.handshake(remote, password)
    refused = [r for r in results if r["kind"] in ("auth", "version", "other")]
    if refused:
        raise HTTPException(400, {"message": refused[0]["error"], "results": results})
    if not any(r["ok"] for r in results):
        raise HTTPException(400, {"message": results[0]["error"], "results": results})
    return remote, password, results


@app.get("/api/remotes")
def list_remotes() -> list[dict]:
    return [_remote_out(r) for r in remote_cfg().remotes()]


@app.post("/api/remotes")
def add_remote(body: RemoteIn) -> dict:
    remote, password, results = _checked_remote(body, None)
    config = remote_cfg()
    config.set_password(remote.id, password)
    config.save(remote)
    return {"remote": _remote_out(remote), "results": results}


@app.patch("/api/remotes/{remote_id}")
def edit_remote(remote_id: str, body: RemoteIn) -> dict:
    config = remote_cfg()
    if config.get(remote_id) is None:
        raise HTTPException(404, "No such remote")
    remote, password, results = _checked_remote(body, remote_id)
    config.set_password(remote.id, password)
    config.save(remote)
    return {"remote": _remote_out(remote), "results": results}


@app.delete("/api/remotes/{remote_id}")
def delete_remote(remote_id: str) -> dict:
    if STATE.remote_config is not None and STATE.remote_config.id == remote_id:
        raise HTTPException(409, "That remote is open — open another library first")
    if not remote_cfg().delete(remote_id):
        raise HTTPException(404, "No such remote")
    return {"status": "deleted"}


def _running_batch() -> str | None:
    """The kind of batch running here, for the heartbeat: another computer
    asking to take over is told what it would cancel."""
    for kind in BATCH_JOBS + ("import", "export", local_push.JOB_KIND):
        if JOBS.active(kind):
            return kind
    return None


def _on_remote_change(state: str) -> None:
    # Taken over: whatever batch is running here is now writing into a library
    # this computer no longer holds — cancel it, as Cancel does (finished
    # tracks stay as the edits the new holder inherits).
    if state == "taken_over":
        import threading

        threading.Thread(target=_stop_batches, name="konduktor-takeover-stop", daemon=True).start()


@app.post("/api/library/open-remote", response_model=CollectionStatus)
def open_remote(body: OpenRemote) -> CollectionStatus:
    """Connect to a saved remote and make its library THE library.

    409 `{code: "in_use", machine, since, batch}` when another computer holds
    it — the picker asks, and retries with `takeover`. On success, the edits
    another computer left unsaved are reported (`pending_edits`) for the user
    to save or discard.
    """
    from .adapters import remote as remote_lib

    remote = remote_cfg().get(body.remote_id)
    if remote is None:
        raise HTTPException(404, "No such remote")
    _stop_batches()
    try:
        opened = remote_lib.open_remote(remote, takeover=body.takeover, batch_probe=_running_batch)
    except remote_lib.InUse as ex:
        raise HTTPException(409, {"code": "in_use", **ex.holder})
    except AdapterError as ex:
        raise HTTPException(400, str(ex))
    STATE.open_remote(opened)
    opened.session.on_change(_on_remote_change)
    status = collection_status()
    status.pending_edits = opened.pending_edits
    return status


def _require_remote():
    adapter = require_adapter()
    if STATE.remote is None:
        raise HTTPException(409, "The open library is not a remote one")
    return adapter


@app.get("/api/library/fs/list", response_model=FsListing)
def library_fs_list(path: str | None = None) -> FsListing:
    """Folders on the machine that HOLDS the library — where an import into a
    remote library lands (`tracks.audio_destination == "library"`). Confined by
    the server to its content folder; `home` is that folder."""
    adapter = _require_remote()
    listing = adapter.client.fs_list(path)
    return FsListing(
        path=listing["path"], parent=listing["parent"], home=listing["root"],
        dirs=[FsEntry(name=e["name"], path=e["path"]) for e in listing["entries"] if e["is_dir"]],
        files=[],
    )


@app.get("/api/remote-cache")
def remote_cache_status() -> dict:
    from .adapters.remote import cache

    return cache.shared().usage()


@app.delete("/api/remote-cache")
def clear_remote_cache() -> dict:
    from .adapters.remote import cache

    return {"freed": cache.shared().clear()}


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
    _require_no_pending("Changing the path mapping")
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
    _require_no_pending("Rewriting paths")
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
    # The files now live under `to`: carry their loudness values across (they
    # are keyed by the path they were measured at). Not an edit, so not saved.
    try:
        STATE.loudness.remap(mapping)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a remap
        log.warning("could not move loudness values for %s", mapping, exc_info=True)
    # Through AppState, not the adapter: this is the one route besides /api/save
    # that writes, and skipping it would leave a gap in the version history.
    _outcome, commit = STATE.save()
    return RemapResult(rewritten=len(moved), commit=commit)


@app.get("/api/library/relocation", response_model=Relocation)
def get_relocation() -> Relocation:
    """The open-time missing-files check: every stored volume in which NO track
    resolves, and where the search found its tracks. Empty once answered, so the
    UI can refetch freely without re-asking.

    Waits briefly for the search, then reports `scanning` so a slow drive
    cannot hold a request open; the UI polls until it is done.
    """
    require_adapter()
    check = STATE.relocation
    if check is None:
        return Relocation()
    if not check.wait(timeout=10):
        return Relocation(scanning=True)
    return Relocation(
        volumes=[
            RelocationVolume(
                label=p.label,
                root=p.root,
                total=p.total,
                status=p.status,
                candidates=[
                    RelocationCandidate.model_validate(
                        {"from": c.mapping.from_prefix, "to": c.mapping.to_prefix, "found": c.found}
                    )
                    for c in p.candidates
                ],
            )
            for p in check.pending
        ]
    )


@app.post("/api/library/relocation", response_model=RelocationApplied)
def answer_relocation(body: RelocationApply) -> RelocationApplied:
    """Answer the check: apply the chosen mappings for THIS SESSION only (no
    mappings = "Not now"). Nothing is written to the library or to prefs, and a
    reopen asks again."""
    _require_no_pending("Relocating files")
    require_adapter()
    check = STATE.relocation
    if check is None:
        raise HTTPException(409, "There is no missing-files check to answer")
    mappings = [PathMapping.make(m.from_, m.to) for m in body.mappings]
    try:
        tracks = check.answer(mappings)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return RelocationApplied(mappings=len(mappings), tracks=tracks)


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
    if body.kind == "folder" and not body.destination.strip():
        raise HTTPException(400, "An export needs a destination folder")
    # Only platforms that can actually be WRITTEN may be targets. Gating here as
    # well as in the UI, because a set persists: one created against a target
    # that later stops being supported would otherwise fail at export time.
    _check_targets(body.targets, kind=body.kind)
    created = exports.create(
        _require_library_id(),
        name=body.name,
        targets=body.targets,
        destination=body.destination if body.kind == "folder" else "",
        kind=body.kind,
        mode=body.mode,
    )
    return ExportSetOut(**created.as_dict())


@app.patch("/api/exports/{set_id}", response_model=ExportSetOut)
def update_export(set_id: str, body: UpdateExportSet) -> ExportSetOut:
    found = _require_set(set_id)
    if body.targets is not None:
        _check_targets(body.targets, kind=found.kind)
    updated = exports.update(
        _require_library_id(),
        set_id,
        name=body.name,
        mode=body.mode,
        targets=body.targets,
        destination=body.destination,
        lock_grids=body.lock_grids,
        normalise=body.normalise,
        auto_gain=body.auto_gain,
        level=body.level.model_dump() if body.level is not None else None,
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
        ) if found.kind == "folder" else None,
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


def _export_options(found, body: RunExport | None) -> dict:
    """The run's options: the body's where given, else the set's own; a level
    the set does not keep comes from the app settings (Normalize's defaults)."""
    normalise = body.normalise if body is not None and body.normalise is not None else found.normalise
    auto_gain = body.auto_gain if body is not None and body.auto_gain is not None else found.auto_gain
    if body is not None and body.level is not None:
        raw = body.level.model_dump()
    elif found.level is not None:
        raw = found.level
    else:
        p = prefs.load_prefs()
        raw = {"measure": p.get("levelMeasure", "lufs"), "target": p.get("levelTarget", -9.0),
               "ceiling": p.get("levelCeiling", -1.0)}
    try:
        level = exporter.Level(measure=raw["measure"] if raw["measure"] in ("lufs", "rms") else "lufs",
                               target=float(raw["target"]), ceiling=float(raw["ceiling"]))
    except (KeyError, TypeError, ValueError):
        level = exporter.Level()
    # Only where some target keeps an auto gain — a stick has none to write.
    writes_gain = any(getattr(core_export.for_platform(t), "writes_auto_gain", False) for t in found.targets)
    return {"store": STATE.loudness, "level": level if normalise else None,
            "auto_gain": bool(auto_gain and writes_gain)}


@app.post("/api/exports/{set_id}/preview", response_model=ExportPreview)
def preview_export(set_id: str, body: RunExport | None = None) -> ExportPreview:
    """What this export would do, right now, with the run dialog's options as
    ticked. Never cached — the set is live."""
    found = _require_folder_set(set_id)
    built = exporter.plan(require_adapter(), found, **_export_options(found, body))
    return ExportPreview(**built.as_dict(exporter.space_for(built)))


@app.post("/api/exports/{set_id}/run", response_model=JobStatus)
def run_export(set_id: str, body: RunExport | None = None) -> JobStatus:
    """Start the export on a background thread; poll `/api/jobs/{id}`.

    Planned again HERE rather than trusting whatever the preview showed: a
    playlist can be edited, a file deleted or a drive unplugged between looking
    and clicking, and the plan carries the destination-safety check.

    The dialog's options arrive with the click and are remembered on the set,
    so the next export of the same stick starts from the same answer.
    """
    found = _require_folder_set(set_id)
    if body is not None:
        changes = {k: v for k, v in (("lock_grids", body.lock_grids), ("normalise", body.normalise),
                                     ("auto_gain", body.auto_gain)) if v is not None}
        if body.level is not None:
            changes["level"] = body.level.model_dump()
        if changes:
            found = exports.update(_require_library_id(), set_id, **changes)
    adapter = require_adapter()
    options = _export_options(found, None)
    built = exporter.plan(adapter, found, **options)
    if built.blocked:
        raise HTTPException(409, built.blocked)
    # A Konduktor setting, read when the export starts.
    root_folder = bool(prefs.load_prefs().get("exportRootFolder", False))
    job = JOBS.submit("export", lambda handle: exporter.run(
        adapter, found, built, handle, root_folder=root_folder, lock_grids=found.lock_grids, **options))
    return JobStatus(**job.as_dict())


# ---- pushing into this computer's DJ apps (`local_push.py`) -----------------------


def _local_level_out(level) -> dict | None:
    return {"measure": level.measure, "target": level.target, "ceiling": level.ceiling} if level else None


@app.get("/api/local-libraries", response_model=LocalLibrariesOut)
def local_libraries() -> LocalLibrariesOut:
    """The DJ apps' libraries on this computer a "computer" set pushes into,
    each with why it cannot be used, plus the shared audio folder and its
    level. Changed through `/api/prefs` (`localLibraries`, `localAudioRoot`,
    `localNormalise`, `localLevel`)."""
    import platformdirs

    return LocalLibrariesOut(
        libraries=[lib.as_dict() for lib in local_push.local_libraries(STATE.path)],
        audio_root=str(local_push.audio_root()),
        audio_root_default=str(Path(platformdirs.user_music_dir()) / "Konduktor"),
        level=_local_level_out(local_push.level()),
    )


def _require_computer_set(set_id: str):
    found = _require_set(set_id)
    if found.kind != "computer":
        raise HTTPException(400, "This set is exported into a folder, not pushed")
    return found


def _push_options(found) -> dict:
    """The push's options: the set's lock and auto gain (as the run dialog
    left them), the shared folder's level (the app setting, not the set's)."""
    return {"store": STATE.loudness, "level": local_push.level(), "auto_gain": bool(found.auto_gain)}


def _remember_run_options(found, body: RunExport | None):
    if body is None:
        return found
    changes = {k: v for k, v in (("lock_grids", body.lock_grids), ("auto_gain", body.auto_gain))
               if v is not None}
    return exports.update(_require_library_id(), found.id, **changes) if changes else found


@app.post("/api/exports/{set_id}/push/preview", response_model=PushPreview)
def preview_push(set_id: str, body: RunExport | None = None) -> PushPreview:
    """What pushing this set would do now, with the dialog's ticks."""
    found = _require_computer_set(set_id)
    if body is not None and body.auto_gain is not None:
        found.auto_gain = body.auto_gain   # a look, not a change: not saved
    built = local_push.plan(require_adapter(), found, open_path=STATE.path, **_push_options(found))
    return PushPreview(**built.as_dict())


@app.post("/api/exports/{set_id}/push", response_model=JobStatus)
def run_push(set_id: str, body: RunExport | None = None) -> JobStatus:
    """Start the push on a background thread; poll `/api/jobs/{id}`. Planned
    again here (a playlist may have changed since the preview, or Traktor been
    started); the dialog's ticks are remembered on the set."""
    found = _remember_run_options(_require_computer_set(set_id), body)
    adapter = require_adapter()
    options = _push_options(found)
    built = local_push.plan(adapter, found, open_path=STATE.path, **options)
    if built.blocked:
        raise HTTPException(409, built.blocked)
    try:
        job = JOBS.submit(local_push.JOB_KIND, lambda handle: local_push.run(
            adapter, found, built, handle, lock_grids=found.lock_grids, **options),
            exclusive_with=(local_push.JOB_KIND,))
    except JobBusy:
        raise HTTPException(409, "A push is already running")
    return JobStatus(**job.as_dict())


def _export_targets() -> set[str]:
    """Platforms Konduktor can write a NEW library for.

    Asked of the exporter registry rather than hard-coded: reading a library and
    creating one from nothing are different capabilities, and a platform can
    have the first without the second. Registering an exporter is what makes a
    platform a target, so this cannot drift from what actually exists.
    """
    return core_export.targets()


def _require_folder_set(set_id: str):
    found = _require_set(set_id)
    if found.kind != "folder":
        raise HTTPException(400, "This set is pushed into this computer's DJ apps, not exported")
    return found


def _check_targets(targets: list[str], *, kind: str = "folder") -> None:
    """Refuse a set with no targets, or one naming a platform with no exporter.

    Checked when the set is saved: a target that later stops being supported
    would otherwise fail at export time. A "computer" set pushes into the DJ
    apps a computer runs, which are `local_push.PLATFORMS`.
    """
    if not targets:
        raise HTTPException(400, "An export needs at least one platform to export for")
    if kind == "computer":
        unsupported = [t for t in targets if t not in local_push.PLATFORMS]
        if unsupported:
            raise HTTPException(422, f"Konduktor cannot push into {', '.join(unsupported)}")
        return
    unsupported = [t for t in targets if t not in _export_targets()]
    if unsupported:
        raise HTTPException(422, f"Konduktor cannot export to {', '.join(unsupported)} yet")


@app.get("/api/export-targets", response_model=list[PlatformOption])
def export_targets() -> list[PlatformOption]:
    """Every platform, flagged by whether it can be an export TARGET.

    Returns the unsupported ones too, with `installed=False` standing for "not a
    target yet", so the UI can show them disabled with a reason. A silently
    absent option reads as a missing feature; a disabled one reads as a roadmap.
    """
    supported = _export_targets()
    options = []
    for d in registry.drivers():
        exporter = core_export.for_platform(d.platform)
        options.append(PlatformOption(
            platform=d.platform,
            name=getattr(exporter, "display_name", None) or d.display_name,
            library_label=getattr(d, "library_label", ""),
            selects="directory",   # an export destination is always a folder
            installed=d.platform in supported,
            found=0,
            removable=bool(getattr(d, "removable", False)),
            drive_root=bool(getattr(exporter, "drive_root", False)),
            computer_only=bool(getattr(exporter, "computer_only", False)),
            locks_grids=bool(getattr(exporter, "locks_grids", False)),
            writes_auto_gain=bool(getattr(exporter, "writes_auto_gain", False)),
        ))
    # Targets with no reader of their own (the stick's legacy Device Library):
    # listed too, or registering one would not make it selectable.
    readers = {d.platform for d in registry.drivers()}
    order = {e.platform: getattr(e, "menu_order", 1000) for e in core_export.exporters()}
    for exporter in core_export.exporters():
        if exporter.platform in readers:
            continue
        options.append(PlatformOption(
            platform=exporter.platform,
            name=getattr(exporter, "display_name", exporter.platform),
            library_label="",
            selects="directory",
            installed=True,
            found=0,
            removable=bool(getattr(exporter, "drive_root", False)),
            drive_root=bool(getattr(exporter, "drive_root", False)),
            computer_only=bool(getattr(exporter, "computer_only", False)),
            locks_grids=bool(getattr(exporter, "locks_grids", False)),
            writes_auto_gain=bool(getattr(exporter, "writes_auto_gain", False)),
        ))
    # Each exporter states its own place; a platform with no exporter (not yet a
    # target, shown disabled) goes last. Stable, so equal places keep their order.
    return sorted(options, key=lambda o: order.get(o.platform, 1000))


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
        display_name = namer(path) if namer and STATE.remote_config is None else path.name
    except OSError:
        display_name = path.name
    remote = None
    if STATE.remote_config is not None:
        # A remote is called what the user called it, not its file's name.
        display_name = STATE.remote_config.name
        remote = {"id": STATE.remote_config.id, "name": STATE.remote_config.name,
                  "via": STATE.remote.client.via if STATE.remote else None}
    return LibraryInfo(
        platform=caps.platform,
        name=caps.save.app_name,
        library_label=caps.save.library_label,
        path=str(STATE.path),
        display_name=display_name,
        version=caps.version,
        remote=remote,
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
    # A library held by a Konduktor server — not a platform of its own (the
    # server says which it holds), but a way to reach one, so it is offered
    # last, after every platform on this computer.
    from .adapters.remote import config as remote_config

    saved = len(remote_config.remotes())
    out.append(PlatformOption(platform="remote", name="Remote", library_label="remote library",
                              selects="remote", installed=saved > 0, found=saved))
    return out


@app.get("/api/capabilities", response_model=Capabilities)
def capabilities() -> Capabilities:
    """What the loaded library can persist, so the UI can gate its controls."""
    return require_adapter().capabilities()


@app.get("/api/state", response_model=EditState)
def state() -> EditState:
    stem_job = JOBS.active(STEM_JOB)
    normalize_job = JOBS.active(NORMALIZE_JOB)
    pending = STATE.pending.summary() if STATE.pending is not None else None
    return EditState(dirty=require_adapter().dirty, library=_library_info(),
                     pending_stems=pending if pending and pending["tracks"] else None,
                     pending_gain=({"tracks": pending["gain_tracks"], "lossless": pending["gain_lossless"]}
                                   if pending and pending.get("gain_tracks") else None),
                     normalize_job=normalize_job[0].id if normalize_job else None,
                     stem_job=stem_job[0].id if stem_job else None,
                     stem_recovery=STATE.recovery,
                     remote=STATE.remote.describe() if STATE.remote is not None else None)


# ---- stem engine (download / side-load / remove) --------------------------
# The engine and its weights live outside the app (see stems/engine_manager.py).
ENGINE_JOB = "stem-engine-install"
STEM_JOB = "stem-conversion"


@app.get("/api/stems/engine")
def stem_engine_status() -> dict:
    """What is installed, what this computer can run, and what a download costs."""
    from .stems import engine_manager as em

    mgr = em.default_manager()
    running = JOBS.active(ENGINE_JOB)
    return {**mgr.status(), "device": prefs.load_prefs().get("stemDevice", "auto"),
            "install_job": running[0].id if running else None}


@app.post("/api/stems/engine/install", response_model=JobStatus)
def install_stem_engine(body: StemEngineInstall) -> JobStatus:
    """Download the engine (and the weights) as a job, in bytes."""
    from .jobs import JobCancelled
    from .stems import download
    from .stems import engine_manager as em

    mgr = em.default_manager()
    if mgr.base_target is None:
        raise HTTPException(422, "There is no stem engine for this computer")
    target = body.target or mgr.base_target
    if target not in mgr.status(network=False)["offered_targets"]:
        raise HTTPException(400, f"The {target} engine is not offered on this computer")

    def run(handle):
        info = mgr.target_info(target)
        if info is None:
            raise em.EngineError("Could not reach the engine download — check the internet connection")
        installed = mgr.installed()
        need_engine = not (installed and installed["target"] == target)
        need_weights = body.weights and not mgr.weights_installed()
        total = (info["size"] if need_engine else 0) + (mgr.weights_size() if need_weights else 0)
        handle.progress(done=0, total=total, unit="bytes", message="Downloading the stem engine…")
        done = [0]

        def on_bytes(n: int) -> None:
            done[0] += n
            handle.progress(done=done[0])

        try:
            if need_engine:
                mgr.install_engine(target, on_bytes=on_bytes, cancelled=lambda: handle.cancelled)
            if need_weights:
                handle.progress(message="Downloading the separation model…")
                mgr.install_weights(on_bytes=on_bytes, cancelled=lambda: handle.cancelled)
        except download.DownloadCancelled:
            raise JobCancelled()
        return {"target": target, "ready": mgr.ready()}

    try:
        job = JOBS.submit(ENGINE_JOB, run, exclusive_with=(ENGINE_JOB, STEM_JOB))
    except JobBusy:
        raise HTTPException(409, "The stem engine is already downloading, or a conversion is running")
    return JobStatus(**job.as_dict())


def _require_no_pending(what: str) -> None:
    """Refuse an operation that would SAVE or DROP a pending file change as a
    side effect — import and remap save pending edits, reload and restore drop
    them — while converted or normalized tracks await Save/Discard, or a
    conversion or Normalize runs. (Remap and relocation would also move the
    paths the ledger is keyed by.)"""
    if JOBS.active(STEM_JOB):
        raise HTTPException(409, f"{what} is not possible while tracks are being converted to stems")
    if JOBS.active(NORMALIZE_JOB):
        raise HTTPException(409, f"{what} is not possible while tracks are being normalized")
    if STATE.pending is not None and STATE.pending.blocking():
        raise HTTPException(409, f"{what} is not possible until the converted or normalized tracks "
                                 "are saved or discarded")


def _stem_options(body):
    from .stems import convert

    # A remote library's destination is a folder ON THE SERVER: not expanded
    # here, where `~` is this computer's home.
    remote = STATE.remote is not None
    destination = None
    if body.destination:
        destination = Path(body.destination) if remote else Path(body.destination).expanduser()
    return convert.Options(
        mode=body.mode, destination=destination,
        collection=body.collection, playlist_id=body.playlist_id, new_playlist=body.new_playlist,
        device=prefs.load_prefs().get("stemDevice", "auto"),
    )


def _stem_plan(body):
    from .stems import convert, remote_convert

    adapter = require_adapter()
    caps = adapter.capabilities()
    if not (caps.writable and caps.tracks.stem_convertible):
        raise HTTPException(422, "This library cannot hold stem files")
    try:
        if _remote_stems(adapter):
            return adapter, remote_convert.plan(adapter, body.track_ids, _stem_options(body))
        return adapter, convert.plan(adapter, body.track_ids, _stem_options(body), STATE.pending)
    except convert.ConvertError as ex:
        raise HTTPException(400, str(ex))


def _remote_stems(adapter) -> bool:
    """A library held by a server: it plans and swaps, this computer separates
    (`stems/remote_convert.py`)."""
    return hasattr(adapter, "publish_stems")


def _needs_engine(planned) -> bool:
    if hasattr(planned, "server"):
        return any(i.get("reuse") is None for i in planned.items)
    return any(p.reuse is None for p in planned.items)


@app.post("/api/tracks/stems/preview")
def preview_stem_conversion(body: StemConvert) -> dict:
    """What a conversion would do — converted, skipped (and why), disk space."""
    _adapter, planned = _stem_plan(body)
    return planned.as_dict()


@app.post("/api/tracks/stems/convert", response_model=JobStatus)
def convert_to_stems(body: StemConvert) -> JobStatus:
    """Convert tracks to stem files as a job; the swap lands at its end."""
    from .stems import convert
    from .stems import engine_manager as em

    _require_no_batch()
    if JOBS.active("import"):
        raise HTTPException(409, "Wait for the import to finish")
    adapter, planned = _stem_plan(body)
    if planned.blocked:
        raise HTTPException(409, planned.blocked)
    if not planned.items:
        raise HTTPException(400, "None of these tracks can be converted")
    if _needs_engine(planned) and not em.default_manager().ready():
        raise HTTPException(409, "The stem engine is not installed")
    opts = _stem_options(body)

    def run(handle):
        if _remote_stems(adapter):
            from .stems import remote_convert

            return remote_convert.run(handle, adapter, planned, opts,
                                      still_current=lambda: STATE.adapter is adapter)
        return convert.run(handle, adapter, planned, opts, STATE.pending, mutation=STATE.mutation,
                           still_current=lambda: STATE.adapter is adapter)

    try:
        job = JOBS.submit(STEM_JOB, run, exclusive_with=BATCH_JOBS + (ENGINE_JOB, "import"))
    except JobBusy:
        raise HTTPException(409, "Another batch, download or import is running")
    return JobStatus(**job.as_dict())


@app.post("/api/stems/engine/sideload")
def side_load_stem_engine(body: StemSideLoad) -> dict:
    """Install the engine or the weights from files the user already has."""
    from .stems import engine_manager as em

    if JOBS.active(ENGINE_JOB) or JOBS.active(STEM_JOB):
        raise HTTPException(409, "Wait for the running download or conversion to finish")
    mgr = em.default_manager()
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(400, f"Not found: {path}")
    try:
        if body.kind == "engine":
            mgr.side_load_engine(path)
        else:
            mgr.side_load_weights(path)
    except em.EngineError as ex:
        raise HTTPException(400, str(ex))
    return stem_engine_status()


@app.delete("/api/stems/engine")
def remove_stem_engine() -> dict:
    from .stems import engine_manager as em

    if JOBS.active(ENGINE_JOB) or JOBS.active(STEM_JOB):
        raise HTTPException(409, "Wait for the running download or conversion to finish")
    em.default_manager().remove()
    return stem_engine_status()


@app.post("/api/discard", response_model=EditState)
def discard_changes() -> EditState:
    """Drop every unsaved edit — the library re-reads itself from disk.

    A running batch is stopped first (and waited for): it writes into the
    library being discarded, so it would otherwise keep adding edits to the
    freshly reloaded one.
    """
    adapter = require_adapter()
    if not adapter.capabilities().writable:
        raise HTTPException(409, "This library cannot be edited, so there is nothing to discard")
    _stop_batches()
    STATE.discard()
    return state()


@app.post("/api/reload")
def reload_collection() -> dict:
    _require_no_pending("Reloading")
    if not STATE.loaded:
        raise HTTPException(409, "No collection loaded")
    if STATE.remote is not None:
        STATE.adapter.refresh()  # a remote library is re-read from its server
    else:
        STATE.open(STATE.path)  # re-parse current file from disk
    return {"status": "reloaded", "tracks": len(require_adapter().tracks)}


@app.on_event("shutdown")
def _release_remote_session() -> None:
    """A graceful stop hands a remote library's session back (see sidecar.py
    for the abrupt one)."""
    if STATE.remote is not None:
        try:
            STATE.remote.stop()
            STATE.remote.client.release(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass


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
        can_move=True,
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
        can_move=True,
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


@app.post("/api/playlists/move")
def move_playlist(body: MovePlaylist) -> dict:
    """Move a playlist, folder or smart playlist in the tree. Answers the
    node's id AFTER the move, which on some platforms is not the one sent."""
    new_id = require_adapter().move_playlist(body.node_id, body.parent_id, body.index)
    return {"status": "moved", "id": new_id}


@app.post("/api/playlists/{playlist_uuid}/add")
def add_entries(playlist_uuid: str, body: AddEntries) -> dict:
    """Append tracks to a playlist. Tracks already there are skipped, added
    again, or — `duplicates="ask"` — counted with nothing changed (see
    `AddEntries`). `duplicates` in the answer is how many there were."""
    a = require_adapter()
    current = a.playlist_entries(playlist_uuid)
    if current is None:
        raise HTTPException(404, f"Playlist not found: {playlist_uuid}")
    have = set(current)
    dupes = [tid for tid in body.track_ids if tid in have]
    mode = body.duplicates
    if mode != "skip" and not a.capabilities().playlists.duplicates:
        mode = "skip"
    if mode == "ask" and dupes:
        return {"status": "duplicates", "id": playlist_uuid, "added": 0,
                "duplicates": len(dupes), "count": len(current)}
    added = list(body.track_ids) if mode == "add" else [t for t in body.track_ids if t not in have]
    at = len(current) if body.index is None else max(0, min(len(current), body.index))
    n = a.set_playlist_entries(playlist_uuid, current[:at] + added + current[at:]) if added else len(current)
    return {"status": "added", "id": playlist_uuid, "added": len(added),
            "duplicates": len(dupes), "count": n}


@app.post("/api/playlists/{playlist_uuid}/remove")
def remove_entries(playlist_uuid: str, body: RemoveEntries) -> dict:
    """Remove tracks from a playlist — EVERY occurrence of each — without the
    caller holding its entry list (the track menu's Remove ▸ From playlist).
    Other entries keep their order, including ones whose track is missing
    from the collection. `tracks` = how many of the given tracks it held,
    `removed` = entries dropped (more, where a track was there twice)."""
    a = require_adapter()
    current = a.playlist_entries(playlist_uuid)
    if current is None:
        raise HTTPException(404, f"Playlist not found: {playlist_uuid}")
    gone = set(body.track_ids)
    kept = [t for t in current if t not in gone]
    held = len(gone & set(current))
    removed = len(current) - len(kept)
    n = len(current)
    if removed and not body.dry_run:
        n = a.set_playlist_entries(playlist_uuid, kept)
    return {"status": "counted" if body.dry_run else "removed", "id": playlist_uuid,
            "tracks": held, "removed": removed, "count": n}


@app.patch("/api/tracks")
def edit_track(body: EditTrack) -> dict:
    """Edit a single track's safe metadata fields (in-memory; persisted on save)."""
    require_adapter().set_track_metadata(body.track_id, body.fields)
    return {"status": "updated", "id": body.track_id}


@app.patch("/api/tracks/color")
def set_track_color(body: SetTrackColor) -> dict:
    """Set (or clear) the colour tag of every given track (in-memory; persisted
    on save). Gated on `tracks.colors`: the platform's swatches."""
    a = require_adapter()
    if not a.capabilities().tracks.colors:
        raise Unsupported("This library cannot store a track colour")
    tracks = [a.set_track_color(tid, body.color) for tid in body.track_ids]
    return {"status": "updated", "tracks": [t for t in tracks if t is not None]}


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


def _stem_audio(path: Path, stem: int) -> FileResponse:
    """One stem of a stem file as a file of its own (see stems/stream_cache.py):
    a browser only ever decodes an MP4's first stream, which is the mix."""
    from .core import stem_file
    from .stems import stream_cache

    layout = stem_file.stem_layout(path)
    if layout is None or not 0 <= stem < len(layout):
        raise HTTPException(404, "That file has no such stem")
    try:
        return FileResponse(stream_cache.stem_file_for(path, stem), media_type="audio/mp4")
    except (stem_file.StemFileError, OSError) as ex:
        raise HTTPException(500, f"Could not read the stem: {ex}")


def _stem_layout_of(path: Path | None) -> dict:
    from .core import stem_file

    if path is None or not path.exists():
        raise HTTPException(404, "Audio file not found")
    return {"stems": stem_file.stem_layout(path) or []}


@app.get("/api/tracks/stems")
def track_stems(track_id: str) -> dict:
    """The stems a track's FILE carries for playback — `[{name, color}]`, empty
    for anything that is not a stem file (whatever the library says)."""
    return _stem_layout_of(require_adapter().audio_path(track_id))


@app.get("/api/tracks/audio")
def track_audio(track_id: str, stem: int | None = None) -> FileResponse:
    """Stream a track's audio file for playback (supports HTTP Range/seeking);
    with `stem`, that stem of a stem file (0-based) instead."""
    path = require_adapter().audio_path(track_id)
    if path is None:
        raise HTTPException(404, "Track not found")
    if not path.exists():
        raise HTTPException(404, f"Audio file not found: {path}")
    if stem is not None:
        return _stem_audio(path, stem)
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
    """The deck's one-click Analyze: BPM + grid (see `core/grid_detect`) and,
    where the library can store one, the KEY — from one decode.

    One constant-tempo marker, written the way the platform's own analyser would
    (on Traktor that also sets hotcue 1 to the first beat). Octave (half/double)
    ambiguity in halftime genres is left for the ×2 / ÷2 controls. The key
    always follows the analysis (a click on Analyze is a request for it), and
    a key that cannot be found never fails the grid."""
    a = require_adapter()
    if a.track(body.track_id) is None:
        raise HTTPException(404, "Track not found")
    path = a.audio_path(body.track_id)
    if path is None or not path.exists():
        raise HTTPException(400, "Audio file not found (is the drive mounted?)")
    try:
        y, sr = _decode_for_analysis(path)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, f"Could not decode: {ex}")
    try:
        cues = _analyse_grid(a, body.track_id, y=y, sr=sr)
    except _AnalysisError as ex:
        raise HTTPException(400, str(ex))
    if a.capabilities().tracks.key_writable:
        try:
            found = key_detect.detect_key_samples(y, sr)
            if found is not None:
                a.set_key(body.track_id, found.wheel, found.mode)
        except Exception:  # noqa: BLE001 — the grid is written; a key is a bonus
            log.warning("key detection failed for %s", body.track_id, exc_info=True)
    try:  # so is loudness, into Konduktor's own store — always re-measured here
        looked = _loud_lookup("collection", [body.track_id]).get(body.track_id)
        if looked is not None:
            values = _measure_loudness(looked[0], looked[1], path)
            # ...and the DJ app's auto gain, where the track has none yet.
            track = a.track(body.track_id)
            caps = a.capabilities()
            if (values is not None and caps.tracks.auto_gain and caps.writable
                    and track is not None and track.auto_gain_db is None):
                a.set_auto_gain(body.track_id, values)
    except _AnalysisError as ex:
        log.warning("no loudness for %s: %s", body.track_id, ex)
    except Exception:  # noqa: BLE001
        log.warning("loudness measurement failed for %s", body.track_id, exc_info=True)
    return cues


class _AnalysisError(Exception):
    """A track that could not be analysed; the message is user-facing."""


# Batch analysis jobs. ONE at a time across both kinds: a grid run replacing a
# track's grid while a hotcue run places cues on the old one would put cues on
# beats that no longer exist.
GRID_JOB = "grid-analysis"
CUE_JOB = "auto-hotcues"
NORMALIZE_JOB = "normalize"
BATCH_JOBS = (GRID_JOB, CUE_JOB, STEM_JOB, NORMALIZE_JOB)


def _stop_batches() -> None:
    """Cancel every running batch analysis and WAIT for it to stop.

    Used wherever the library is about to be replaced or reloaded: a job still
    finishing its current track would otherwise keep writing into it (or, for
    file work, keep moving files) afterwards. Batches check for a cancel
    between tracks, so this is short.
    """
    stopping = [job for kind in BATCH_JOBS for job in JOBS.active(kind)]
    for job in stopping:
        JOBS.cancel(job.id)
    for job in stopping:
        if not JOBS.wait(job.id, timeout=60):
            log.warning("batch %s (%s) still running after cancel; continuing", job.id, job.kind)


def _require_no_batch() -> None:
    """An early, friendly refusal. Not race-free on its own — the batch routes
    START through `_submit_batch`, which re-checks under the registry's lock."""
    if any(JOBS.active(k) for k in BATCH_JOBS):
        raise HTTPException(409, "A batch analysis is already running")


def _submit_batch(kind: str, run) -> "Job":
    try:
        return JOBS.submit(kind, run, exclusive_with=BATCH_JOBS)
    except JobBusy:
        raise HTTPException(409, "A batch analysis is already running")


def _analyse_grid(a, track_id: str, step: GridStep = GridStep("full"), *,
                  y=None, sr: int | None = None) -> TrackCues:
    """Detect and write one track's grid — shared by the deck's Analyze and the
    batch job, so the two cannot come to mean different things.

    `step` is `core/grid_plan`'s decision. Each kind is written through the
    command a hand edit of the same thing uses, so it inherits that command's
    handling (Traktor's beat-1 companion cue, the time base, the BPM column):
      - "full":  `set_analysed_grid` — one marker, written as the platform's
                 own analyser would (on Traktor also hotcue 1 on the first beat);
      - "bpm":   `set_grid_marker_bpm` on the first marker — the anchor, the
                 marker's name and its companion stay exactly where they are;
      - "phase": `move_grid_marker` on the first marker (dragging its
                 companion), or a fresh marker at the kept BPM when there was
                 only a TEMPO and no grid.
    `y`/`sr` let a caller that already decoded the file share it."""
    if step.action in ("none", "skip"):
        raise ValueError(f"nothing to write for a {step.action!r} step")
    if y is None:
        path = a.audio_path(track_id)
        if path is None or not path.exists():
            raise _AnalysisError("Audio file not found (is the drive mounted?)")
    try:
        kw = {"y": y, "sr": sr} if y is not None else {}
        found = grid_detect.detect_grid(
            "" if y is not None else str(path),
            bpm=step.hold_bpm if step.action == "phase" else None,
            anchor=step.hold_anchor if step.action == "bpm" else None,
            **kw,
        )
    except Exception as ex:  # analysis is best-effort; never 500 the UI
        raise _AnalysisError(f"Analysis failed: {ex}") from ex
    if step.action == "bpm":
        return a.set_grid_marker_bpm(track_id, 0, found.bpm)
    if step.action == "phase":
        cues = a.track_cues(track_id)
        if cues is not None and cues.grid_markers:
            return a.move_grid_marker(track_id, 0, found.anchor)
    # "Analysed", not "replace": each platform writes an analysis result in its
    # own shape (Traktor pairs the first marker with a beat-1 cue).
    return a.set_analysed_grid(track_id, [(found.anchor, found.bpm)])



# ---- Analyze (the context menu's "Analyze…": BPM / Grid / Key) -------------


def _require_analysable(body: AnalyzeRequest) -> None:
    if not (body.bpm or body.grid or body.key or body.loudness or body.gain):
        raise HTTPException(400, "Nothing to analyse: tick BPM, Grid, Key, Gain or Loudness")
    if not (body.bpm or body.grid or body.key or body.gain):
        return  # loudness: Konduktor's own store, so any track, any library
    if body.origin != "collection":
        raise HTTPException(400, "BPM, grid, key and gain are analysed only for the library's own tracks")
    caps = require_adapter().capabilities()
    if (body.bpm or body.grid) and not caps.grid.editable:
        raise HTTPException(422, "This library's beatgrids cannot be edited")
    if body.key and not caps.tracks.key_writable:
        raise HTTPException(422, "This library's keys cannot be written")
    if body.gain and not (caps.tracks.auto_gain and caps.writable):
        raise HTTPException(422, "This library has no auto gain to write")


def _grid_step(a, track, body: AnalyzeRequest, *, exact: bool) -> GridStep:
    """`plan_grid` for one track. `exact` reads the real markers (a lazy
    platform's projected count is only "has a grid"); the preview does not,
    so it cannot see a flexible grid there — the run does."""
    if not (body.bpm or body.grid):
        return GridStep("none")
    if track.grid_marker_count and exact:
        markers = list((a.track_cues(track.id) or TrackCues()).grid_markers)
    else:
        markers = [SimpleNamespace(start=0.0, bpm=track.bpm or 0.0)] * (track.grid_marker_count or 0)
    return plan_grid(bpm=body.bpm, grid=body.grid, markers=markers, track_bpm=track.bpm,
                     locked=bool(track.grid_locked), replace_existing=body.replace_grid)


def _wants_key(track, body: AnalyzeRequest) -> bool:
    return body.key and (track.key_wheel is None or body.replace_key)


def _wants_gain(track, body: AnalyzeRequest) -> bool:
    return body.gain and (track.auto_gain_db is None or body.replace_gain)


@app.post("/api/tracks/analyze/preview", response_model=AnalyzePreview)
def analyze_preview(body: AnalyzeRequest) -> AnalyzePreview:
    """Counts for the Analyze dialog, from the same `plan_grid` the run uses
    (and, for loudness, the same store lookups)."""
    a = require_adapter() if body.origin == "collection" else None
    looked = _loud_lookup(body.origin, body.track_ids) if body.loudness or a is None else {}
    out = AnalyzePreview(total=0)
    # Gain needs a measurement; one the run would make anyway is not "busy"
    # twice, so only the gain's own decision is counted here.
    for tid in dict.fromkeys(body.track_ids):
        if a is not None:
            track = a.track(tid)
            if track is None:
                continue
        elif tid not in looked:
            continue
        out.total += 1
        busy = False
        if a is not None:
            if track.grid_marker_count and not track.grid_locked:
                out.with_grid += 1
            step = _grid_step(a, track, body, exact=False)
            busy = step.action in ("full", "bpm", "phase") or _wants_key(track, body)
            if step.action in ("full", "bpm", "phase"):
                setattr(out, f"grid_{step.action}", getattr(out, f"grid_{step.action}") + 1)
            elif step.action == "skip":
                setattr(out, f"grid_{step.reason}", getattr(out, f"grid_{step.reason}") + 1)
            if track.key_wheel is not None:
                out.with_key += 1
            if body.key:
                if _wants_key(track, body):
                    out.key_set += 1
                else:
                    out.key_existing += 1
            if track.auto_gain_db is not None:
                out.with_gain += 1
            if body.gain:
                if _wants_gain(track, body):
                    out.gain_set += 1
                    busy = True
                else:
                    out.gain_existing += 1
        if body.loudness:
            _target, facts, entry = looked.get(tid, (None, None, None))
            fresh = entry is not None and not entry.stale(facts)
            out.with_loudness += fresh
            if fresh and not body.replace_loudness:
                out.loudness_existing += 1
            else:
                out.loudness_measure += 1
                busy = True
        out.tracks_to_analyze += busy
    return out


def _decode_for_analysis(path: Path):
    """Mono samples at the rate BOTH detectors work at (they share one decode)."""
    from .core import waveform

    assert grid_detect._SR == key_detect.SR, "grid and key detection must share a sample rate"
    return waveform.load(path, sr=key_detect.SR)


def _analyse_one(a, track_id: str, body: AnalyzeRequest, result: dict) -> None:
    """Analyse one track as `body` asks, folding the outcome into `result`.

    `a` is the loaded library for BPM / Grid / Key (None for a device's or a
    folder's tracks, which can only have their loudness measured). The file is
    decoded ONCE and shared by grid and key; loudness decodes on its own, at
    the file's own rate and channels (`core/loudness.py`). A failing part is
    reported and does not stop the others: a track whose grid cannot be fitted
    (a one-shot) can still have a key, and the reverse."""
    try:
        wanted = body.loudness or body.gain or a is None
        looked = _loud_lookup(body.origin, [track_id]).get(track_id) if wanted else None
    except HTTPException:  # the source went away mid-run
        looked = None
    if a is not None:
        track, src, sid = a.track(track_id), a, track_id
    elif looked is not None:
        src, sid = looked[0].src, looked[0].sid
        track = src.track(sid)
    else:
        track = None
    title = (track.title or track_id) if track else track_id
    if track is None:
        result["failed"].append({"title": title, "reason": "Track not found"})
        return
    step = _grid_step(a, track, body, exact=True) if a is not None else GridStep("none")
    if step.action == "skip":
        result["grid"][step.reason] += 1
    want_key = a is not None and _wants_key(track, body)
    if body.key and not want_key:
        result["key"]["existing"] += 1
    want_loud = False
    if body.loudness and looked is not None:
        _target, facts, entry = looked
        want_loud = body.replace_loudness or entry is None or entry.stale(facts)
        if not want_loud:
            result["loudness"]["existing"] += 1
    want_gain = a is not None and _wants_gain(track, body)
    if body.gain and not want_gain:
        result["gain"]["existing"] += 1
    gain_values = None
    if want_gain and looked is not None and not want_loud:
        _target, facts, entry = looked
        if entry is not None and not entry.stale(facts):
            gain_values = entry.values  # measured and fresh: no decode for the gain
    want_grid = step.action in ("full", "bpm", "phase")
    if not (want_grid or want_key or want_loud or want_gain):
        return
    # A gain from a fresh measurement needs no audio (on a remote library,
    # asking for the path would download the file).
    path = None
    if want_grid or want_key or want_loud or gain_values is None:
        path = src.audio_path(sid)
        if path is None or not path.exists():
            result["failed"].append({"title": title, "reason": "Audio file not found (is the drive mounted?)"})
            return
    wrote = False
    if want_grid or want_key:
        try:
            y, sr = _decode_for_analysis(path)
        except Exception as ex:  # noqa: BLE001 — an undecodable file is one failure
            result["failed"].append({"title": title, "reason": f"Could not decode: {ex}"})
            return
        if want_grid:
            try:
                _analyse_grid(a, track_id, step, y=y, sr=sr)
                result["grid"][step.action] += 1
                wrote = True
            except (_AnalysisError, AdapterError) as ex:
                result["failed"].append({"title": title, "reason": f"Grid: {ex}"})
        if want_key:
            try:
                found = key_detect.detect_key_samples(y, sr)
                if found is None:
                    result["key"]["none"] += 1
                else:
                    a.set_key(track_id, found.wheel, found.mode)
                    result["key"]["set"] += 1
                    wrote = True
            except (AdapterError, ValueError) as ex:
                result["failed"].append({"title": title, "reason": f"Key: {ex}"})
    measured_now = False
    if want_loud or (want_gain and gain_values is None):
        try:
            gain_values = _measure_loudness(looked[0], looked[1], path) if looked is not None else None
            measured_now = True
            if want_loud:
                if gain_values is None:
                    result["loudness"]["none"] += 1
                else:
                    result["loudness"]["measured"] += 1
                    wrote = True
        except (_AnalysisError, AdapterError) as ex:
            result["failed"].append({"title": title, "reason": f"Loudness: {ex}"})
            want_gain = False
    if want_gain:
        if gain_values is None:
            if measured_now or looked is not None:
                result["gain"]["none"] += 1
        else:
            try:
                a.set_auto_gain(track_id, gain_values)
                result["gain"]["set"] += 1
                wrote = True
            except AdapterError as ex:
                result["failed"].append({"title": title, "reason": f"Gain: {ex}"})
    if wrote:
        result["analysed"].append(track_id)


def _analyze_job(a, body: AnalyzeRequest):
    ids = list(dict.fromkeys(body.track_ids))

    def run(handle) -> dict:
        result = {
            "analysed": [], "failed": [],
            "grid": {"full": 0, "bpm": 0, "phase": 0, "locked": 0, "existing": 0, "flexible": 0},
            "key": {"set": 0, "existing": 0, "none": 0},
            "loudness": {"measured": 0, "existing": 0, "none": 0},
            "gain": {"set": 0, "existing": 0, "none": 0},
        }
        handle.progress(done=0, total=len(ids))
        for i, track_id in enumerate(ids):
            if handle.cancelled:
                break  # keep what is done; return it rather than raise
            track = a.track(track_id) if a is not None else None
            handle.progress(message=(track.title or track_id) if track else Path(track_id).name)
            _analyse_one(a, track_id, body, result)
            handle.progress(done=i + 1)
        # The names the context menu's toast read before the dialog existed.
        result["locked"], result["existing"] = result["grid"]["locked"], result["grid"]["existing"]
        return result

    return run


@app.post("/api/tracks/analyze", response_model=JobStatus)
def analyze_batch(body: AnalyzeRequest) -> JobStatus:
    """Analyse many tracks' BPM / grid / key as a JOB; poll `/api/jobs/{id}`.

    Each track is written as it finishes, into the in-memory model like any
    other edit — so a cancel keeps what is done (it is all unsaved until Save),
    and the result is returned even from a cancelled run. Loudness is stored
    as each track is measured and is not an edit at all (`loudness_store.py`).
    A track that fails is reported and skipped, never fatal: one unmounted
    drive must not stop 400 other tracks."""
    _require_analysable(body)
    if body.origin == "device":
        require_source()
    a = require_adapter() if body.origin == "collection" else None
    _require_no_batch()
    job = _submit_batch(GRID_JOB, _analyze_job(a, body))
    return JobStatus(**job.as_dict())


@app.post("/api/tracks/grid/auto-batch", response_model=JobStatus)
def auto_grid_batch(body: AutoGridBatchRequest) -> JobStatus:
    """The pre-dialog route: BPM + Grid, no key. Kept as an alias of
    `/api/tracks/analyze` so the two cannot drift."""
    return analyze_batch(AnalyzeRequest(track_ids=body.track_ids, bpm=True, grid=True,
                                        key=False, replace_grid=body.replace_existing))


# ---- Normalize (the track menu's Mastering ▸ Normalize…) --------------------
#
# Changes the audio files' level in place (`gain/normalize.py`): parked
# originals until Save, like a stem conversion. The library's own tracks only.


def _remote_normalize(adapter) -> bool:
    """A library held by a server: it plans and swaps, this computer rewrites
    (`gain/remote_normalize.py`)."""
    return hasattr(adapter, "normalize_plan")


def _normalize_target(body: NormalizeRequest):
    a = require_adapter()
    caps = a.capabilities()
    if not (caps.writable and caps.tracks.normalizable):
        raise HTTPException(422, "This library's audio files cannot be normalized")
    if not -30.0 <= body.target <= 0.0 or not -12.0 <= body.ceiling <= 0.0:
        raise HTTPException(400, "Target or ceiling out of range")
    from .gain import normalize

    return a, normalize.Options(measure=body.measure, target=body.target, ceiling=body.ceiling)


@app.post("/api/tracks/normalize/preview")
def normalize_preview(body: NormalizeRequest) -> dict:
    """What Normalize would do per track, from the same `plan_gain` the run uses."""
    from .gain import normalize

    a, opts = _normalize_target(body)
    if _remote_normalize(a):
        from .gain import remote_normalize

        try:
            return remote_normalize.plan(a, body.track_ids, opts)
        except normalize.NormalizeError as ex:
            raise HTTPException(400, str(ex))
    return normalize.plan(a, body.track_ids, opts, STATE.loudness, STATE.pending).as_dict()


@app.post("/api/tracks/normalize", response_model=JobStatus)
def normalize_tracks(body: NormalizeRequest) -> JobStatus:
    """Normalize as a JOB; the new files land at its end as unsaved changes."""
    from .gain import normalize

    a, opts = _normalize_target(body)
    _require_no_batch()
    if JOBS.active("import"):
        raise HTTPException(409, "Wait for the import to finish")

    def run(handle):
        if _remote_normalize(a):
            from .gain import remote_normalize

            return remote_normalize.run(handle, a, body.track_ids, opts,
                                        still_current=lambda: STATE.adapter is a)
        return normalize.run(handle, a, body.track_ids, opts, store=STATE.loudness, pending=STATE.pending,
                             mutation=STATE.mutation, still_current=lambda: STATE.adapter is a)

    try:
        job = JOBS.submit(NORMALIZE_JOB, run, exclusive_with=BATCH_JOBS + ("import",))
    except JobBusy:
        raise HTTPException(409, "Another batch or import is running")
    return JobStatus(**job.as_dict())


# ---- Loudness (the Mastering columns) --------------------------------------
#
# Measured by `core/loudness.py` into Konduktor's own store (`loudness_store.py`),
# keyed by the FILE: the loaded library's (on a remote library, the server's),
# this computer's for a device's or a Files folder's tracks. Never an edit.


@dataclass
class _LoudTarget:
    src: object      # .track / .audio_facts / .audio_path for `sid`
    sid: str
    store: object    # loudness_store.LoudnessStore


def _loud_target(origin: str, track_id: str) -> _LoudTarget | None:
    """Where a track's file and its loudness value live, by whose id it is."""
    if origin == "device":
        source = require_source()
        return _LoudTarget(source, track_id, loudness_store.local()) if source.track(track_id) else None
    if origin == "folder":
        raw = _on_server(track_id)
        if raw is not None:
            if STATE.remote is None or STATE.adapter is None:
                return None
            files = STATE.adapter.listed_files([raw])
            return _LoudTarget(files, raw, STATE.loudness) if files else None
        source = FOLDERS.source_for_file(track_id)
        if source is not None:
            return _LoudTarget(source, track_id, loudness_store.local())
        if not STATE.loaded:
            return None
        # Not a listed file: a folder view's held row, which IS a library track.
    a = require_adapter()
    return _LoudTarget(a, track_id, STATE.loudness) if a.track(track_id) else None


def _loud_lookup(origin: str, track_ids) -> dict:
    """`{id: (target, facts | None, entry | None)}` for the known ids. Each
    source is asked for its facts once and each store once — a whole library's
    worth for the table's columns. Facts never download (`audio_facts`); a
    file that is missing has none, and so no value."""
    targets = {tid: t for tid in dict.fromkeys(track_ids) if (t := _loud_target(origin, tid)) is not None}
    facts: dict[str, object] = {}
    by_source: dict[int, list[str]] = {}
    for tid, t in targets.items():
        by_source.setdefault(id(t.src), []).append(tid)
    for tids in by_source.values():
        src = targets[tids[0]].src
        found = src.audio_facts([targets[tid].sid for tid in tids])
        for tid in tids:
            facts[tid] = found.get(targets[tid].sid)
    entries: dict[str, object] = {}
    by_store: dict[int, list[str]] = {}
    for tid, t in targets.items():
        if facts[tid] is not None:
            by_store.setdefault(id(t.store), []).append(tid)
    for tids in by_store.values():
        store = targets[tids[0]].store
        got = store.get([facts[tid].key for tid in tids])
        for tid in tids:
            entries[tid] = got.get(facts[tid].key)
    return {tid: (t, facts[tid], entries.get(tid)) for tid, t in targets.items()}


def _measure_loudness(target: _LoudTarget, facts, path: Path | None = None):
    """Measure one file and store it (None: silent, or under 3 s). Raises
    `_AnalysisError` with a user-facing reason. The facts stored are the ones
    read BEFORE decoding, so a file changing meanwhile reads as stale."""
    if not getattr(target.store, "available", True):
        raise _AnalysisError("This Konduktor server is too old to keep loudness — update it")
    if facts is None:
        raise _AnalysisError("Audio file not found (is the drive mounted?)")
    if path is None:
        path = target.src.audio_path(target.sid)
    if path is None or not Path(path).exists():
        raise _AnalysisError("Audio file not found (is the drive mounted?)")
    try:
        values = loudness.measure_file(path)
    except Exception as ex:  # noqa: BLE001 — an undecodable file is one failure
        raise _AnalysisError(f"Could not decode: {ex}")
    if values is not None:
        target.store.put(facts, values)
    return values


@app.post("/api/loudness", response_model=LoudnessValues)
def loudness_values(body: LoudnessRequest) -> LoudnessValues:
    """The Mastering columns' values for a list's tracks — measured ones only."""
    values = {}
    for tid, (_target, facts, entry) in _loud_lookup(body.origin, body.track_ids).items():
        if entry is not None:
            values[tid] = LoudnessValue(**entry.values.as_dict(), stale=entry.stale(facts))
    return LoudnessValues(values=values)


@app.post("/api/tracks/loudness", response_model=LoudnessValue)
def measure_loudness(body: MeasureLoudnessRequest) -> LoudnessValue:
    """The deck's Analyze where it cannot write a grid (a read-only library, a
    device, a loose file): measure loudness alone, always afresh."""
    looked = _loud_lookup(body.origin, [body.track_id]).get(body.track_id)
    if looked is None:
        raise HTTPException(404, "Track not found")
    try:
        values = _measure_loudness(looked[0], looked[1])
    except _AnalysisError as ex:
        raise HTTPException(400, str(ex))
    if values is None:
        raise HTTPException(400, "Nothing to measure: the track is silent or under 3 seconds")
    return LoudnessValue(**values.as_dict())


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

    job = _submit_batch(CUE_JOB, run)
    return JobStatus(**job.as_dict())


@app.patch("/api/tracks/cue", response_model=TrackCues)
def edit_cue_type(body: SetCueType) -> TrackCues:
    """Change the type of an existing cue (keeps its position)."""
    return require_adapter().set_cue_type(body.track_id, body.slot, body.type)


@app.patch("/api/tracks/cue/color", response_model=TrackCues)
def edit_cue_color(body: SetCueColor) -> TrackCues:
    """Recolour an existing cue from the platform's palette (None = uncoloured)."""
    return require_adapter().set_cue_color(body.track_id, body.slot, body.color)


@app.delete("/api/tracks/cue", response_model=TrackCues)
def delete_cue(track_id: str, slot: int) -> TrackCues:
    """Remove the hotcue in a slot."""
    return require_adapter().delete_cue(track_id, slot)


# Memory cues — cues on no pad, addressed by CuePoint.id (gated on
# capabilities.cues.memory_cues; the adapter refuses past max_memory_cues).
@app.post("/api/tracks/memory-cue", response_model=TrackCues)
def add_memory_cue(body: AddMemoryCue) -> TrackCues:
    """Add a memory cue (a memory loop when `length` > 0)."""
    return require_adapter().add_memory_cue(
        body.track_id,
        start_sec=body.start,
        cue_type=body.type,
        length_sec=body.length,
        name=body.name,
        color=body.color,
    )


@app.patch("/api/tracks/memory-cue", response_model=TrackCues)
def rename_memory_cue(body: RenameMemoryCue) -> TrackCues:
    """Name a memory cue (rekordbox calls it the comment); None/"" clears it."""
    return require_adapter().rename_memory_cue(body.track_id, body.cue_id, body.name or None)


@app.patch("/api/tracks/memory-cue/type", response_model=TrackCues)
def edit_memory_cue_type(body: SetMemoryCueType) -> TrackCues:
    """Change a memory cue's point type (keeps its position)."""
    return require_adapter().set_memory_cue_type(body.track_id, body.cue_id, body.type)


@app.patch("/api/tracks/memory-cue/color", response_model=TrackCues)
def edit_memory_cue_color(body: SetMemoryCueColor) -> TrackCues:
    """Recolour a memory cue from capabilities.cues.memory_palette (None = uncoloured)."""
    return require_adapter().set_memory_cue_color(body.track_id, body.cue_id, body.color)


@app.delete("/api/tracks/memory-cue", response_model=TrackCues)
def delete_memory_cue(track_id: str, cue_id: str) -> TrackCues:
    """Remove a memory cue."""
    return require_adapter().delete_memory_cue(track_id, cue_id)


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
    return [HistoryEntry(**e.__dict__) for e in STATE.services.history()]


@app.post("/api/history/{commit_id}/restore", response_model=CollectionStatus)
def restore_version(commit_id: str) -> CollectionStatus:
    """Restore the collection to a past version. Writes that version back as a
    NEW forward save (a fresh commit on top of history — never a rewind), then
    reloads. The user should close Traktor first (it overwrites on exit)."""
    _require_no_pending("Restoring a version")
    require_adapter()
    if not STATE.restore_version(commit_id):
        raise HTTPException(404, f"Version not found: {commit_id}")
    return collection_status()


@app.delete("/api/history")
def clear_history() -> dict:
    """Permanently delete ALL version history for the current collection."""
    require_adapter()
    STATE.services.clear_history()
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
def source_track_audio(track_id: str, stem: int | None = None) -> FileResponse:
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
    if stem is not None:
        return _stem_audio(path, stem)
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)


@app.get("/api/source/tracks/stems")
def source_track_stems(track_id: str) -> dict:
    return _stem_layout_of(require_source().audio_path(track_id))


# ---- drives and folders -----------------------------------------------
#
# The sidebar's Devices and Local Storage trees. Browsing a folder is READ-ONLY
# and needs no library of its own: the files are projected as tracks for the
# table and deck, and adding them goes through the ordinary importer.

FOLDERS = FolderScanner()

# A remote library's content folder rides the same routes. Its paths carry this
# prefix, so a path on the server can never be taken for the same path on this
# computer — and the UI's folder view, deck and add dialog need no second set of
# routes. The prefix alone is the content folder itself.
SERVER_FOLDER = "server:"


def _on_server(path: str) -> str | None:
    """The server path behind a prefixed one ('' = the content folder), else None."""
    return path[len(SERVER_FOLDER):] if path.startswith(SERVER_FOLDER) else None


def _server_track(track: Track) -> Track:
    return track.model_copy(update={"id": SERVER_FOLDER + track.id})


def _folder_rows(dest, tracks: list[Track], held_of) -> tuple[list[Track], dict[str, str]]:
    """A folder's files as table rows. Where the library holds a file, the row
    IS the library's track — its key, rating, comments, cues and plays, and
    its id, so it plays, edits and drags as the collection track it is
    (decided 2026-10-10) — else the file's own tags. Also returns library id →
    the file's folder id, for the rows that are library tracks: adding still
    speaks in folder ids."""
    rows: list[Track] = []
    files: dict[str, str] = {}
    for track in tracks:
        known = held_of(track)
        held = dest.track(known) if known else None
        if held is None or held.id in files:
            rows.append(track)
            continue
        rows.append(held)
        files[held.id] = track.id
    return rows, files


def _server_file(track_id: str) -> tuple[object, str]:
    """The remote adapter and the server path of a listed content-folder file."""
    adapter = _require_remote()
    raw = _on_server(track_id) or ""
    if adapter.listed_files([raw]) is None:
        raise HTTPException(404, "Track not found — browse to its folder first")
    return adapter, raw


def _drive_of(path: Path, drives: list[dict]) -> dict | None:
    """The drive `path` lives on: the longest drive root that contains it."""
    best = None
    for d in drives:
        root = Path(d["path"])
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if best is None or len(root.parts) > len(Path(best["path"]).parts):
            best = d
    return best


@app.get("/api/fs/drives", response_model=list[Drive])
def fs_drives() -> list[Drive]:
    """Every drive, with the OneLibrary library on it where there is one.

    Polled, like `/api/sources`, because drives come and go while the app is
    open. Cheap after the first call: which drives are internal is cached per
    mount, since asking the OS is a subprocess on macOS.
    """
    found = places.drives()
    libraries = sources()
    out = [Drive(**d) for d in found]
    if STATE.remote is not None and STATE.remote_config is not None:
        # The server holding the library, first: its content folder is where
        # this library's music is, and this computer cannot see it otherwise.
        out.insert(0, Drive(name=STATE.remote_config.name, path=SERVER_FOLDER, kind="server"))
    externals = [d for d in found if d["path"] != "/"]
    for lib in libraries:
        home = _drive_of(Path(lib.path), externals)
        if home is None:
            continue
        for drive in out:
            if drive.path == home["path"]:
                drive.library = lib
    return out


@app.get("/api/fs/folders", response_model=list[FsEntry])
def fs_folders(path: str) -> list[FsEntry]:
    """The visible folders directly inside `path` — one level of a Files tree."""
    raw = _on_server(path)
    if raw is not None:
        listing = _require_remote().client.fs_list(raw or None)
        return [FsEntry(name=e["name"], path=SERVER_FOLDER + e["path"])
                for e in listing["entries"] if e["is_dir"]]
    base = Path(path).expanduser()
    if not base.is_dir():
        raise HTTPException(404, f"Not a folder: {base}")
    return [FsEntry(name=p.name, path=str(p)) for p in places.subfolders(base)]


def _formats() -> list[str]:
    return require_adapter().capabilities().tracks.audio_formats


@app.get("/api/folder/tracks", response_model=FolderTracks)
def folder_tracks(path: str) -> FolderTracks:
    """The audio files directly inside a folder, and which are already held."""
    raw = _on_server(path)
    if raw is not None:
        adapter = _require_remote()
        listing = adapter.folder_listing(raw or None)
        rows, files = _folder_rows(adapter, [_server_track(t) for t in listing.tracks],
                                   lambda t: listing.held(t.id[len(SERVER_FOLDER):]))
        return FolderTracks(path=SERVER_FOLDER + listing.path, tracks=rows,
                            in_collection=list(files), files=files, removable=False)
    base = Path(path).expanduser()
    if not base.is_dir():
        raise HTTPException(404, f"Not a folder: {base}")
    source = FOLDERS.scan(base, _formats())
    dest = require_adapter()
    held = importer._existing_files(dest)
    rows, files = _folder_rows(dest, source.tracks, lambda t: held.get(importer._key(Path(t.id))))
    drive = _drive_of(base, places.drives())
    return FolderTracks(
        path=str(base),
        tracks=rows,
        in_collection=list(files),
        files=files,
        removable=drive is None or drive["kind"] == "external",
    )


@app.post("/api/folder/dropped", response_model=DroppedFiles)
def folder_dropped(body: DroppedPaths) -> DroppedFiles:
    """Files and folders dropped onto the window. Folders are searched all the
    way down; every audio file found is then addable and playable through the
    folder routes, as if it had been browsed to in the sidebar."""
    dest = require_adapter()
    dropped = FOLDERS.dropped(body.paths, _formats())
    held = importer._existing_files(dest)
    rows, files = _folder_rows(dest, dropped.tracks, lambda t: held.get(importer._key(Path(t.id))))
    return DroppedFiles(tracks=rows, files=files, skipped=dropped.skipped, truncated=dropped.truncated)


def _folder_track(track_id: str) -> FolderSource:
    # Only a file a scan found. This is what keeps the audio route from being a
    # way to read any path on the disk.
    source = FOLDERS.source_for_file(track_id)
    if source is None:
        raise HTTPException(404, "Track not found — browse to its folder first")
    return source


@app.get("/api/folder/capabilities", response_model=Capabilities)
def folder_capabilities(path: str) -> Capabilities:
    from .core.folder import capabilities_for

    raw = _on_server(path)
    if raw is not None:
        return capabilities_for(Path(raw) if raw else Path(STATE.remote_config.name if STATE.remote_config else "Server"))
    return capabilities_for(Path(path))


@app.get("/api/folder/tracks/cues", response_model=TrackCues)
def folder_track_cues(track_id: str) -> TrackCues:
    if _on_server(track_id) is not None:
        _server_file(track_id)
        return TrackCues(cues=[], grid_markers=[])
    cues = _folder_track(track_id).track_cues(track_id)
    assert cues is not None
    return cues


@app.get("/api/folder/tracks/audio")
def folder_track_audio(track_id: str, stem: int | None = None) -> FileResponse:
    if _on_server(track_id) is not None:
        adapter, raw = _server_file(track_id)
        path = adapter.folder_audio(raw)  # this computer's copy, as for a library track
    else:
        path = _folder_track(track_id).audio_path(track_id)
    if path is None or not path.is_file():
        raise HTTPException(404, "Audio file not found")
    if stem is not None:
        return _stem_audio(path, stem)
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)


@app.get("/api/folder/tracks/stems")
def folder_track_stems(track_id: str) -> dict:
    if _on_server(track_id) is not None:
        adapter, raw = _server_file(track_id)
        return _stem_layout_of(adapter.folder_audio(raw))
    return _stem_layout_of(_folder_track(track_id).audio_path(track_id))


def _require_addable(dest) -> None:
    caps = dest.capabilities()
    if not (caps.writable and caps.tracks.addable):
        raise HTTPException(422, "Tracks cannot be added to this library")


def _check_add_targets(dest, body: FolderAddRequest) -> None:
    if body.playlist_id is not None and dest.playlist_entries(body.playlist_id) is None:
        raise HTTPException(404, f"Playlist not found: {body.playlist_id}")
    if body.export_id is not None:
        if STATE.library_id is None or exports.get(STATE.library_id, body.export_id) is None:
            raise HTTPException(404, f"Export not found: {body.export_id}")


def _is_server_add(body: FolderAddRequest) -> bool:
    on_server = [_on_server(t) is not None for t in body.track_ids]
    if any(on_server) and not all(on_server):
        raise HTTPException(400, "Files on the server and on this computer are added separately")
    return bool(on_server) and all(on_server)


def _server_add_plan(body: FolderAddRequest):
    """Files in the remote library's content folder: already where the library
    can reach them, so they are added IN PLACE — nothing uploaded, nothing asked."""
    dest = _require_remote()
    _require_addable(dest)
    paths = [_on_server(t) for t in body.track_ids]
    source = dest.listed_files(paths)
    if source is None:
        raise HTTPException(400, "No tracks to add")
    _check_add_targets(dest, body)
    return source, dest, importer.plan_on_host(source, paths)


def _folder_add_plan(body: FolderAddRequest):
    dest = require_adapter()
    _require_addable(dest)
    tracks = []
    for track_id in body.track_ids:
        track = _folder_track(track_id).track(track_id)
        if track is not None:
            tracks.append(track)
    if not tracks:
        raise HTTPException(400, "No tracks to add")
    # One source across however many folders the ids came from.
    source = FolderSource(Path(tracks[0].id).parent, tracks)
    destination = None
    if importer.places_audio(dest):
        # The library decides (a stick): no mode, no destination to ask for.
        destination = dest.audio_home()
    elif body.mode == "copy":
        if not body.destination:
            raise HTTPException(400, "Copying needs a destination folder")
        destination = Path(body.destination).expanduser()
    _check_add_targets(dest, body)
    return source, dest, destination, importer.plan(
        source, dest, destination, track_ids=[t.id for t in tracks]
    )


@app.post("/api/folder/add/preview")
def folder_add_preview(body: FolderAddRequest) -> dict:
    if _is_server_add(body):
        _source, _dest, plan = _server_add_plan(body)
        out = plan.as_dict()
        out.update(total_bytes=0, free_bytes=None, enough_space=None, removable=False)
        return out
    _source, dest, destination, plan = _folder_add_plan(body)
    out = plan.as_dict(free_bytes=importer.free_bytes(destination, dest) if destination else None)
    if importer.places_audio(dest):
        _placed_space(out, dest, plan, destination)
    elif body.mode == "reference":
        # Nothing is copied, so there is no size to fit anywhere.
        out.update(total_bytes=0, free_bytes=None, enough_space=None)
    elif destination is not None:
        # Copying a file that is already in the import folder makes a second
        # copy beside it — legal, but rarely meant, so the dialog says so.
        home = destination.resolve()
        out["in_destination"] = sum(
            1 for t in plan.importable
            if t.source_path is not None and t.source_path.resolve().is_relative_to(home)
        )
    # Per FILE, not per request: a drop can mix the computer's own disk with a
    # stick, and only the files on the stick go missing when it is unplugged.
    drives = places.drives()
    external: dict[str, int] = {}
    for t in plan.importable:
        if t.source_path is None:
            continue
        drive = _drive_of(t.source_path, drives)
        if drive is None or drive["kind"] == "external":
            name = drive["name"] if drive else str(t.source_path.anchor or t.source_path.parent)
            external[name] = external.get(name, 0) + 1
    out["removable"] = bool(external)
    out["removable_count"] = sum(external.values())
    out["removable_drives"] = sorted(external)
    return out


@app.post("/api/folder/add", response_model=JobStatus)
def folder_add(body: FolderAddRequest) -> JobStatus:
    """Add browsed files to the collection. A job, like import: copying can be long."""
    _require_no_pending("Adding files")
    in_place = _is_server_add(body)
    if in_place:
        source, dest, plan = _server_add_plan(body)
        destination = None
    else:
        source, dest, destination, plan = _folder_add_plan(body)
    if not plan.importable and not plan.existing:
        raise HTTPException(400, "Nothing to add (none of those files exist)")
    # Nothing is copied in place, so there is no space to check.
    if not in_place and (body.mode == "copy" or importer.places_audio(dest)):
        free = importer.free_bytes(destination, dest)
        needed = importer.placed_bytes(dest, plan) if importer.places_audio(dest) else plan.total_bytes
        if free is not None and free < needed + importer.SPACE_HEADROOM:
            raise HTTPException(
                400,
                f"Not enough space: {needed / 1e9:.1f} GB needed, "
                f"{free / 1e9:.1f} GB free at {destination}",
            )
    if JOBS.active("import"):
        raise HTTPException(409, "An import is already running")
    library = STATE.library_id

    def work(handle):
        result = importer.run(
            source, dest, plan, handle,
            reference=in_place or body.mode == "reference",
            into_playlist=body.playlist_id,
            into_playlist_index=body.playlist_index,
        )
        # Export sets are Konduktor's own data, not the library's, so they are
        # updated after the library has been saved rather than inside it.
        if body.export_id is not None and library is not None:
            exports.add(library, body.export_id, track_ids=result["track_ids"])
        return result

    job = JOBS.submit("import", work)
    return JobStatus(**job.as_dict())


# ---- import -----------------------------------------------------------
#
# The only operation in the app that can run for minutes, so it is the only one
# that is a JOB rather than a request. See `jobs.py` for why that is polling and
# threads rather than something fancier.


def _placed_space(out: dict, dest, plan, destination: Path) -> None:
    """A self-placing library's preview: only what is really COPIED needs room,
    and the preview says how many files will be copied vs used in place."""
    needed = importer.placed_bytes(dest, plan)
    free = importer.free_bytes(destination)
    in_place = sum(1 for t in plan.importable if t.source_path is not None
                   and t.source_path.resolve().is_relative_to(destination.resolve()))
    out.update(total_bytes=needed, free_bytes=free,
               enough_space=None if free is None else free >= needed + importer.SPACE_HEADROOM,
               places_audio=True, in_place=in_place, copied=len(plan.importable) - in_place)


def _import_plan(body: ImportRequest):
    source, dest = require_source(), require_adapter()
    _require_addable(dest)
    destination = (dest.audio_home() if importer.places_audio(dest)
                   else Path(body.destination).expanduser())
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
    _source, dest, destination, plan = _import_plan(body)
    out = plan.as_dict(free_bytes=importer.free_bytes(destination, dest))
    if importer.places_audio(dest):
        _placed_space(out, dest, plan, destination)
    return out


@app.post("/api/import", response_model=JobStatus)
def start_import(body: ImportRequest) -> JobStatus:
    """Start an import. Returns immediately; poll the job for progress."""
    _require_no_pending("Importing")
    source, dest, destination, plan = _import_plan(body)
    if not plan.importable:
        raise HTTPException(400, "Nothing to import (no tracks, or none of their files exist)")
    free = importer.free_bytes(destination, dest)
    needed = importer.placed_bytes(dest, plan) if importer.places_audio(dest) else plan.total_bytes
    if free is not None and free < needed + importer.SPACE_HEADROOM:
        raise HTTPException(
            400,
            f"Not enough space: {needed / 1e9:.1f} GB needed, "
            f"{free / 1e9:.1f} GB free at {destination}",
        )
    # One at a time: two concurrent imports would race on filename collisions and
    # on the destination library's save.
    if JOBS.active("import"):
        raise HTTPException(409, "An import is already running")

    folder_name = body.folder_name
    if folder_name is None and STATE.source is not None:
        folder_name = STATE.source.capabilities().save.library_label
    library = STATE.library_id

    def work(handle):
        result = importer.run(
            source, dest, plan, handle, folder_name=folder_name, into_playlist=body.into_playlist,
            into_playlist_index=body.into_playlist_index,
        )
        # As folder add: an export set is Konduktor's data, updated afterwards.
        if body.export_id is not None and library is not None:
            exports.add(library, body.export_id, track_ids=result["track_ids"])
        return result

    job = JOBS.submit("import", work)
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
