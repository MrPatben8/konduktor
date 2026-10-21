"""Pushing an export set into the DJ apps' own libraries on THIS computer.

An export set of kind "computer" is not written into a folder: its tracks and
playlists go straight into the Traktor collection and/or rekordbox library this
computer's DJ apps use — the collection a DJ keeps on a server, prepared there,
and pushed to the laptop that plays the gig. Decided 2026-10-20
(`.claude/discussions/discuss-export-to-local-library-2026-10-20.md`).

## Two modes, chosen on the set

**Merge** (the default) adds and updates ONLY what this set put there: the
user's own tracks and playlists are never touched, and several sets can push
into one library side by side. Each push brings the set's tracks up to date
from the source (the source wins: a cue added on the laptop to a pushed track
is replaced), rebuilds the set's playlists inside ONE folder named after the
set, and removes from the library what left the set — unless another set still
holds it, or rekordbox refuses (a track in its History, Sampler, …), in which
case it stays, is reported, and the next push tries again.

**Overwrite** replaces the whole library with this set: the export writers
build a fresh one in app-data and it is swapped in, as a user would swap an
exported `collection.nml` / `master.db` by hand. Every other set that had been
pushed into that library is gone from it afterwards.

## One shared audio folder

Every pushed file lives under one folder (default `~/Music/Konduktor`), at its
SOURCE path minus the anchor — stable across sets and pushes, so a track in
two sets is one file and one entry in the DJ app. (`common_dir_prefix`, which
an export mirrors from, moves whenever a set changes; in a shared folder that
would leave the same track at two paths.) Being shared, the files can have only
one level, so Normalise lives with the folder in the app's settings, not on a
set. A file is deleted when no set in any library still holds it.

## What Konduktor remembers

A ledger in app-data (`local_push/ledger.json`, Konduktor's own data, never
written into a library): per library (by `library_id`) and set, which local
track each source track became, the copy it uses, a hash of the prep last
pushed (an unchanged track costs nothing on a re-push), and the set's folder;
per audio folder, the facts of each copy (the export manifest's record), so an
unchanged file is never copied again.

## Safety

A push refuses while the DJ app runs (Traktor writes its collection back on
quit, undoing the push; rekordbox holds its database open), checked again
just before each save. It never writes the library open in Konduktor itself,
nor a cloud-synced rekordbox library. Before writing, a Traktor collection
gets a version-history baseline and each push is committed to it (restored by
opening that collection in Konduktor); a rekordbox library is copied to
app-data, the last 10 kept. Audio is copied FIRST and the library written
LAST: a cancel before any save leaves every library untouched and removes the
files this run placed. A copy that REPLACES a file a library already plays
waits under a temporary name until a library has been saved.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, exports, history, library_id, paths, prefs
from .core import apps_running, registry
from .core import export as core_export
from .core.adapter import AudioChange, NewTrack, Unsupported
from .core.analysis_cache import AnalysisCache
from .core.export import ExportPayload, ExportPlaylist, ExportTrack, write_playlist_tree
from .core.level_plan import shifted
from .exporter import (
    PARTIAL_SUFFIX,
    ExportPlan,
    Level,
    PlannedTrack,
    _cover,
    _fingerprint,
    _folder_paths,
    _locked,
    _measure_missing,
    _record,
    assign_reuse,
    set_level,
    write_copy,
)
from .importer import SPACE_HEADROOM, _existing_files, _key, free_bytes
from .jobs import JobHandle

log = logging.getLogger(__name__)

#: The DJ apps a set can be pushed into, in the order they are offered.
PLATFORMS = ("traktor", "rekordbox")
JOB_KIND = "local-push"
_BACKUPS_KEPT = 10

#: Why a library cannot be pushed into. Facts; the UI words them.
NOT_FOUND = "not_found"
OPEN_IN_KONDUKTOR = "open_in_konduktor"
CLOUD_SYNCED = "cloud_synced"
UNREADABLE = "unreadable"
#: Why a push cannot run.
BLOCKED_NO_TARGET = "no_target"
#: Every library the set names is missing on THIS computer.
BLOCKED_NONE_HERE = "no_library_here"
BLOCKED_EMPTY = "nothing_to_push"
BLOCKED_UNAVAILABLE = "target_unavailable"
BLOCKED_RUNNING = "app_running"

_LOCK = threading.RLock()


class PushRefused(Exception):
    """A push that must not go ahead (a DJ app started, a library became
    unwritable). The message is for the user."""


# ---- where things are ---------------------------------------------------------


def _local_dir() -> Path:
    return paths.app_data_dir() / "local_push"


def audio_root() -> Path:
    """The shared folder pushed audio lives in: the setting, else the user's
    Music folder (platformdirs — hardcoding `~/Music` is wrong somewhere on
    every OS) + `Konduktor`."""
    raw = prefs.load_prefs().get("localAudioRoot")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    import platformdirs

    return Path(platformdirs.user_music_dir()) / "Konduktor"


def level() -> Level | None:
    """The shared folder's Normalise level, or None when it is off. The
    measure / target / ceiling default to Normalize's app settings."""
    p = prefs.load_prefs()
    if not p.get("localNormalise"):
        return None
    raw = p.get("localLevel") if isinstance(p.get("localLevel"), dict) else {}
    try:
        measure = raw.get("measure", p.get("levelMeasure", "lufs"))
        return Level(measure=measure if measure in ("lufs", "rms") else "lufs",
                     target=float(raw.get("target", p.get("levelTarget", -9.0))),
                     ceiling=float(raw.get("ceiling", p.get("levelCeiling", -1.0))))
    except (TypeError, ValueError):
        return Level()


