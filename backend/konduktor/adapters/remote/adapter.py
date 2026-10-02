"""`RemoteAdapter`: a library held by a Konduktor server, as a `LibraryAdapter`.

Everything above the adapter layer — routes, export, import, batches, the UI —
works on a remote library unchanged, because this IS the adapter: the server
holds the native model (`konduktor.server`), and every command travels to it
over `remote_protocol`.

  * **The projection is mirrored here.** The server's tracks are fetched once
    into a local `TrackIndex`, and each command's answer carries the tracks it
    changed — so filtering, sorting, facets and stats are as instant as on a
    local library, and use the SAME query code (`core/query.py`). Every answer
    carries the server's revision; a gap (an answer out of order, a reconnect)
    re-fetches the whole projection rather than guessing.
  * **`audio_path` downloads.** It must return a readable file on THIS
    computer, so it fetches the track into the shared cache (`cache.py`).
    Anything that only needs to know WHICH file a track is asks `audio_facts`,
    which never downloads.
  * **Read-only when the session is not ours** — taken over, or the server out
    of reach — through `capabilities()`, so the UI gates itself as it does for
    any read-only library. A command the server refuses for that reason (423)
    turns the session `taken_over` on the spot, not at the next heartbeat.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

from ... import remote_protocol as rp
from ...core.adapter import (
    AudioFacts,
    FileTagResult,
    SaveOutcome,
    Unavailable,
    Unsupported,
)
from ...core.capabilities import Capabilities
from ...core.query import TrackIndex
from .cache import RemoteAudioCache, StaleFacts
from .cache import shared as shared_cache
from .client import RemoteClient, SessionLost
from .session import RemoteSession

_NOT_HERE = "A remote library's paths are the server's configuration"


class RemoteAdapter:
    def __init__(self, client: RemoteClient, session: RemoteSession, hello: dict, *,
                 cache: RemoteAudioCache | None = None):
        self.platform = hello["platform"]
        self.path = Path(hello.get("path") or "")
        self.hello = hello
        self.client = client
        self.session = session
        self.cache = cache or shared_cache()
        self._index = TrackIndex()
        self._lock = threading.RLock()
        self._rev: int | None = None
        self._caps: tuple[int, Capabilities] | None = None
        self._facts: dict[str, AudioFacts] = {}
        #: Server path of an upload → (the local file it came from, its analysis lead).
        self._uploads: dict[str, tuple[Path, float | None]] = {}
        self._dirty = bool(hello.get("dirty"))
        self._load()

    # ---- following the server ---------------------------------------------------
    def _load(self) -> None:
        envelope = self.client.rpc("tracks")
        with self._lock:
            self._index.rebuild(list(envelope["result"]))
            self._rev = envelope["rev"]
            self._facts.clear()
            self._caps = None

    def _apply(self, envelope: dict) -> None:
        """Fold a command's answer into the mirror — or re-fetch on any gap."""
        with self._lock:
            rev = envelope.get("rev")
            if envelope.get("reset") or self._rev is None or rev != self._rev + 1:
                in_order = False
            else:
                in_order = True
                from ...core.model import Track

                removed = set(envelope.get("removed") or [])
                if removed:
                    self._index.rebuild([t for t in self._index.tracks if t.id not in removed])
                for raw in envelope.get("changed") or []:
                    track = Track.model_validate(raw)
                    self._index.add(track)
                    self._facts.pop(track.id, None)
                for track_id in removed:
                    self._facts.pop(track_id, None)
                self._rev = rev
        if not in_order:
            self._load()

    def _call(self, method: str, /, **args):
        try:
            envelope = self.client.rpc(method, **args)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        if rp.RPC[method].mutating:
            self.session.reached()
            self._apply(envelope)
        return envelope["result"]

    def refresh(self) -> None:
        """Re-read everything from the server (after a restore, a reconnect)."""
        self._load()

    # ---- identity -----------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        with self._lock:
            cached = self._caps
        if cached is None or cached[0] != self._rev:
            try:
                caps = self._call("capabilities")
            except Unavailable:
                # Out of reach: the last answer, read-only (below) — the UI
                # must still render, and gate every edit.
                if cached is None:
                    raise
                caps = cached[1]
            else:
                with self._lock:
                    self._caps = (self._rev, caps)
        else:
            caps = cached[1]
        if self.session.writable:
            return caps
        return caps.model_copy(update={"writable": False, "readonly_cause": self.session.readonly_cause})

    def reload(self) -> None:
        # The server is what holds (and drops) the edits — `AppState.discard`
        # asks it to discard first; this is the mirror following.
        self._load()

    # ---- read projection (the mirror) -------------------------------------------
    @property
    def tracks(self):
        return self._index.tracks

    def track(self, track_id: str):
        return self._index.get(track_id)

    def query_tracks(self, **kw):
        return self._index.query_tracks(**kw)

    def facets(self):
        return self._index.facets()

    def stats(self, playlist_count: int):
        return self._index.stats(playlist_count)

    # ---- read projection (the server) -------------------------------------------
    def playlist_tree(self):
        return self._call("playlist_tree")

    def playlist_count(self) -> int:
        return self._call("playlist_count")

    def playlist_entries(self, node_id: str):
        return self._call("playlist_entries", node_id=node_id)

    def playlist_tracks(self, node_id: str):
        entries = self.playlist_entries(node_id)
        if entries is None:
            return None
        return [t for k in entries if (t := self._index.get(k)) is not None]

    def track_cues(self, track_id: str):
        return self._call("track_cues", track_id=track_id)

    @property
    def dirty(self) -> bool:
        try:
            self._dirty = self._call("dirty")
        except Unavailable:
            pass  # out of reach: what it was
        return self._dirty

    # ---- commands -------------------------------------------------------------------
    def create_playlist(self, name, parent_id=None):
        return self._call("create_playlist", name=name, parent_id=parent_id)

    def create_folder(self, name, parent_id=None):
        return self._call("create_folder", name=name, parent_id=parent_id)

    def rename_playlist(self, node_id, name):
        return self._call("rename_playlist", node_id=node_id, name=name)

    def delete_playlist(self, node_id):
        return self._call("delete_playlist", node_id=node_id)

    def set_playlist_entries(self, node_id, track_ids):
        return self._call("set_playlist_entries", node_id=node_id, track_ids=list(track_ids))

    def remove_tracks(self, track_ids):
        return self._call("remove_tracks", track_ids=list(track_ids))

    def set_track_metadata(self, track_id, fields):
        return self._call("set_track_metadata", track_id=track_id, fields=fields)

    def set_key(self, track_id, wheel, mode):
        return self._call("set_key", track_id=track_id, wheel=wheel, mode=mode)

    def set_cue(self, track_id, *, slot, start_sec, cue_type, length_sec=0.0, role="hotcue", name=None):
        return self._call("set_cue", track_id=track_id, slot=slot, start_sec=start_sec,
                          cue_type=cue_type, length_sec=length_sec, role=role, name=name)

    def set_cue_type(self, track_id, slot, cue_type):
        return self._call("set_cue_type", track_id=track_id, slot=slot, cue_type=cue_type)

    def set_cue_color(self, track_id, slot, color):
        return self._call("set_cue_color", track_id=track_id, slot=slot, color=color)

    def delete_cue(self, track_id, slot):
        return self._call("delete_cue", track_id=track_id, slot=slot)

    def place_cues(self, track_id, cues, *, overwrite=False):
        return self._call("place_cues", track_id=track_id, cues=list(cues), overwrite=overwrite)

    def add_grid_marker(self, track_id, start_sec, bpm=None):
        return self._call("add_grid_marker", track_id=track_id, start_sec=start_sec, bpm=bpm)

    def move_grid_marker(self, track_id, index, start_sec):
        return self._call("move_grid_marker", track_id=track_id, index=index, start_sec=start_sec)

    def set_grid_marker_bpm(self, track_id, index, bpm):
        return self._call("set_grid_marker_bpm", track_id=track_id, index=index, bpm=bpm)

    def delete_grid_marker(self, track_id, index):
        return self._call("delete_grid_marker", track_id=track_id, index=index)

    def replace_grid(self, track_id, markers):
        return self._call("replace_grid", track_id=track_id, markers=_pairs(markers))

    def set_analysed_grid(self, track_id, markers):
        return self._call("set_analysed_grid", track_id=track_id, markers=_pairs(markers))

    def delete_grid(self, track_id):
        return self._call("delete_grid", track_id=track_id)

    def set_grid_lock(self, track_id, locked):
        return self._call("set_grid_lock", track_id=track_id, locked=locked)

    # ---- adding tracks ------------------------------------------------------------------
    def upload_audio(self, source: Path, folder: str, name: str, *, progress=None) -> Path:
        """Send `source` into `folder` on the server; the path it landed at (a
        taken name gets `-2`…, as a local copy does). What a local import's
        file copy becomes when the library's destination is the server's."""
        try:
            landed = self.client.upload(Path(source), folder, name, progress=progress)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        self._uploads[landed["path"]] = (Path(source), landed.get("analysis_lead"))
        return Path(landed["path"])

    def free_space(self, path: str) -> int:
        """Free space on the SERVER's disk at `path` (an import's destination)."""
        return self.client.fs_space(path)

    def delete_upload(self, path) -> None:
        """Undo `upload_audio` (a cancelled or failed import)."""
        self._uploads.pop(str(path), None)
        self.client.delete_upload(str(path))

    def add_tracks(self, items, *, checkpoint=None):
        """Add uploaded files. Where the platform analyses what it adds (the
        server said so with an analysis lead), the analysis is done HERE, on
        the copy this computer uploaded — the server never decodes audio."""
        from ...core import add_analysis

        wire = []
        total = len(items)
        for n, item in enumerate(items, start=1):
            key = str(item.audio_path)
            source, lead = self._uploads.get(key, (None, None))
            analysis = item.analysis
            if analysis is None and lead is not None and source is not None:
                if checkpoint is not None:
                    checkpoint(f"Analysing {item.track.title or source.name} ({n}/{total})", step=n, of=total)
                has_grid = bool(item.cues and item.cues.grid_markers)
                analysis = add_analysis.prepare(source, lead=lead, detect=not has_grid,
                                                detect_key=item.track.key_wheel is None)
            art = rp.WireArt(data=base64.b64encode(item.art[0]).decode("ascii"), mime=item.art[1]) \
                if item.art else None
            wire.append(rp.WireNewTrack(track=item.track, audio_path=key, cues=item.cues, art=art,
                                        analysis=rp.pack_analysis(analysis)))
        if checkpoint is not None:
            checkpoint(f"Adding {total} track{'' if total == 1 else 's'} on the server",
                       step=total, of=total)
        try:
            envelope = self.client.rpc("add_tracks", items=wire, timeout=None)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        self._apply(envelope)
        for item in items:
            self._uploads.pop(str(item.audio_path), None)
        return envelope["result"]

    def apply_stem_swaps(self, swaps, *, add_to_playlist=None):
        raise Unsupported("A remote library's stem files are swapped in on the server")

    # ---- Convert to Stems (the library side lives on the server) ---------------------
    # `stems/remote_convert.py` drives these: the server plans and swaps, this
    # computer separates. See the server's stem routes for why.
    def stems_plan(self, body: dict) -> dict:
        return self.client.stems_plan(body)

    def stage_stem(self, track_id: str, target: str, local: Path, *, bit_rate: int,
                   duration: float, progress=None) -> dict:
        import hashlib

        digest = hashlib.sha256()
        with open(local, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
        try:
            return self.client.stems_stage(track_id, target, local, sha256=digest.hexdigest(),
                                           bit_rate=bit_rate, duration=duration, progress=progress)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise

    def abandon_stems(self, targets: list[str]) -> None:
        self.client.stems_abandon(targets)

    def publish_stems(self, items: list[dict], options: dict) -> dict:
        try:
            result = self.client.stems_publish(items, options)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        self._load()  # ids changed (repoint) or entries were added
        return result

    # ---- metadata / art ------------------------------------------------------------------
    def set_cover_art(self, track_id, data, mime):
        try:
            envelope = self.client.put_art(track_id, data, mime)
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        self._apply({**envelope, "removed": [], "reset": False})

    def cover_art(self, track_id):
        return self.client.art(track_id)

    # ---- audio ------------------------------------------------------------------------------
    def audio_facts(self, track_ids):
        wanted = list(dict.fromkeys(track_ids))
        with self._lock:
            missing = [t for t in wanted if t not in self._facts]
        if missing:
            found = {}
            for start in range(0, len(missing), 2000):
                found.update(self._call("audio_facts", track_ids=missing[start:start + 2000]))
            with self._lock:
                self._facts.update(found)
        with self._lock:
            return {t: self._facts[t] for t in wanted if t in self._facts}

    def audio_path(self, track_id, *, progress=None):
        """A local copy of the track's audio, downloaded if this computer has
        none of this version. None when the server has no file for it."""
        for attempt in range(2):
            facts = self.audio_facts([track_id]).get(track_id)
            if facts is None:
                return None
            cached = self.cache.get(facts)
            if cached is not None:
                return cached
            try:
                return self.cache.fetch(
                    facts, lambda partial: self.client.download(track_id, partial, progress=progress))
            except StaleFacts:
                with self._lock:
                    self._facts.pop(track_id, None)
                if attempt:
                    raise
        return None

    def cached_audio(self, track_id) -> Path | None:
        """The local copy if there already is one — never downloads."""
        facts = self.audio_facts([track_id]).get(track_id)
        return self.cache.get(facts) if facts else None

    # ---- paths: the server's business ------------------------------------------------
    def set_path_mapping(self, mapping):
        raise Unsupported(_NOT_HERE)

    def set_session_mappings(self, mappings):
        raise Unsupported(_NOT_HERE)

    def unresolved_path_groups(self):
        return []

    def path_prefix_suggestions(self):
        raise Unsupported(_NOT_HERE)

    def remap_preview(self, mapping):
        raise Unsupported(_NOT_HERE)

    def remap_locations(self, mapping):
        raise Unsupported(_NOT_HERE)

    # ---- save ----------------------------------------------------------------------------
    def save(self):
        try:
            result = self.client.save()
        except SessionLost as ex:
            self.session.lost(ex.holder)
            raise
        outcome = SaveOutcome(
            summary=result.get("summary") or "",
            snapshot=None,
            tag_results=[FileTagResult(**r) for r in result.get("tag_results") or []],
        )
        # Versioned on the server; `RemoteServices.commit_save` hands it on.
        outcome.commit = result.get("commit")
        self._load()
        return outcome

    def snapshot(self) -> bytes:
        raise Unsupported("A remote library's version history is kept on the server")

    def close(self) -> None:
        self.session.stop()
        self.client.release()
        self.client.close()


def _pairs(markers) -> list[tuple[float, float]]:
    out = []
    for m in markers:
        if hasattr(m, "start"):
            out.append((float(m.start), float(m.bpm)))
        else:
            start, bpm = m
            out.append((float(start), float(bpm)))
    return out
