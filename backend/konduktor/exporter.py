"""Running an export: plan it, copy the audio, write the library.

App-level, like `importer.py`, and for the same reason — it coordinates a source
library, a destination folder and an exporter, and none of those should know
about the others.

**`core` plans and copies; the exporter only writes the library file.** Layout,
collision and dedup rules are platform-independent and must not drift between
targets, and the exporter cannot write a Traktor `<LOCATION>` before the
destination path is known anyway.

## Ordering is the safety property

Audio is copied FIRST and the library written LAST, so a cancel during the long
part leaves no library file — its absence is what marks an export unfinished —
and everything copied is removed on the way out. The rollback tracks files
BEFORE each write, not after: cleaning up only COMPLETED copies leaves the
in-flight one behind, which is a bug the import feature had and was fixed there.

## Konduktor only deletes what Konduktor wrote

A re-export clears its previous export first, which combined with "the user picks
the destination" is a folder-deletion hazard: point an export at `~/Documents`
and a confirm dialog is the only thing between a click and real loss. So every
export writes a **manifest** of what it put there, and a destination that is
non-empty without one is REFUSED rather than confirmed. Clearing then removes
exactly the manifest's files — anything the user added themselves survives.

## A re-export copies only what changed

The manifest also records, per copied file, the SOURCE's path, size and mtime
and the COPY's size and mtime as read back. A re-export keeps a previous copy
when all four still match — the same quick check rsync makes, and no
checksums: hashing the copy means reading it back over USB, which costs about
what copying it again does. A tag edit rewrites the source file, so its mtime
moves and the file is copied again. Copies are matched by SOURCE path, not by
where they sit, because adding a track from another folder moves the shared
root below and with it every destination path; a kept copy whose path changed
is renamed, not copied. No record, or an unreadable one, means copy everything.

The ORDER keeps the safety properties: the previous export's libraries and
every file not kept go first, then the kept copies are moved, then what
changed is copied (under a temporary name, renamed into place), and the
libraries are written last. A cancel removes what THIS run wrote and rewrites
the manifest around what it kept — deleting the manifest with kept files still
there would leave a folder the next export refuses as not ours.

Decoding every track for its Pioneer waveforms is the other slow part, so the
measurements are cached on the drive too (`core/analysis_cache.py`), keyed by
the same source facts.

## Several targets, one copy of the audio

An export can target several platforms at once. The audio is copied ONCE and
every target's library is written over the same files, one after another. If
any of them fails, EVERYTHING is undone — the audio and the libraries already
written — because "the libraries' absence marks an export unfinished" only
holds if a half-finished multi-target export also leaves none behind.

## Mirror layout, under `Contents/`

Audio is laid out mirroring the source tree, relative to the deepest folder all
the tracks share (`common_dir_prefix`), inside a `Contents/` folder — the name
rekordbox itself uses on a stick. Mirroring absolute paths would put the user's
home directory on the stick; mirroring relative to the common root keeps the
structure recognisable and makes filename collisions impossible, since two
distinct source paths cannot share a relative path. `Contents/` is what keeps
the AUDIO from colliding with the LIBRARIES: without it, a music folder with a
top-level `PIONEER/` or `share/`, or a file called `master.db`, would land on
top of one.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import exports
from .core import registry
from .core.analysis_cache import CACHE_DIR, AnalysisCache, fingerprint
from .core.export import ExportPayload, ExportPlaylist, ExportTrack, for_platform
from .core.level_plan import Measure, plan_gain, shifted
from .core.pathmap import common_dir_prefix
from .core.places import is_drive_root, is_os_housekeeping
from .importer import SPACE_HEADROOM, copy_file, free_bytes
from .jobs import JobHandle

log = logging.getLogger(__name__)

MANIFEST_NAME = ".konduktor-export.json"
#: Every exported audio file lives under this folder, never at the root.
AUDIO_DIR = "Contents"
#: Where kept copies wait while being moved, and the suffix a copy is written
#: under until it is complete. Both are listed in the manifest while in use.
MOVING_DIR = ".konduktor-moving"
PARTIAL_SUFFIX = ".konduktor-partial"

#: Why an export cannot run. The UI words these; they are facts, not sentences.
BLOCKED_OCCUPIED = "destination_not_empty"
BLOCKED_EMPTY = "nothing_to_export"
BLOCKED_NO_TARGET = "unsupported_target"


@dataclass
class PlannedTrack:
    track_id: str
    title: str
    source_path: Path | None
    destination: Path | None
    size: int = 0
    missing: bool = False
    #: The source's mtime when planned, recorded in the manifest with `size`.
    mtime_ns: int = 0
    #: Where the previous export's copy of this very file sits, when it can be
    #: kept instead of copied again — possibly a different path from
    #: `destination`, in which case it is moved. None means copy.
    reuse: Path | None = None
    #: The source's stored loudness when fresh (None: not measured, or stale —
    #: the run measures it when it needs one).
    loudness: object | None = None
    #: The level the copy is written at (`level_plan.plan_gain`; 0.0 = copied
    #: as it is) and why (None: the run does not normalise).
    gain_db: float = 0.0
    level_outcome: str | None = None
    #: The run needs this source's loudness (it normalises, or writes auto gain).
    needs_loudness: bool = False


@dataclass(frozen=True)
class Level:
    """The run's "Normalise audio": every copy brought to `target` in `measure`,
    a raise stopping at the true-peak `ceiling` — Normalize's rules exactly
    (`level_plan`), applied to the copy and never to the source."""

    measure: Measure = "lufs"
    target: float = -9.0
    ceiling: float = -1.0


@dataclass
class ExportPlan:
    tracks: list[PlannedTrack] = field(default_factory=list)
    playlists: list[ExportPlaylist] = field(default_factory=list)
    destination: Path | None = None
    #: One of the BLOCKED_* facts, or None when the export can run.
    blocked: str | None = None
    #: Whether the destination already holds a Konduktor export to replace.
    replacing: bool = False
    #: Targets whose library is only found at a drive's root, when the
    #: destination is not one. A warning, not a block: the other targets are
    #: fine anywhere, and the user may be staging a stick's contents on purpose.
    not_drive_root: list[str] = field(default_factory=list)

    @property
    def exportable(self) -> list[PlannedTrack]:
        return [t for t in self.tracks if not t.missing]

    @property
    def total_bytes(self) -> int:
        """What will actually be COPIED — a kept file costs neither time nor
        space, since the previous export's other files are removed first."""
        return sum(t.size for t in self.exportable if t.reuse is None)

    @property
    def unchanged(self) -> int:
        return sum(1 for t in self.exportable if t.reuse is not None)

    def as_dict(self, free: int | None = None) -> dict:
        exportable = self.exportable
        levels: dict[str, int] = {}
        for t in exportable:
            if t.level_outcome:
                levels[t.level_outcome] = levels.get(t.level_outcome, 0) + 1
        return {
            # Normalise audio: copies whose level changes, tracks the run
            # measures first, and every outcome (`level_plan`) by count.
            "normalised": sum(1 for t in exportable if t.gain_db),
            "to_measure": sum(1 for t in exportable if t.needs_loudness and t.loudness is None),
            "level_counts": levels,
            "tracks": len(self.tracks),
            "exportable": len(self.exportable),
            "unchanged": self.unchanged,
            "missing": [t.title for t in self.tracks if t.missing],
            "playlists": [p.name for p in self.playlists],
            "total_bytes": self.total_bytes,
            "destination": str(self.destination) if self.destination else None,
            "free_bytes": free,
            "enough_space": (
                None if free is None else free >= self.total_bytes + SPACE_HEADROOM
            ),
            "blocked": self.blocked,
            "replacing": self.replacing,
            "not_drive_root": list(self.not_drive_root),
        }