def _override(platform: str) -> Path | None:
    raw = prefs.load_prefs().get("localLibraries")
    value = raw.get(platform) if isinstance(raw, dict) else None
    return Path(value).expanduser() if isinstance(value, str) and value.strip() else None


def library_path(platform: str) -> tuple[Path | None, bool]:
    """(the library a push into `platform` writes, whether it was DETECTED).
    The setting wins; else the platform's own detection, best first (Traktor:
    the newest `Traktor x.y` folder)."""
    chosen = _override(platform)
    if chosen is not None:
        return chosen, False
    for found in registry.detect_for(platform):
        raw = found.get("path") if isinstance(found, dict) else getattr(found, "path", None)
        if raw and Path(raw).is_file():
            return Path(raw), True
    return None, True


def _same(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


# Opening a library to ask whether it can be written costs a parse; the answer
# only changes with the file, so it is kept per path + mtime. Only a platform
# whose library can be read-only is opened for it: rekordbox (cloud sync). A
# Traktor collection is always writable, and parsing 8,000 entries to hear so
# would hold up the dialog that asks.
_MAY_BE_READ_ONLY = {"rekordbox"}
_WRITABLE: dict[tuple[str, int], str | None] = {}


def _unwritable(path: Path, platform: str) -> str | None:
    """Why `platform`'s library at `path` cannot be written, or None."""
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return NOT_FOUND
    if stamp in _WRITABLE:
        return _WRITABLE[stamp]
    reason: str | None
    adapter = None
    try:
        driver = registry.driver_for(path)   # the cheap probe: is it that format at all?
        if driver.platform != platform:
            reason = UNREADABLE   # e.g. a master.db chosen as the Traktor collection
        elif driver.platform not in _MAY_BE_READ_ONLY:
            reason = None
        else:
            adapter = driver.open(path)
            caps = adapter.capabilities()
            if caps.writable:
                reason = None
            else:
                reason = CLOUD_SYNCED if caps.readonly_cause == "cloud_synced" else UNREADABLE
    except Exception as ex:  # noqa: BLE001 — anything that will not open is not a target
        log.warning("cannot open %s to push into: %s", path, ex)
        reason = UNREADABLE
    finally:
        _close(adapter)
    _WRITABLE[stamp] = reason
    return reason


def _close(adapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            log.warning("closing a pushed-into library failed", exc_info=True)


def _display_name(platform: str) -> str:
    try:
        return registry.driver_by_platform(platform).display_name
    except registry.LibraryNotSupported:
        return platform


@dataclass
class LocalLibrary:
    """One DJ app's library on this computer, as a push target."""

    platform: str
    name: str
    path: str | None
    detected: bool
    #: Why it cannot be pushed into (`NOT_FOUND`, `OPEN_IN_KONDUKTOR`,
    #: `CLOUD_SYNCED`, `UNREADABLE`), or None.
    blocked: str | None = None
    running: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def local_libraries(open_path: Path | None, platforms=PLATFORMS) -> list[LocalLibrary]:
    """The push targets this computer has, each with why it cannot be used."""
    running = apps_running.running(list(platforms))
    out = []
    for platform in platforms:
        path, detected = library_path(platform)
        lib = LocalLibrary(platform=platform, name=_display_name(platform),
                           path=str(path) if path else None, detected=detected,
                           running=platform in running)
        if path is None or not path.is_file():
            lib.blocked = NOT_FOUND
        elif _same(path, open_path):
            # Its edits are the open session's; a push would race them.
            lib.blocked = OPEN_IN_KONDUKTOR
        else:
            lib.blocked = _unwritable(path, platform)
        out.append(lib)
    return out


# ---- the ledger ----------------------------------------------------------------


def _ledger_path() -> Path:
    return _local_dir() / "ledger.json"


def _read_ledger() -> dict:
    data = paths.read_json(_ledger_path(), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("libraries", {})
    data.setdefault("audio", {})
    return data


def _write_ledger(doc: dict) -> None:
    paths.write_json(_ledger_path(), doc)


def _rel_for(source: Path) -> str:
    """Where `source` sits under the shared folder: its whole path minus the
    anchor (`/Volumes/Music/a.mp3` -> `Volumes/Music/a.mp3`, `D:\\x.mp3` ->
    `D/x.mp3`). Never collides, and never moves when a set changes."""
    text = str(source).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        text = text[0] + text[2:]
    return text.lstrip("/")


def _owned(doc: dict) -> set[str]:
    """Every copy (by `rel`) some set in some library still holds."""
    out = set()
    for lib in doc["libraries"].values():
        for entry in (lib.get("sets") or {}).values():
            for rec in (entry.get("tracks") or {}).values():
                if rec.get("rel"):
                    out.add(rec["rel"])
    return out


# ---- planning -------------------------------------------------------------------


@dataclass
class TargetPlan:
    platform: str
    name: str
    path: str | None
    blocked: str | None = None
    running: bool = False
    #: Tracks this set has not pushed into this library before.
    add: int = 0
    #: Tracks it has: kept as they are, or brought up to date where changed.
    already: int = 0
    #: Tracks that left the set since the last push (removed from the library
    #: unless another set holds them, or rekordbox refuses).
    remove: int = 0
    #: Overwrite: the other sets pushed into this library, which it removes.
    wipes_sets: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        """Not on this computer, so passed over rather than blocking: a set's
        targets are shared by every computer that opens the library (a
        remote's sets live on its server), and one with only Traktor must
        still push a Traktor + rekordbox set."""
        return self.blocked == NOT_FOUND

    def as_dict(self) -> dict:
        return {**self.__dict__, "skipped": self.skipped}


@dataclass
class PushPlan:
    #: The audio, planned as an export into the shared folder: one
    #: `PlannedTrack` per track, `destination` = its copy there, `reuse` set
    #: where the copy on record is still good.
    audio: ExportPlan
    playlists: list[ExportPlaylist]
    targets: list[TargetPlan]
    mode: str = "merge"
    blocked: str | None = None
    #: The copies on record before this push, by source path (for `reuse`).
    previous: dict = field(default_factory=dict)

    @property
    def tracks(self) -> list[PlannedTrack]:
        return self.audio.tracks

    @property
    def active(self) -> list[TargetPlan]:
        """The targets this push writes: every one found on this computer."""
        return [t for t in self.targets if not t.skipped]

    def as_dict(self) -> dict:
        audio = self.audio
        free = free_bytes(audio.destination) if audio.destination else None
        need = audio.total_bytes
        counts: dict[str, int] = {}
        for t in audio.exportable:
            if t.level_outcome:
                counts[t.level_outcome] = counts.get(t.level_outcome, 0) + 1
        return {
            "mode": self.mode,
            "tracks": len(audio.tracks),
            "pushable": len(audio.exportable),
            "unchanged": audio.unchanged,
            "missing": [t.title for t in audio.tracks if t.missing],
            "playlists": [p.name for p in self.playlists],
            "total_bytes": need,
            "audio_root": str(audio.destination),
            "free_bytes": free,
            "enough_space": None if free is None else free >= need + SPACE_HEADROOM,
            "normalised": sum(1 for t in audio.exportable if t.gain_db),
            "level_counts": counts,
            "to_measure": sum(1 for t in audio.exportable if t.needs_loudness and t.loudness is None),
            "targets": [t.as_dict() for t in self.targets],
            "blocked": self.blocked,
        }


def plan(adapter, export_set, *, store=None, level: Level | None = None,
         auto_gain: bool = False, open_path: Path | None = None) -> PushPlan:
    """What a push of `export_set` would do now. Reads only — neither the
    target libraries (their state is the ledger's, checked again by the run)
    nor any audio file."""
    root = audio_root()
    built = ExportPlan(destination=root)
    doc = _read_ledger()
    records = doc["audio"].get(str(root)) or {}
    previous = {r["source"]: (root / rel, r) for rel, r in records.items()
                if isinstance(r, dict) and isinstance(r.get("source"), str)}

    resolved = exports.resolve(adapter, export_set)
    tracks = [t for t in (adapter.track(i) for i in resolved.track_ids) if t is not None]
    facts = adapter.audio_facts([t.id for t in tracks])
    wants_loudness = store is not None and (level is not None or auto_gain)
    entries = store.get([f.key for f in facts.values() if f is not None]) if wants_loudness else {}
    for track in tracks:
        fact = facts.get(track.id)
        source = Path(fact.key) if fact is not None else None
        planned = PlannedTrack(
            track_id=track.id,
            title=track.title or (source.name if source else track.id),
            source_path=source,
            destination=(root / _rel_for(source)) if source else None,
            size=fact.size if fact else 0,
            mtime_ns=fact.mtime_ns if fact else 0,
            missing=fact is None,
            needs_loudness=fact is not None and wants_loudness,
        )
        if planned.needs_loudness:
            entry = entries.get(fact.key)
            planned.loudness = entry.values if entry is not None and not entry.stale(fact) else None
        if fact is not None and level is not None:
            set_level(planned, level)
        built.tracks.append(planned)
    assign_reuse(built, previous)

    folders = _folder_paths(adapter)
    playlists = [ExportPlaylist(name=p.name, track_ids=list(p.track_ids), folders=folders.get(p.id, []))
                 for p in resolved.playlists if not p.missing and p.track_ids]

    pushable = {t.track_id for t in built.exportable}
    targets = []
    known = {lib.platform: lib for lib in local_libraries(open_path, [
        t for t in export_set.targets if t in PLATFORMS])}
    for platform in export_set.targets:
        lib = known.get(platform)
        if lib is None:
            targets.append(TargetPlan(platform=platform, name=_display_name(platform), path=None,
                                      blocked=NOT_FOUND))
            continue
        target = TargetPlan(platform=platform, name=lib.name, path=lib.path,
                            blocked=lib.blocked, running=lib.running)
        lid = library_id.read_sidecar(Path(lib.path)) if lib.path else None
        sets = (doc["libraries"].get(lid) or {}).get("sets") or {} if lid else {}
        mine = (sets.get(export_set.id) or {}).get("tracks") or {}
        target.add = sum(1 for t in pushable if t not in mine)
        target.already = sum(1 for t in pushable if t in mine)
        target.remove = sum(1 for t in mine if t not in pushable)
        if export_set.mode == "overwrite":
            target.wipes_sets = sorted(s.get("name") or "Untitled"
                                       for sid, s in sets.items() if sid != export_set.id)
        targets.append(target)

    out = PushPlan(audio=built, playlists=playlists, targets=targets, mode=export_set.mode,
                   previous=previous)
    if not targets:
        out.blocked = BLOCKED_NO_TARGET
    elif not out.active:
        out.blocked = BLOCKED_NONE_HERE
    elif not built.exportable:
        out.blocked = BLOCKED_EMPTY
    elif any(t.blocked for t in out.active):
        out.blocked = BLOCKED_UNAVAILABLE
    elif any(t.running for t in out.active):
        out.blocked = BLOCKED_RUNNING
    return out


# ---- running --------------------------------------------------------------------


def _partial(target: Path) -> Path:
    return target.with_name(target.name + PARTIAL_SUFFIX)


def _jsonable(value):
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return value


_PREP_FIELDS = ("title", "artist", "album", "genre", "label", "remixer", "producer", "mix",
                "comment", "comment2", "release_date", "rating", "key_wheel", "key_mode", "color")
_METADATA_FIELDS = ("title", "artist", "album", "genre", "label", "remixer", "producer", "mix",
                    "comment", "comment2", "release_date", "rating")


def _prep_hash(item: ExportTrack) -> str:
    """What a re-push compares: the track's own fields, its cues and grid
    (lock included) and the auto gain it would get — not the file, whose
    copy is checked by its own facts."""
    blob = {
        "track": {k: getattr(item.track, k, None) for k in _PREP_FIELDS},
        "cues": _jsonable(item.cues),
        "loudness": _jsonable(item.loudness),
        "gain_db": round(item.gain_db, 4),
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _checkpoint(handle: JobHandle):
    def checkpoint(message: str, step: int | None = None, of: int | None = None) -> None:
        handle.raise_if_cancelled()
        if step is not None and of:
            handle.progress(done=step - 1, total=of, unit="tracks", message=message)
        else:
            handle.progress(message=message)
    return checkpoint


def _refuse_if_running(platform: str) -> None:
    if apps_running.running([platform]):
        raise PushRefused(f"{_display_name(platform)} is running — quit it, then push again")


def run(adapter, export_set, built: PushPlan, handle: JobHandle, *, lock_grids: bool = True,
        store=None, level: Level | None = None, auto_gain: bool = False) -> dict:
    """Copy what changed into the shared folder, then push into each target.
    See the module docstring for the order and what a cancel leaves."""
    if built.blocked:
        raise PushRefused(f"This push cannot run: {built.blocked}")
    for target in built.active:
        _refuse_if_running(target.platform)

    root = Path(built.audio.destination)
    created: list[Path] = []        # new files this run placed (rolled back)
    waiting: list[tuple[Path, Path]] = []   # (partial, file) replacing a file in use
    written: set[str] = set()       # source ids whose copy this run wrote
    saved = False
    done = 0
    try:
        _measure_missing(adapter, built.audio, handle, store=store, level=level,
                         previous=built.previous)
        items = built.audio.exportable
        handle.progress(done=0, total=built.audio.total_bytes, unit="bytes", message="Copying…")
        for planned in items:
            handle.raise_if_cancelled()
            if planned.reuse is not None:
                continue
            target = Path(planned.destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = _partial(target)
            created.append(partial)

            def advance(n: int) -> None:
                nonlocal done
                done += n
                handle.progress(done=done)

            handle.progress(message=f"Copying {planned.title}")
            write_copy(adapter, planned, partial, handle, advance)
            written.add(planned.track_id)
            if target.exists():
                # A library may be playing this file: it is replaced only
                # once a library has been saved to match.
                waiting.append((partial, target))
            else:
                os.replace(partial, target)
                created.remove(partial)
                created.append(target)

        cache = AnalysisCache(_local_dir() / "analysis")
        payload_tracks = []
        for planned in items:
            handle.raise_if_cancelled()
            payload_tracks.append(ExportTrack(
                track=adapter.track(planned.track_id),
                destination=Path(planned.destination),
                cues=_locked(adapter.track_cues(planned.track_id), lock_grids),
                art=_cover(adapter, planned.track_id),
                fingerprint=_fingerprint(planned),
                analysis_cache=cache,
                loudness=(shifted(planned.loudness, planned.gain_db)
                          if auto_gain and planned.loudness is not None else None),
                gain_db=planned.gain_db,
            ))
        payload = ExportPayload(name=export_set.name, root_folder=True, tracks=payload_tracks,
                                playlists=built.playlists, checkpoint=_checkpoint(handle))

        results = []
        for target in built.active:
            handle.raise_if_cancelled()
            handle.progress(done=0, total=0, unit="tracks",
                            message=f"Pushing into {target.name}…")
            push = _overwrite if export_set.mode == "overwrite" else _merge
            results.append(push(target, export_set, payload, handle,
                                planned={t.track_id: t for t in items}, written=written,
                                waiting=waiting, previous=built.previous))
            saved = True
            # The library now plays the new files: the waiting ones go in.
            for partial, final in waiting:
                os.replace(partial, final)
            waiting.clear()

        with _LOCK:
            doc = _read_ledger()
            records = doc["audio"].setdefault(str(root), {})
            for planned in items:
                records[_rel_for(planned.source_path)] = _record(planned, Path(planned.destination))
            removed = _prune_audio(doc, root)
            _write_ledger(doc)
        return {
            "tracks": len(items),
            "copied": len(written),
            "unchanged": built.audio.unchanged,
            "normalised": sum(1 for t in items if t.gain_db),
            "skipped": len(built.audio.tracks) - len(items),
            "deleted_files": removed,
            "audio_root": str(root),
            "targets": results,
            "skipped_targets": [t.name for t in built.targets if t.skipped],
        }
    except BaseException:
        for partial, _final in waiting:
            partial.unlink(missing_ok=True)
        if not saved:
            # No library refers to anything this run placed.
            for path in created:
                path.unlink(missing_ok=True)
        raise


def _prune_audio(doc: dict, root: Path) -> int:
    """Delete the shared folder's copies no set holds any more."""
    records = doc["audio"].get(str(root)) or {}
    owned = _owned(doc)
    gone = [rel for rel in records if rel not in owned]
    for rel in gone:
        path = root / rel
        try:
            path.unlink(missing_ok=True)
        except OSError as ex:
            log.warning("could not delete %s: %s", path, ex)
            continue
        records.pop(rel, None)
        _prune_empty(path.parent, root)
    return len(gone)


def _prune_empty(folder: Path, root: Path) -> None:
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


# ---- one library: merge ------------------------------------------------------------


def _backup(platform: str, path: Path) -> None:
    """Before a push writes `path`. Traktor: a version-history baseline (the
    push itself is committed after). rekordbox, which Konduktor does not
    version (a library is more than one file): the database, its WAL and the
    playlist XML copied to app-data, the last 10 kept."""
    if platform == "traktor":
        history.ensure_baseline(path)
        return
    base = _local_dir() / "backups" / platform
    dest = base / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while dest.exists():
        n += 1
        dest = base / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    dest.mkdir(parents=True)
    for p in (path, path.with_name(path.name + "-wal"), path.with_name("masterPlaylists6.xml")):
        if p.is_file():
            shutil.copy2(p, dest / p.name)
    for old in sorted(d for d in base.iterdir() if d.is_dir())[:-_BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


def _commit(platform: str, path: Path, data: bytes | None, summary: str) -> None:
    if platform == "traktor" and data is not None:
        history.commit(path, data, summary, __version__)


def _folder_name(adapter, wanted: str, own: str | None) -> str:
    """The set's folder name: `wanted`, unless a top-level node the user owns
    already has it — Traktor's `create_folder` would hand back THEIR folder,
    and the next push would delete it."""
    taken = {n.name for n in adapter.playlist_tree() if n.id != own}
    name, n = wanted, 1
    while name in taken:
        name = f"{wanted} (Konduktor)" if n == 1 else f"{wanted} (Konduktor {n})"
        n += 1
    return name


def _tree_hash(name: str, payload: ExportPayload, moved: dict[str, str]) -> str:
    blob = [name, [[p.name, p.folders, [moved.get(t) for t in p.track_ids]] for p in payload.playlists],
            [moved.get(t.source_id) for t in payload.tracks]]
    return hashlib.sha256(json.dumps(blob).encode()).hexdigest()[:24]


def _node_ids(nodes) -> set[str]:
    out = set()
    for n in nodes or []:
        out.add(n.id)
        out |= _node_ids(getattr(n, "children", None))
    return out


def _update(target, local_id: str, item: ExportTrack, *, audio_change: AudioChange | None) -> None:
    """Bring a track this set pushed earlier up to date with the source."""
    caps = target.capabilities().tracks
    now = target.track(local_id)
    fields = {}
    for k in _METADATA_FIELDS:
        if k in caps.editable_fields:
            value = getattr(item.track, k, None)
            if (getattr(now, k, None) or None) != (value or None):
                fields[k] = value if value is not None else ("" if k != "rating" else 0)
    if fields:
        target.set_track_metadata(local_id, fields)
    if caps.key_writable and item.track.key_wheel and item.track.key_mode and (
            (now.key_wheel, now.key_mode) != (item.track.key_wheel, item.track.key_mode)):
        target.set_key(local_id, item.track.key_wheel, item.track.key_mode)
    if caps.colors and now.color != item.track.color:
        target.set_track_color(local_id, item.track.color)
    if audio_change is not None and caps.normalizable:
        target.audio_changed(local_id, audio_change)
    if item.loudness is not None and caps.auto_gain:
        target.set_auto_gain(local_id, item.loudness)
    if item.cues is not None:
        target.replace_cues(local_id, item.cues)


def _audio_change(target, item: ExportTrack, planned: PlannedTrack, previous: dict,
                  waiting: list) -> AudioChange:
    """What a copy written this run means to a library already playing it:
    its size, the level change, and — for a platform that draws waveforms
    from the file (rekordbox) — the new audio measured at its time base."""
    final = item.destination
    path = next((p for p, f in waiting if f == final), final)
    old = previous.get(str(planned.source_path))
    before = float((old[1] if old else {}).get("gain_db") or 0.0)
    measured = None
    lead = getattr(target, "analysis_lead", None)
    if lead is not None:
        from .core import waveform

        try:
            measured = waveform.analyse(path, lead=lead(final))
        except Exception as ex:  # noqa: BLE001 — the old waveform stays drawn
            log.warning("could not measure %s for its waveform: %s", path, ex)
    return AudioChange(gain_db=item.gain_db - before, size=path.stat().st_size, measured=measured)


def _merge(tp: TargetPlan, export_set, payload: ExportPayload, handle: JobHandle, *,
           planned: dict[str, PlannedTrack], written: set[str], waiting: list,
           previous: dict) -> dict:
    path = Path(tp.path)
    lid = library_id.for_path(path, platform=tp.platform, label=tp.name)
    with _LOCK:
        doc = _read_ledger()
    lib = doc["libraries"].get(lid) or {}
    sets = lib.get("sets") or {}
    mine = sets.get(export_set.id) or {}
    tracks: dict[str, dict] = dict(mine.get("tracks") or {})
    others = {rec.get("local_id") for sid, s in sets.items() if sid != export_set.id
              for rec in (s.get("tracks") or {}).values()}

    _backup(tp.platform, path)
    target = registry.open_library(path)
    try:
        caps = target.capabilities()
        if not caps.writable:
            raise PushRefused(f"{tp.name} cannot be written: {caps.readonly_cause}")
        held = _existing_files(target)
        moved: dict[str, str] = {}
        new_items: list[ExportTrack] = []
        updates: list[tuple[ExportTrack, str]] = []
        for item in payload.tracks:
            sid = item.source_id
            rec = tracks.get(sid)
            local = rec.get("local_id") if rec and rec.get("local_id") else None
            if local is not None and target.track(local) is None:
                local = None   # removed in the DJ app since: add it again
            if local is None:
                # Already in the library at that path (another set's, or a
                # ledger that was lost): the same file is the same track — a
                # second entry would be a corrupt Traktor collection.
                local = held.get(_key(item.destination))
            if local is None:
                new_items.append(item)
                continue
            moved[sid] = local
            if rec is None or rec.get("prep") != _prep_hash(item) or sid in written:
                updates.append((item, local))

        added = target.add_tracks(
            [NewTrack(track=i.track, audio_path=i.destination, cues=i.cues, art=i.art)
             for i in new_items], checkpoint=payload.checkpoint) if new_items else []
        for item, local in zip(new_items, added):
            moved[item.source_id] = local
            if item.loudness is not None and caps.tracks.auto_gain:
                target.set_auto_gain(local, item.loudness)
        for n, (item, local) in enumerate(updates, start=1):
            payload.checkpoint(f"Updating {item.track.title or item.destination.name}", step=n,
                               of=len(updates))
            change = (_audio_change(target, item, planned[item.source_id], previous, waiting)
                      if item.source_id in written else None)
            _update(target, local, item, audio_change=change)

        # What left the set.
        present = {i.source_id for i in payload.tracks}
        kept: list[str] = []
        removed = 0
        for sid in [s for s in tracks if s not in present]:
            rec = tracks[sid]
            local = rec.get("local_id")
            if not local or local in others or target.track(local) is None:
                tracks.pop(sid)
                continue
            title = target.track(local).title or local
            try:
                target.remove_tracks([local])
                removed += 1
                tracks.pop(sid)
            except Unsupported as ex:
                # rekordbox: a track in its History, Sampler, … — kept, and
                # the next push tries again.
                kept.append(title)
                rec["kept"] = str(ex)
        for item in payload.tracks:
            local = moved.get(item.source_id)
            if local is not None:
                tracks[item.source_id] = {
                    "local_id": local, "rel": _rel_for(planned[item.source_id].source_path),
                    "prep": _prep_hash(item)}

        # The set's folder, rebuilt when anything in it would change.
        existing = _node_ids(target.playlist_tree())
        folder = mine.get("folder_id") if mine.get("folder_id") in existing else None
        tree = _tree_hash(export_set.name, payload, moved)
        if folder is None or mine.get("tree") != tree or mine.get("folder_name") != export_set.name:
            if folder is not None:
                target.delete_playlist(folder)
            name = _folder_name(target, export_set.name, None)
            folder = target.create_folder(name)
            write_playlist_tree(target, payload.playlists, [t.source_id for t in payload.tracks],
                                moved, folder)
            folder_name = export_set.name
        else:
            folder_name = mine.get("folder_name")

        if target.dirty:
            _refuse_if_running(tp.platform)
            handle.progress(message=f"Saving {tp.name}…")
            outcome = target.save()
            _commit(tp.platform, path, getattr(outcome, "snapshot", None),
                    f"Pushed “{export_set.name}” from Konduktor")
        with _LOCK:
            doc = _read_ledger()
            lib = doc["libraries"].setdefault(lid, {})
            lib.update({"platform": tp.platform, "path": str(path)})
            lib.setdefault("sets", {})[export_set.id] = {
                "name": export_set.name, "folder_id": folder, "folder_name": folder_name,
                "tree": tree, "tracks": tracks, "pushed": time.time(),
            }
            _write_ledger(doc)
        return {"platform": tp.platform, "name": tp.name, "added": len(new_items),
                "updated": len(updates), "removed": removed, "kept": kept}
    finally:
        _close(target)


# ---- one library: overwrite ----------------------------------------------------------


def _replace_file(source: Path, final: Path) -> None:
    """Put `source` at `final` atomically: copied beside it, then renamed."""
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    shutil.copy2(source, partial)
    os.replace(partial, final)


def _overwrite(tp: TargetPlan, export_set, payload: ExportPayload, handle: JobHandle, *,
               planned: dict[str, PlannedTrack], written: set[str], waiting: list,
               previous: dict) -> dict:
    """Replace the library with this set: the platform's export writer builds
    a fresh one in app-data (the playlists in a folder named after the set,
    as a merge puts them), and its files are swapped in."""
    path = Path(tp.path)
    lid = library_id.for_path(path, platform=tp.platform, label=tp.name)
    writer = core_export.for_platform(tp.platform)
    if writer is None:
        raise PushRefused(f"Konduktor cannot write a {tp.name} library")
    # The old library goes, so nothing still plays the files being replaced —
    # and the writer measures and sizes each file as it now is.
    for partial, final in waiting:
        os.replace(partial, final)
    waiting.clear()
    work_root = _local_dir() / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=work_root))
    try:
        result = writer.write(payload, work)
        handle.raise_if_cancelled()
        _refuse_if_running(tp.platform)
        handle.progress(message=f"Replacing the {tp.name} library…")
        _backup(tp.platform, path)
        if tp.platform == "traktor":
            _replace_file(result.library, path)
            _commit(tp.platform, path, path.read_bytes(), f"Replaced by “{export_set.name}” from Konduktor")
        else:
            # rekordbox: the database, its playlist XML and the analysis files
            # under `share/` beside it (new UUID paths, so nothing collides).
            # A WAL left by the old database would be replayed into the new one.
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            _replace_file(result.library, path)
            xml = work / "masterPlaylists6.xml"
            if xml.is_file():
                _replace_file(xml, path.with_name(xml.name))
            share = work / "share"
            if share.is_dir():
                shutil.copytree(share, path.parent / "share", dirs_exist_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # Record what the new library holds, so a later merge push of this set
    # finds its tracks and folder.
    target = registry.open_library(path)
    try:
        held = _existing_files(target)
        tracks = {}
        for item in payload.tracks:
            local = held.get(_key(item.destination))
            if local is not None:
                tracks[item.source_id] = {"local_id": local,
                                          "rel": _rel_for(planned[item.source_id].source_path),
                                          "prep": _prep_hash(item)}
        folder = next((n.id for n in target.playlist_tree() if n.name == export_set.name), None)
        moved = {sid: rec["local_id"] for sid, rec in tracks.items()}
    finally:
        _close(target)
    with _LOCK:
        doc = _read_ledger()
        doc["libraries"][lid] = {"platform": tp.platform, "path": str(path), "sets": {
            export_set.id: {"name": export_set.name, "folder_id": folder,
                            "folder_name": export_set.name,
                            "tree": _tree_hash(export_set.name, payload, moved),
                            "tracks": tracks, "pushed": time.time()}}}
        _write_ledger(doc)
    return {"platform": tp.platform, "name": tp.name, "added": len(tracks), "updated": 0,
            "removed": 0, "kept": [], "replaced": True, "wiped_sets": tp.wipes_sets}
