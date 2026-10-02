"""The Konduktor SERVER: one library, held where its audio lives.

Runs in a container on the user's NAS (`server/Dockerfile`) and is opened from
the desktop app as a "Remote" library. It is deliberately NOT `main.py` behind a
password: the UI's routes would become a public contract, and half of them are
about THIS computer (its drives, its stem engine, its sticks). What it serves is
the `LibraryAdapter` protocol itself (`remote_protocol.py`), plus the few things
that are bytes — audio, art, uploads — and the app-level bookkeeping that lives
beside the library: save + backups + version history, export sets, the stems
ledger.

Lightweight by decision: the server never DECODES audio. Analysis, waveforms and
stem separation all run on the computer that opened the library; what arrives
here are the results, as ordinary commands. Its work is file IO, the native
model, tags, and header reads.

One computer at a time (`session.py`): every command carries the session token,
and anything that changes the library without it is answered 423 with who holds
it.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import shutil
import threading
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import __version__, exports
from .. import remote_protocol as rp
from ..app_state import AppState
from ..core import registry
from ..core.adapter import (
    AdapterError,
    InvalidCommand,
    LibraryNotSupported,
    NewTrack,
    NotFound,
    Unsupported,
)
from ..core.places import is_os_housekeeping
from .config import ConfigError, ServerConfig
from .session import InUse, Lost, SessionLock

log = logging.getLogger("konduktor.server")

#: Upload partials, published by a rename that never overwrites.
PARTIAL_SUFFIX = ".konduktor-upload"
_CHUNK = 1 << 20


class Server:
    """The open library, its session, and the revision counter clients follow."""

    def __init__(self, config: ServerConfig, *, state: AppState | None = None,
                 lock: SessionLock | None = None):
        self.config = config
        self.state = state if state is not None else _default_state()
        self.lock = lock or SessionLock()
        self.rev = 0
        self._rev_lock = threading.Lock()

    # ---- opening ---------------------------------------------------------------
    def open(self) -> None:
        cfg = self.config
        try:
            driver = registry.driver_for(cfg.library)
        except LibraryNotSupported as ex:
            raise ConfigError(f"KONDUKTOR_LIBRARY: {ex}")
        if driver.platform != cfg.platform:
            raise ConfigError(
                f"KONDUKTOR_PLATFORM is {cfg.platform!r} but {cfg.library} is a "
                f"{driver.display_name} library"
            )
        self.state.open_hooks.append(self._configure)
        self.state.open(cfg.library)

    def _configure(self, adapter) -> None:
        """After every (re)open: the configured path mapping, and no missing-files
        search of a container's empty drives."""
        maps = self.config.path_maps
        if maps:
            adapter.set_path_mapping(maps[0])
            adapter.set_session_mappings(list(maps[1:]))
            # A file uploaded here is stored as the library stores every other
            # path — so it still opens, mapping and all, where it was made.
            write_stored = getattr(adapter, "set_write_stored_paths", None)
            if write_stored is not None:
                write_stored(True)
        self.state.relocation = None

    @property
    def adapter(self):
        if self.state.adapter is None:
            raise HTTPException(503, "The library is not open")
        return self.state.adapter

    def bump(self) -> int:
        with self._rev_lock:
            self.rev += 1
            return self.rev

    # ---- facts -------------------------------------------------------------------
    def capabilities(self):
        """The adapter's own, plus what being held by a server changes."""
        caps = self.adapter.capabilities().model_copy(deep=True)
        caps.tracks.audio_destination = "library"
        caps.tracks.reference = False
        caps.paths.remappable = False
        # Nothing runs a DJ app against a library on a NAS while it is being
        # saved: the "close Traktor first" warning would only confuse.
        caps.save.overwrite_risk = "none"
        return caps

    def name(self) -> str:
        return self.config.name or self.config.library.stem

    def pending_edits(self, client_id: str | None = None) -> dict | None:
        """The unsaved edits someone else left, for the computer arriving."""
        adapter = self.adapter
        if not adapter.dirty:
            return None
        editor = self.lock.last_editor or {}
        if client_id and editor.get("client_id") == client_id:
            return None
        summary = getattr(adapter, "edit_summary", None)
        return {
            "machine": editor.get("machine"),
            "summary": (summary() if summary else "") or "unsaved changes",
        }

    def confine(self, raw: str | None) -> Path:
        """A path inside the content folder, or 400. Resolved first, so neither
        `..` nor a symlink can lead out of it."""
        root = self.config.content
        candidate = Path(raw) if raw else root
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise HTTPException(400, f"{raw} is outside the library's content folder")
        return resolved