# ---- the destination ---------------------------------------------------------


def manifest_path(destination: Path) -> Path:
    return Path(destination) / MANIFEST_NAME


def is_ours(destination: Path) -> bool:
    """Whether this folder holds an export Konduktor wrote."""
    try:
        return manifest_path(destination).is_file()
    except OSError:
        return False


def destination_blocked(destination: Path) -> str | None:
    """Why this folder cannot be exported into, or None.

    A folder that does not exist yet, or is empty, or carries our manifest, is
    fine. Anything else is REFUSED — not confirmed — because an export clears
    its destination and Konduktor must never delete what it did not write.
    "Empty" ignores what the OS writes by itself (`.Spotlight-V100`, `.Trashes`,
    …), or a stick's root would be refused the moment it was first mounted.
    """
    destination = Path(destination)
    try:
        if not destination.exists():
            return None
        if not destination.is_dir():
            return BLOCKED_OCCUPIED
        if is_ours(destination):
            return None
        occupied = any(not is_os_housekeeping(p.name) for p in destination.iterdir())
        return BLOCKED_OCCUPIED if occupied else None
    except OSError:
        return BLOCKED_OCCUPIED


def _clear(destination: Path, keep: set[Path] = frozenset()) -> None:
    """Remove the previous export, and only it — except the audio in `keep`,
    which this export reuses.

    Driven by the manifest rather than by wiping the folder: the manifest lists
    everything the last export wrote, so anything the user put there themselves
    survives. Empty directories are pruned afterwards so a re-export does not
    inherit the last one's folder skeleton.
    """
    data = _read_manifest(destination)
    for name in data.get("files", []):
        path = destination / name
        if path in keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass
    # `library` is the single-target manifest an older export left behind.
    libraries = list(data.get("libraries") or []) + [data.get("library")]
    for name in (*libraries, MANIFEST_NAME):
        if name:
            try:
                (destination / name).unlink()
            except OSError:
                pass
    _prune_empty(destination)


def _prune_empty(root: Path) -> None:
    try:
        for folder in sorted(
            (p for p in root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                folder.rmdir()
            except OSError:
                pass  # not empty — the user's own files live here
    except OSError:
        pass


def _read_manifest(destination: Path) -> dict:
    from . import paths

    data = paths.read_json(manifest_path(destination), {})
    return data if isinstance(data, dict) else {}


def _write_manifest(destination: Path, *, libraries: list[str], files: list[str],
                    audio: dict[str, dict], name: str) -> None:
    from . import paths

    paths.write_json(
        manifest_path(destination),
        {
            "note": "Written by Konduktor. It lists what this export put here, "
                    "so re-exporting can replace exactly those files.",
            "export": name,
            "written": time.time(),
            "libraries": libraries,
            "files": files,
            "audio": audio,
        },
    )


def _record(planned: PlannedTrack, copy: Path) -> dict:
    """What the manifest remembers about one copied file.

    The SOURCE's size and mtime as planned — if it changed during the copy, the
    next export sees a mismatch and copies again, which is the safe direction —
    and the COPY's as read back now, never assumed equal to the source's: a
    FAT32 stick keeps mtimes to 2 s, so `copystat` does not round-trip there.
    """
    st = copy.stat()
    return {
        "source": str(planned.source_path),
        "size": planned.size,
        "mtime_ns": planned.mtime_ns,
        "copy_size": st.st_size,
        "copy_mtime_ns": st.st_mtime_ns,
        # The level the copy was written at: a copy kept by a later export must
        # be at the level THAT export wants (missing = 0, every older manifest).
        "gain_db": planned.gain_db,
    }


def _previous_copies(destination: Path) -> dict[str, tuple[Path, dict]]:
    """The last export's copies, by SOURCE path — not by where they sit, since
    adding a track from another folder moves the shared root and with it every
    destination path. No record (none yet, an older manifest, an unreadable
    one) means nothing is reused: never guess from the files on disk."""
    if not is_ours(destination):
        return {}
    audio = _read_manifest(destination).get("audio")
    out: dict[str, tuple[Path, dict]] = {}
    if isinstance(audio, dict):
        for rel, record in audio.items():
            if isinstance(record, dict) and isinstance(record.get("source"), str):
                out[record["source"]] = (destination / rel, record)
    return out


def _reusable(planned: PlannedTrack, previous: dict[str, tuple[Path, dict]]) -> Path | None:
    """The previous copy of this file, if neither it nor its source changed."""
    found = previous.get(str(planned.source_path))
    if found is None:
        return None
    copy, record = found
    if (record.get("size"), record.get("mtime_ns")) != (planned.size, planned.mtime_ns):
        return None
    try:
        if abs(float(record.get("gain_db") or 0.0) - planned.gain_db) > 0.01:
            return None  # written at another level (Normalise audio changed)
    except (TypeError, ValueError):
        return None
    try:
        st = copy.stat()
    except OSError:
        return None
    # The copy is checked too: a file edited or replaced ON the stick is not
    # the one this export would have written.
    if (st.st_size, st.st_mtime_ns) != (record.get("copy_size"), record.get("copy_mtime_ns")):
        return None
    return copy


# ---- planning ----------------------------------------------------------------


def plan(adapter, export_set, *, store=None, level: Level | None = None,
         auto_gain: bool = False) -> ExportPlan:
    """Resolve the set and work out where every file lands. Reads only.

    With `level` (Normalise audio) each copy's gain comes from the source's
    STORED loudness in `store`; with `auto_gain` that loudness is also what the
    DJ app's gain is written from. A source with none (or a stale one) is
    measured by the run — and its copy decided then, since whether the
    previous one can be kept depends on its level."""
    destination = Path(export_set.destination).expanduser()
    built = ExportPlan(destination=destination)

    exporters = [for_platform(t) for t in export_set.targets]
    if not exporters or None in exporters:
        built.blocked = BLOCKED_NO_TARGET
        return built

    resolved = exports.resolve(adapter, export_set)
    tracks = [t for t in (adapter.track(i) for i in resolved.track_ids) if t is not None]

    # The source is the adapter's file, never `Track.filepath`: that is a
    # DISPLAY path, which on Traktor omits the volume and ignores the active
    # path mapping — so every track off the boot volume (or on a remapped
    # Windows drive) read as missing and the export was "empty". Its FACTS,
    # not `audio_path`: planning must not read a file, and on a library held
    # by a server reading one means downloading it. `run` fetches each file
    # only when it copies it.
    facts = adapter.audio_facts([t.id for t in tracks])
    sources = {tid: Path(f.key) for tid, f in facts.items()}

    # Mirror relative to the deepest shared folder, so the export carries the
    # user's structure without carrying their home directory.
    present = [str(p) for p in sources.values() if p]
    root = common_dir_prefix(present) if present else ""

    previous = _previous_copies(destination)
    wants_loudness = store is not None and (level is not None or auto_gain)
    entries = store.get([f.key for f in facts.values() if f is not None]) if wants_loudness else {}
    for track in tracks:
        source = sources.get(track.id)
        fact = facts.get(track.id)
        exists = fact is not None
        size, mtime_ns = (fact.size, fact.mtime_ns) if fact else (0, 0)
        planned = PlannedTrack(
            track_id=track.id,
            title=track.title or (source.name if source else track.id),
            source_path=source,
            destination=(destination / AUDIO_DIR / _relative(source, root)) if exists else None,
            size=size,
            mtime_ns=mtime_ns,
            missing=not exists,
            needs_loudness=exists and wants_loudness,
        )
        if planned.needs_loudness:
            entry = entries.get(fact.key)
            planned.loudness = entry.values if entry is not None and not entry.stale(fact) else None
        if exists and level is not None:
            set_level(planned, level)
        built.tracks.append(planned)
    assign_reuse(built, previous)

    names = {p.id: p for p in resolved.playlists}
    folders = _folder_paths(adapter)
    for playlist in resolved.playlists:
        if playlist.missing or not playlist.track_ids:
            continue
        built.playlists.append(
            ExportPlaylist(
                name=names[playlist.id].name,
                track_ids=list(playlist.track_ids),
                folders=folders.get(playlist.id, []),
            )
        )

    if not built.exportable:
        built.blocked = BLOCKED_EMPTY
    else:
        built.blocked = destination_blocked(destination)
    built.replacing = is_ours(destination)
    if not is_drive_root(destination):
        built.not_drive_root = [e.platform for e in exporters if e.drive_root]
    return built


def set_level(planned: PlannedTrack, level: Level) -> None:
    """The copy's gain from the source's loudness (`level_plan.plan_gain`)."""
    suffix = planned.source_path.suffix if planned.source_path else ""
    step = plan_gain(planned.loudness, suffix, measure=level.measure, target=level.target,
                     ceiling=level.ceiling)
    planned.gain_db, planned.level_outcome = step.gain_db, step.outcome


def assign_reuse(built: ExportPlan, previous: dict[str, tuple[Path, dict]]) -> None:
    """Which planned files keep the previous export's copy. A file whose level
    is still undecided (normalising, not yet measured) is copied unless the run
    decides otherwise once it has measured it."""
    claimed: set[Path] = set()
    for planned in built.tracks:
        planned.reuse = None
        if planned.missing or planned.level_outcome == "unmeasured":
            continue
        reuse = _reusable(planned, previous)
        # One kept copy can serve one planned file: two entries sharing a
        # source would otherwise both try to move it.
        if reuse is not None and reuse not in claimed:
            planned.reuse = reuse
            claimed.add(reuse)


def _relative(source: Path, root: str) -> Path:
    """Where `source` sits under the export root, mirroring its own folders."""
    text = str(source).replace("\\", "/")
    if root and text.startswith(root + "/"):
        return Path(text[len(root) + 1:])
    # No shared root (tracks on different volumes): keep the full path minus its
    # leading separator, which is ugly but never collides.
    return Path(text.lstrip("/"))


def _folder_paths(adapter) -> dict[str, list[str]]:
    """Each playlist's folder chain, outermost first."""
    out: dict[str, list[str]] = {}

    def walk(nodes, path: list[str]) -> None:
        for node in nodes or []:
            if node.kind == "folder":
                walk(getattr(node, "children", None), path + [node.name])
            else:
                out[node.id] = path
                walk(getattr(node, "children", None), path)

    walk(adapter.playlist_tree(), [])
    return out


# ---- running -----------------------------------------------------------------


def _locked(cues, lock_grids: bool):
    """The cues as exported: with `lock_grids`, a grid is LOCKED.

    The lock rides on the generic `TrackCues.grid_locked`, the same flag a
    source's own lock arrives in, so a writer reads one signal and never learns
    which of the two set it. Unlocked Traktor re-analyses an exported track on
    its first load (it has no AUDIO_ID, which cannot be computed), re-measuring
    the BPM — a 174 track can come back as 87 — and drifting the grid; a
    locked grid is left alone. A track with no grid is never locked: that
    would stop Traktor ever giving it one.
    """
    if lock_grids and cues is not None and cues.grid_markers and not cues.grid_locked:
        return cues.model_copy(update={"grid_locked": True})
    return cues


def run(adapter, export_set, built: ExportPlan, handle: JobHandle, *,
        root_folder: bool = False, lock_grids: bool = False, store=None,
        level: Level | None = None, auto_gain: bool = False) -> dict:
    """Copy what changed, then write every target's library over the audio.

    `root_folder` nests the playlists in a folder named after the export (the
    "Put exported playlists in a folder" setting); otherwise they sit at the top.
    `lock_grids` locks every exported track that HAS a grid (`_locked`); off,
    each track keeps the source's own lock. `level` (Normalise audio) writes
    each copy at its planned gain (`core.level_edit`, decode-verified); the
    source is never touched. `auto_gain` hands every writer the COPY's loudness
    for the DJ app's gain. Sources with no stored loudness are measured first,
    into `store` — the same measurement the Mastering columns show.

    Rolls back EVERYTHING this run wrote on cancel or on any failure —
    including the libraries of targets that had already been written when a
    later one failed. What it KEPT from the previous export stays, and the
    manifest is rewritten to say so: deleting the manifest with kept files
    still there would leave a folder the next export refuses as not ours.
    """
    exporters = [for_platform(t) for t in export_set.targets]
    if not exporters or None in exporters:
        raise ValueError(f"Konduktor cannot export to {', '.join(export_set.targets)}")
    if built.blocked:
        raise ValueError(f"This export cannot run: {built.blocked}")

    destination = Path(built.destination)
    items = built.exportable
    handle.progress(done=0, total=built.total_bytes, unit="bytes", message="Preparing…")
    cache = AnalysisCache(destination / CACHE_DIR)

    # Registered BEFORE each write, so a cancel mid-file removes the partial one
    # too. Cleaning up only completed copies leaves debris the retry then
    # suffixes around — a bug the import feature had.
    created: list[Path] = []
    # The previous export's copies this one keeps, by where they are NOW — so a
    # rollback during the moves still records each at its real location.
    kept: dict[Path, dict] = {}
    # What was on disk before the libraries were written. An exporter that
    # raises midway has written files it never got to REPORT (a OneLibrary
    # drive's analysis files, say), so rollback also removes anything that
    # appeared after this snapshot. Safe because the destination is known to
    # hold only our files and the user's, and the user's predate it.
    before: set[Path] | None = None
    done = 0
    try:
        destination.mkdir(parents=True, exist_ok=True)
        _measure_missing(adapter, built, handle, store=store, level=level)
        if is_ours(destination):
            handle.progress(message="Replacing the previous export…")
            old = _read_manifest(destination).get("audio") or {}
            for planned in items:
                if planned.reuse is not None:
                    record = old.get(_rel(planned.reuse, destination))
                    if record is None:
                        # The manifest changed since the plan: copy after all.
                        planned.reuse = None
                    else:
                        kept[planned.reuse] = record
            _clear(destination, keep=set(kept))
        else:
            for planned in items:
                planned.reuse = None
        reused = [t for t in items if t.reuse is not None]
        handle.progress(done=0, total=built.total_bytes, unit="bytes")
        # Written before anything moves or is copied, listing every path this
        # run may leave behind, so even a crash (not a cancel — nothing gets to
        # clean up after a crash) leaves a folder the next export can clear.
        _write_manifest(
            destination, libraries=[], name=export_set.name,
            files=sorted({_rel(p, destination) for p in (*kept, *(t.destination for t in items))}
                         | {_rel(_partial(t.destination), destination) for t in items}
                         | {_rel(_staging(destination, i), destination) for i in range(len(reused))}),
            audio={_rel(p, destination): r for p, r in kept.items()},
        )
        if reused:
            handle.progress(message=f"Keeping {len(reused)} unchanged track{'' if len(reused) == 1 else 's'}…")
            _move_kept(destination, reused, kept)

        payload_tracks: list[ExportTrack] = []
        for planned in items:
            handle.raise_if_cancelled()
            target = Path(planned.destination)
            if planned.reuse is None:
                handle.progress(message=f"Copying {planned.title}")
                target.parent.mkdir(parents=True, exist_ok=True)
                # Copied beside the target and renamed into place, so a cancel
                # can never leave a half-written file under a real track's name.
                partial = _partial(target)
                created += [partial, target]

                def advance(n: int) -> None:
                    nonlocal done
                    done += n
                    handle.progress(done=done)

                # The file to READ: on this machine that is the source itself;
                # on a library held by a server, a downloaded copy of it.
                local = adapter.audio_path(planned.track_id) or planned.source_path
                levelled = False
                if planned.gain_db:
                    from .core import level_edit

                    handle.progress(message=f"Normalising {planned.title} ({planned.gain_db:+.1f} dB)")
                    try:
                        level_edit.apply(local, partial, planned.gain_db)
                        handle.raise_if_cancelled()
                        # A copy carried to a gig must be the source at the
                        # gain, nothing else: decode both and compare.
                        level_edit.verify(local, partial, planned.gain_db)
                        levelled = True
                        advance(planned.size)
                    except level_edit.LevelEditError as ex:
                        # Only the file says some things (HE-AAC in an .m4a):
                        # such a copy goes as it is, never fails the export.
                        log.warning("copying %s unlevelled: %s", planned.title, ex)
                        partial.unlink(missing_ok=True)
                        planned.gain_db, planned.level_outcome = 0.0, "unsupported"
                if not levelled:
                    copy_file(local, partial, handle, advance)
                os.replace(partial, target)
            payload_tracks.append(
                ExportTrack(
                    track=adapter.track(planned.track_id),
                    destination=target,
                    cues=_locked(adapter.track_cues(planned.track_id), lock_grids),
                    art=_cover(adapter, planned.track_id),
                    fingerprint=_fingerprint(planned),
                    analysis_cache=cache,
                    loudness=(shifted(planned.loudness, planned.gain_db)
                              if auto_gain and planned.loudness is not None else None),
                    gain_db=planned.gain_db,
                )
            )

        def checkpoint(message: str, step: int | None = None, of: int | None = None) -> None:
            handle.raise_if_cancelled()
            if step is not None and of:
                # The bar follows the library being written, in tracks: after
                # the copy, the slow part is decoding — and a re-export may
                # have copied nothing at all.
                handle.progress(done=step - 1, total=of, unit="tracks", message=message)
            else:
                handle.progress(message=message)

        payload = ExportPayload(
            name=export_set.name,
            root_folder=root_folder,
            tracks=payload_tracks,
            playlists=built.playlists,
            checkpoint=checkpoint,
        )
        before = _files(destination)
        libraries: list[tuple[str, Path]] = []
        for exporter in exporters:
            handle.raise_if_cancelled()
            # Indeterminate until the writer reports its tracks.
            handle.progress(done=0, total=0, unit="tracks",
                            message=f"Writing the {_display_name(exporter.platform)} library…")
            written = exporter.write(payload, destination)
            # Everything the exporter reported, not just the library: a target
            # can write a database PLUS a per-track analysis file, and anything
            # left out here is an orphan the next re-export cannot clear.
            created.extend(written.all_paths)
            libraries.append((exporter.platform, written.library))

        library_paths = {path for _, path in libraries}
        audio_paths = {Path(t.destination) for t in items}
        _write_manifest(
            destination,
            libraries=[_rel(p, destination) for _, p in libraries],
            files=sorted(
                {_rel(p, destination) for p in (*created, *audio_paths)
                 if p not in library_paths and p.exists()}
            ),
            audio={_rel(Path(t.destination), destination): _record(t, Path(t.destination))
                   for t in items},
            name=export_set.name,
        )
        # Only what this export's tracks need; an unticked Pioneer target
        # keeps its entries, since they are keyed by source, not by target.
        cache.prune({_fingerprint(t) for t in items})
        return {
            "tracks": len(payload_tracks),
            "unchanged": len(reused),
            "normalised": sum(1 for t in items if t.gain_db),
            "playlists": len(built.playlists),
            "skipped": len(built.tracks) - len(items),
            "destination": str(destination),
            "libraries": [
                {"platform": platform, "library": _rel(path, destination)}
                for platform, path in libraries
            ],
        }
    except BaseException:
        # Cancelled or failed: leave no library file and no half-written copies.
        # Matches import, deliberately — two features whose cancel means
        # different things is worse than either behaviour on its own.
        leftovers = (_files(destination) - before) if before is not None else set()
        for path in (*created, *leftovers):
            if CACHE_DIR in path.relative_to(destination).parts:
                continue   # measurements of the SOURCE: still true, and ours
            try:
                path.unlink()
            except OSError:
                pass
        if kept:
            # The previous export's files this one kept are still here, so the
            # folder must still say it is ours — with no library, which is what
            # marks an export unfinished.
            _write_manifest(
                destination, libraries=[], name=export_set.name,
                files=sorted(_rel(p, destination) for p in kept),
                audio={_rel(p, destination): r for p, r in kept.items()},
            )
        else:
            # Nothing kept: the folder goes back to having no export in it at
            # all, cache included, as a first export's cancel always has.
            cache.clear()
            try:
                manifest_path(destination).unlink()
            except OSError:
                pass
        _prune_empty(destination)
        raise


def _move_kept(destination: Path, reused: list[PlannedTrack], kept: dict[Path, dict]) -> None:
    """Rename each kept copy to where this export puts it — instant on one drive.

    In two steps, through a staging folder, because one kept file's new path
    can be another's old one (the shared root moved), and a direct rename
    would overwrite a copy that has not moved out of the way yet. `kept` is
    updated after every rename, so a failure part-way still knows where each
    file really is.
    """
    moving = [t for t in reused if t.reuse != Path(t.destination)]
    staged: list[tuple[Path, PlannedTrack]] = []
    for i, planned in enumerate(moving):
        stage = _staging(destination, i)
        stage.parent.mkdir(parents=True, exist_ok=True)
        os.replace(planned.reuse, stage)
        kept[stage] = kept.pop(planned.reuse)
        staged.append((stage, planned))
    for stage, planned in staged:
        target = Path(planned.destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, target)
        kept[target] = kept.pop(stage)
    if staged:
        _prune_empty(destination)


def _staging(destination: Path, i: int) -> Path:
    return destination / MOVING_DIR / str(i)


def _partial(target: Path) -> Path:
    return target.with_name(target.name + PARTIAL_SUFFIX)


def _rel(path: Path, destination: Path) -> str:
    return str(Path(path).relative_to(destination))


def _fingerprint(planned: PlannedTrack) -> str:
    # The waveform cache is keyed by the SOURCE; a copy written at another
    # level draws differently, so its gain is part of the key — else a
    # normalised stick would be drawn from the un-levelled measurement.
    source = planned.source_path if not planned.gain_db else f"{planned.source_path}#gain={planned.gain_db:.4f}"
    return fingerprint(source, planned.size, planned.mtime_ns)


def _measure_missing(adapter, built: ExportPlan, handle: JobHandle, *, store, level: Level | None) -> None:
    """Measure the sources the run needs a loudness for and has none, store
    each, then settle their copies' levels and which previous copies can be
    kept. Before anything at the destination changes, so a cancel here
    leaves the previous export as it was."""
    todo = [t for t in built.exportable if t.needs_loudness and t.loudness is None]
    if not todo or store is None:
        return
    from .core import loudness

    for n, planned in enumerate(todo, start=1):
        handle.raise_if_cancelled()
        handle.progress(done=n - 1, total=len(todo), unit="tracks",
                        message=f"Measuring {planned.title} ({n}/{len(todo)})")
        local = adapter.audio_path(planned.track_id) or planned.source_path
        try:
            values = loudness.measure_file(local)
        except Exception as ex:  # noqa: BLE001 — copied as it is, with no gain
            log.warning("could not measure %s: %s", planned.title, ex)
            values = None
        planned.loudness = values
        if values is not None:
            fact = adapter.audio_facts([planned.track_id]).get(planned.track_id)
            if fact is not None:
                store.put(fact, values)
        if level is not None:
            set_level(planned, level)
    assign_reuse(built, _previous_copies(Path(built.destination)))


def _cover(adapter, track_id: str):
    """The source's cover art, or None. A missing or unreadable cover costs the
    artwork on the stick, never the export."""
    try:
        return adapter.cover_art(track_id)
    except Exception as ex:  # noqa: BLE001
        log.warning("no cover art for %s: %s", track_id, ex)
        return None


def _files(root: Path) -> set[Path]:
    try:
        return {p for p in root.rglob("*") if p.is_file()}
    except OSError:
        return set()


def _display_name(platform: str) -> str:
    try:
        return registry.driver_by_platform(platform).display_name
    except registry.LibraryNotSupported:
        return platform


def space_for(built: ExportPlan) -> int | None:
    return free_bytes(Path(built.destination)) if built.destination else None


__all__ = [
    "AUDIO_DIR", "BLOCKED_EMPTY", "BLOCKED_NO_TARGET", "BLOCKED_OCCUPIED", "ExportPlan",
    "MANIFEST_NAME", "PlannedTrack", "destination_blocked", "is_ours", "plan",
    "run", "space_for",
]