def _default_state() -> AppState:
    # The process-wide STATE: a server holds one library, and the stem end step
    # reaches for `app_state.STATE.mutation` like the desktop app's does.
    from ..app_state import STATE

    return STATE


def create_app(server: Server) -> FastAPI:
    cfg = server.config
    security = HTTPBasic(realm="Konduktor")

    def auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
        user_ok = secrets.compare_digest(creds.username.encode(), cfg.username.encode())
        pass_ok = secrets.compare_digest(creds.password.encode(), cfg.password.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(401, "Wrong username or password",
                                headers={"WWW-Authenticate": 'Basic realm="Konduktor"'})

    app = FastAPI(title="Konduktor server", version=__version__, dependencies=[Depends(auth)])
    app.state.server = server

    @app.exception_handler(AdapterError)
    def _adapter_error(_request, exc: AdapterError):
        status = {NotFound: 404, InvalidCommand: 400, Unsupported: 422,
                  LibraryNotSupported: 400}.get(type(exc), 400)
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    def session(token: str | None = Header(None, alias=rp.SESSION_HEADER)):
        try:
            return server.lock.check(token)
        except Lost as ex:
            raise HTTPException(423, {
                "code": "taken_over", "message": str(ex), "holder": server.lock.describe(),
            })

    # ---- handshake + session -----------------------------------------------------
    @app.get("/v1/hello")
    def hello(client_id: str | None = None) -> dict:
        state = server.state
        caps = server.capabilities()
        return {
            "api_version": list(rp.API_VERSION),
            "app_version": __version__,
            "platform": caps.platform,
            "app_name": caps.save.app_name,
            "library_label": caps.save.library_label,
            "name": server.name(),
            "path": str(state.path),
            "library_id": state.library_id,
            "holder": server.lock.describe(),
            "dirty": server.adapter.dirty,
            "pending_edits": server.pending_edits(client_id),
            "rev": server.rev,
        }

    @app.post("/v1/session/acquire")
    def acquire(body: dict = Body(...)) -> dict:
        client_id = str(body.get("client_id") or "")
        machine = str(body.get("machine") or "another computer")
        if not client_id:
            raise HTTPException(400, "client_id is required")
        try:
            holder = server.lock.acquire(client_id, machine, takeover=bool(body.get("takeover")))
        except InUse as ex:
            h = ex.holder
            raise HTTPException(409, {"code": "in_use", "machine": h.machine,
                                      "since": h.since, "batch": h.batch})
        return {
            "token": holder.token,
            "lease": server.lock.lease,
            "heartbeat": rp.HEARTBEAT_SECONDS,
            "pending_edits": server.pending_edits(client_id),
            "rev": server.rev,
        }

    @app.post("/v1/session/heartbeat")
    def heartbeat(body: dict = Body(default={}),
                  token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        try:
            server.lock.heartbeat(token, body.get("batch"))
        except Lost as ex:
            raise HTTPException(423, {"code": "taken_over", "message": str(ex),
                                      "holder": server.lock.describe()})
        return {"rev": server.rev, "dirty": server.adapter.dirty}

    @app.post("/v1/session/release")
    def release(token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        server.lock.release(token)
        return {"status": "released"}

    # ---- the protocol ------------------------------------------------------------
    @app.post("/v1/rpc/{name}")
    def rpc(name: str, body: dict = Body(default={}),
            token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        method = rp.RPC.get(name)
        if method is None:
            raise HTTPException(404, f"No such method: {name}")
        holder = session(token) if method.mutating else None
        try:
            args = rp.load_args(name, body.get("args") or {})
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        adapter = server.adapter
        changed: list = []
        removed: list[str] = []
        reset = False
        if name == "capabilities":
            value = server.capabilities()
        elif name == "add_tracks":
            with server.state.mutation:
                value = adapter.add_tracks([_new_track(server, w) for w in args["items"]])
            changed = [t for i in value if (t := adapter.track(i)) is not None]
        elif method.prop:
            value = getattr(adapter, name)
        else:
            value = getattr(adapter, name)(**args)
        if method.mutating:
            if name == "remove_tracks":
                removed = list(args["track_ids"])
            elif "track_id" in args:
                track = adapter.track(args["track_id"])
                if track is None:
                    removed = [args["track_id"]]
                else:
                    changed = [track]
            server.lock.touch(holder)
            rev = server.bump()
        else:
            rev = server.rev
        return {
            "result": rp.dump_result(name, value),
            "rev": rev,
            "changed": [t.model_dump(mode="json") for t in changed],
            "removed": removed,
            "reset": reset,
        }

    # ---- bytes -------------------------------------------------------------------
    @app.get("/v1/audio")
    def audio(track_id: str):
        path = server.adapter.audio_path(track_id)
        if path is None or not Path(path).is_file():
            raise HTTPException(404, "The audio file is missing")
        st = Path(path).stat()
        media = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers={
            "X-Konduktor-Size": str(st.st_size),
            "X-Konduktor-Mtime-Ns": str(st.st_mtime_ns),
        })

    @app.get("/v1/art")
    def art(track_id: str):
        found = server.adapter.cover_art(track_id)
        if not found:
            raise HTTPException(404, "No cover art")
        data, mime = found
        return Response(content=data, media_type=mime)

    @app.put("/v1/art")
    async def put_art(request: Request, track_id: str, mime: str,
                      token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        holder = session(token)
        data = await request.body()
        server.adapter.set_cover_art(track_id, data, mime)
        server.lock.touch(holder)
        track = server.adapter.track(track_id)
        return {"rev": server.bump(), "changed": [track.model_dump(mode="json")] if track else []}

    @app.post("/v1/upload")
    async def upload(request: Request, dir: str, name: str,
                     token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        """Receive one audio file into `dir` (created if needed) under the content
        folder. Never overwrites: a name already taken gets `-2`, `-3`, … as a
        local import's copy does. The partial is renamed in only when complete,
        so a cut connection leaves no half file under a real name."""
        holder = session(token)
        folder = server.confine(dir)
        clean = _safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f".{clean}.{secrets.token_hex(4)}{PARTIAL_SUFFIX}"
        try:
            with open(partial, "wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
            target = _publish(partial, folder / clean)
        finally:
            partial.unlink(missing_ok=True)
        holder.uploads.add(str(target))
        st = target.stat()
        # A platform that analyses what it adds measures on its own clock, read
        # from the file's header — which only the server can read. The client
        # measures with this (`PreparedAnalysis`); None: nothing to measure.
        lead = getattr(server.adapter, "analysis_lead", None)
        return {"path": str(target), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "analysis_lead": lead(target) if lead else None}

    @app.delete("/v1/upload")
    def delete_upload(path: str, token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        """Remove a file THIS session uploaded and the library does not name —
        how a cancelled or failed import cleans up after itself."""
        holder = session(token)
        target = server.confine(path)
        if str(target) not in holder.uploads:
            raise HTTPException(403, "Only a file this session uploaded can be removed")
        adapter = server.adapter
        named = {f.key for f in adapter.audio_facts([t.id for t in adapter.tracks]).values()}
        if str(target) in named:
            raise HTTPException(409, "The library names that file")
        target.unlink(missing_ok=True)
        holder.uploads.discard(str(target))
        return {"status": "deleted"}

    # ---- the content folder ------------------------------------------------------
    @app.get("/v1/fs/list")
    def fs_list(path: str | None = None) -> dict:
        folder = server.confine(path)
        if not folder.is_dir():
            raise HTTPException(404, f"Not a folder: {folder}")
        entries = []
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as ex:
            raise HTTPException(400, f"Cannot list {folder}: {ex}")
        for child in children:
            if child.name.startswith(".") or is_os_housekeeping(child.name):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": is_dir})
        root = server.config.content
        return {
            "path": str(folder),
            "parent": None if folder == root else str(folder.parent),
            "root": str(root),
            "entries": entries,
        }

    @app.get("/v1/fs/space")
    def fs_space(path: str | None = None) -> dict:
        folder = server.confine(path)
        probe = folder
        while not probe.exists() and probe != server.config.content:
            probe = probe.parent
        return {"free": shutil.disk_usage(probe).free}

    # ---- save / discard / history ------------------------------------------------
    @app.post("/v1/save")
    def save(token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        session(token)
        adapter = server.adapter
        if not adapter.dirty:
            return {"saved": False, "commit": None, "summary": "",
                    "tag_results": [], "playlists": adapter.playlist_count(), "rev": server.rev}
        outcome, commit = server.state.save()
        server.lock.clear_editor()
        return {
            "saved": True,
            "commit": commit,
            "summary": outcome.summary,
            "tag_results": [r.__dict__ for r in outcome.tag_results],
            "playlists": adapter.playlist_count(),
            "rev": server.bump(),
        }

    @app.post("/v1/discard")
    def discard(token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        session(token)
        server.state.discard()
        server._configure(server.adapter)
        server.lock.clear_editor()
        return {"rev": server.bump()}

    @app.get("/v1/history")
    def history() -> list[dict]:
        return [e.__dict__ for e in server.state.services.history()]

    @app.post("/v1/history/{commit_id}/restore")
    def restore(commit_id: str, token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        session(token)
        if not server.state.restore_version(commit_id):
            raise HTTPException(404, f"Version not found: {commit_id}")
        server.lock.clear_editor()
        return {"rev": server.bump()}

    @app.delete("/v1/history")
    def clear_history(token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        session(token)
        server.state.services.clear_history()
        return {"status": "cleared"}

    # ---- export sets -------------------------------------------------------------
    @app.get("/v1/export-sets")
    def get_export_sets() -> dict:
        return exports._read_doc(server.state.library_id)

    @app.put("/v1/export-sets")
    def put_export_sets(doc: dict = Body(...),
                        token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        session(token)
        if not isinstance(doc.get("sets"), dict):
            raise HTTPException(400, "Expected {'sets': {...}}")
        exports._write_doc(server.state.library_id,
                           {"library_id": server.state.library_id, "sets": doc["sets"]})
        return {"status": "saved"}

    # ---- Convert to Stems: the library side ---------------------------------------
    # The SEPARATION runs on the computer that opened the library (the server
    # never decodes audio); what lives here is everything about the FILES and the
    # library: where each stem file goes, what is skipped and why, the verified
    # files it uploads (kept as leftovers until the batch's end step, so a
    # takeover mid-batch loses nothing — the next run reuses them), and the end
    # step itself, under the library's mutation lock, with the server's ledger.
    def stem_options(body: dict):
        from ..stems import convert

        destination = body.get("destination")
        if body.get("mode") == "destination":
            if not destination:
                raise HTTPException(400, "Choose a destination folder")
            destination = server.confine(destination)
        return convert.Options(
            mode=body.get("mode") or "replace", destination=Path(destination) if destination else None,
            collection=body.get("collection") or "repoint", playlist_id=body.get("playlist_id"),
            new_playlist=body.get("new_playlist"),
        )

    @app.post("/v1/stems/plan")
    def stems_plan(body: dict = Body(...)) -> dict:
        from ..stems import convert

        adapter = server.adapter
        caps = adapter.capabilities()
        if not caps.tracks.stem_convertible:
            raise HTTPException(422, "This library cannot hold stem files")
        try:
            planned = convert.plan(adapter, list(body.get("track_ids") or []), stem_options(body),
                                   server.state.pending)
        except convert.ConvertError as ex:
            raise HTTPException(400, str(ex))
        out = planned.as_dict()
        out["items"] = [{
            "track_id": p.track_id, "title": p.title, "source": str(p.source), "target": str(p.target),
            "seconds": p.seconds, "source_size": p.source.stat().st_size if p.source.exists() else 0,
            "reuse": p.reuse["written"] if p.reuse else None,
        } for p in planned.items]
        return out

    def stem_item(track_id: str, target: str) -> tuple[Path, Path]:
        """The source and target a stem upload / publish names, checked against
        the library rather than trusted: a target outside the content folder, or
        one not named after its own track's file, is refused."""
        from ..core import stem_file as sf

        source = server.adapter.audio_path(track_id)
        if source is None or not Path(source).is_file():
            raise HTTPException(404, "The track's audio file is missing")
        dest = server.confine(target)
        if dest.name != sf.stem_target_name(Path(source).name):
            raise HTTPException(400, f"{dest.name} is not the stem file of {Path(source).name}")
        return Path(source), dest

    @app.put("/v1/stems/stage")
    async def stems_stage(request: Request, track_id: str, target: str, sha256: str,
                          bit_rate: int, duration: float,
                          token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        import hashlib

        from ..stems.pending import partial_name, source_facts

        session(token)
        source, dest = stem_item(track_id, target)
        if dest.exists():
            raise HTTPException(409, f"A stem file already exists: {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = partial_name(dest)
        incoming = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}{PARTIAL_SUFFIX}")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(incoming, "wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != sha256.lower():
                raise HTTPException(400, "The stem file arrived damaged (checksum mismatch)")
            os.replace(incoming, partial)
        finally:
            incoming.unlink(missing_ok=True)
        pending = server.state.pending
        with pending._lock:
            pending.leftovers = [lo for lo in pending.leftovers if lo["partial"] != str(partial)]
            pending.leftovers.append({
                "source": str(source), "facts": source_facts(source), "partial": str(partial),
                "written": {"bit_rate": bit_rate, "duration": duration, "size": size},
            })
            pending._write()
        return {"partial": str(partial), "size": size}

    @app.post("/v1/stems/abandon")
    def stems_abandon(body: dict = Body(...),
                      token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        """A cancelled batch: delete the files it uploaded (a cancel deletes
        only new files, as on this computer)."""
        from ..stems.pending import partial_name

        session(token)
        pending = server.state.pending
        wanted = {str(partial_name(server.confine(t))) for t in body.get("targets") or []}
        for lo in [lo for lo in pending.leftovers if lo["partial"] in wanted]:
            pending.forget_leftover(lo, delete=True)
        return {"status": "abandoned"}

    @app.post("/v1/stems/publish")
    def stems_publish(body: dict = Body(...),
                      token: str | None = Header(None, alias=rp.SESSION_HEADER)) -> dict:
        """The batch's END STEP, here where the files are: publish each staged
        stem file, park originals (Replace), swap every entry at once."""
        from ..core import stem_file as sf
        from ..stems import convert
        from ..stems.pending import partial_name

        holder = session(token)
        opts = stem_options(body.get("options") or {})
        pending = server.state.pending
        result = {"converted": [], "renamed": {}, "added": {}, "failed": [], "clamped": {}}
        with server.state.mutation:
            finished = []
            for entry in body.get("items") or []:
                source, dest = stem_item(entry["track_id"], entry["target"])
                partial = partial_name(dest)
                lo = next((x for x in pending.leftovers if x["partial"] == str(partial)), None)
                track = server.adapter.track(entry["track_id"])
                title = convert._title(track)
                if lo is None or not partial.is_file():
                    result["failed"].append({"track_id": entry["track_id"], "title": title,
                                             "reason": "the converted file never reached the server"})
                    continue
                written = lo["written"]
                pitem = pending.add({"track_id": entry["track_id"], "mode": opts.collection,
                                     "source": str(source), "stem": str(dest), "state": "converted",
                                     "facts": lo.get("facts"), "written": written})
                pending.forget_leftover(lo, delete=False)
                planned = convert.Planned(entry["track_id"], title, source, dest, float(written["duration"]))
                finished.append((planned, pitem, sf.Written(path=partial, bit_rate=int(written["bit_rate"]),
                                                            duration=float(written["duration"]),
                                                            size=int(written["size"]), tags={})))
            convert._end_step(server.adapter, finished, opts, pending, result)
        server.lock.touch(holder)
        result["rev"] = server.bump()
        return result

    @app.get("/v1/stems/pending")
    def stems_pending() -> dict:
        pending = server.state.pending
        return pending.summary() if pending is not None else {"tracks": 0, "parked": 0, "bytes": 0}

    return app


def _safe_name(name: str) -> str:
    clean = os.path.basename((name or "").replace("\\", "/")).strip()
    if not clean or clean in (".", "..") or clean.startswith("."):
        raise HTTPException(400, f"Not a usable file name: {name!r}")
    return clean


def _publish(partial: Path, wanted: Path) -> Path:
    """Rename `partial` to `wanted`, or `wanted-2`… — never over an existing file
    (a hard link fails rather than replaces)."""
    stem, suffix = wanted.stem, wanted.suffix
    # `.stem.m4a` keeps its double suffix together.
    if stem.endswith(".stem"):
        stem, suffix = stem[: -len(".stem")], ".stem" + suffix
    n = 1
    while True:
        target = wanted if n == 1 else wanted.with_name(f"{stem}-{n}{suffix}")
        try:
            os.link(partial, target)
            return target
        except FileExistsError:
            n += 1


def _new_track(server: Server, wire: rp.WireNewTrack) -> NewTrack:
    import base64

    path = server.confine(wire.audio_path)
    art = (base64.b64decode(wire.art.data), wire.art.mime) if wire.art else None
    return NewTrack(track=wire.track, audio_path=path, cues=wire.cues, art=art,
                    analysis=rp.unpack_analysis(wire.analysis))


# ---- entry points ------------------------------------------------------------------


def create_from_env() -> FastAPI:
    """`uvicorn --factory konduktor.server.app:create_from_env`."""
    from .config import from_env

    server = Server(from_env())
    server.open()
    log.info("Serving %s (%s) to remote clients", server.config.library, server.config.platform)
    return create_app(server)
