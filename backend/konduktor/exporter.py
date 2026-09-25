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

A re-export clears its destination first, which combined with "the user picks
the destination" is a folder-deletion hazard: point an export at `~/Documents`
and a confirm dialog is the only thing between a click and real loss. So every
export writes a **manifest** of what it put there, and a destination that is
non-empty without one is REFUSED rather than confirmed. Clearing then removes
exactly the manifest's files — anything the user added themselves survives.

## Mirror layout

Audio is laid out mirroring the source tree, relative to the deepest folder all
the tracks share (`common_dir_prefix`). Mirroring absolute paths would put the
user's home directory on the stick; mirroring relative to the common root keeps
the structure recognisable and makes filename collisions impossible, since two
distinct source paths cannot share a relative path.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import exports
from .core.export import ExportPayload, ExportPlaylist, ExportTrack, for_platform
from .core.pathmap import common_dir_prefix
from .core.places import is_os_housekeeping
from .importer import SPACE_HEADROOM, copy_file, free_bytes
from .jobs import JobHandle

log = logging.getLogger(__name__)

MANIFEST_NAME = ".konduktor-export.json"

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


@dataclass
class ExportPlan:
    tracks: list[PlannedTrack] = field(default_factory=list)
    playlists: list[ExportPlaylist] = field(default_factory=list)
    destination: Path | None = None
    #: One of the BLOCKED_* facts, or None when the export can run.
    blocked: str | None = None
    #: Whether the destination already holds a Konduktor export to replace.
    replacing: bool = False

    @property
    def exportable(self) -> list[PlannedTrack]:
        return [t for t in self.tracks if not t.missing]

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.exportable)

    def as_dict(self, free: int | None = None) -> dict:
        return {
            "tracks": len(self.tracks),
            "exportable": len(self.exportable),
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


def _clear(destination: Path) -> None:
    """Remove the previous export, and only it.

    Driven by the manifest rather than by wiping the folder: the manifest lists
    everything the last export wrote, so anything the user put there themselves
    survives. Empty directories are pruned afterwards so a re-export does not
    inherit the last one's folder skeleton.
    """
    data = _read_manifest(destination)
    for name in data.get("files", []):
        try:
            (destination / name).unlink()
        except OSError:
            pass
    for name in (data.get("library"), MANIFEST_NAME):
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


def _write_manifest(destination: Path, *, library: str, files: list[str], name: str) -> None:
    from . import paths

    paths.write_json(
        manifest_path(destination),
        {
            "note": "Written by Konduktor. It lists what this export put here, "
                    "so re-exporting can replace exactly those files.",
            "export": name,
            "written": time.time(),
            "library": library,
            "files": files,
        },
    )


# ---- planning ----------------------------------------------------------------


def plan(adapter, export_set) -> ExportPlan:
    """Resolve the set and work out where every file lands. Reads only."""
    destination = Path(export_set.destination).expanduser()
    built = ExportPlan(destination=destination)

    if for_platform(export_set.target) is None:
        built.blocked = BLOCKED_NO_TARGET
        return built

    resolved = exports.resolve(adapter, export_set)
    tracks = [t for t in (adapter.track(i) for i in resolved.track_ids) if t is not None]

    # The source is the adapter's `audio_path`, never `Track.filepath`: that is
    # a DISPLAY path, which on Traktor omits the volume and ignores the active
    # path mapping — so every track off the boot volume (or on a remapped
    # Windows drive) read as missing and the export was "empty".
    sources = {t.id: adapter.audio_path(t.id) for t in tracks}

    # Mirror relative to the deepest shared folder, so the export carries the
    # user's structure without carrying their home directory.
    present = [str(p) for p in sources.values() if p]
    root = common_dir_prefix(present) if present else ""

    for track in tracks:
        source = sources[track.id]
        exists = False
        size = 0
        try:
            exists = bool(source and source.is_file())
            size = source.stat().st_size if exists else 0
        except OSError:
            exists = False
        built.tracks.append(
            PlannedTrack(
                track_id=track.id,
                title=track.title or (source.name if source else track.id),
                source_path=source,
                destination=(destination / _relative(source, root)) if exists else None,
                size=size,
                missing=not exists,
            )
        )

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
    return built


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


def run(adapter, export_set, built: ExportPlan, handle: JobHandle) -> dict:
    """Copy the audio, then write the library. Rolls back everything on cancel."""
    exporter = for_platform(export_set.target)
    if exporter is None:
        raise ValueError(f"Konduktor cannot export to {export_set.target}")
    if built.blocked:
        raise ValueError(f"This export cannot run: {built.blocked}")

    destination = Path(built.destination)
    items = built.exportable
    total = sum(t.size for t in items)
    handle.progress(done=0, total=total, message="Preparing…")

    # Registered BEFORE each write, so a cancel mid-file removes the partial one
    # too. Cleaning up only completed copies leaves debris the retry then
    # suffixes around — a bug the import feature had.
    created: list[Path] = []
    done = 0
    try:
        destination.mkdir(parents=True, exist_ok=True)
        if is_ours(destination):
            handle.progress(message="Replacing the previous export…")
            _clear(destination)

        payload_tracks: list[ExportTrack] = []
        for planned in items:
            handle.raise_if_cancelled()
            handle.progress(message=f"Copying {planned.title}")
            target = Path(planned.destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            created.append(target)

            def advance(n: int) -> None:
                nonlocal done
                done += n
                handle.progress(done=done)

            copy_file(planned.source_path, target, handle, advance)
            payload_tracks.append(
                ExportTrack(
                    track=adapter.track(planned.track_id),
                    destination=target,
                    cues=adapter.track_cues(planned.track_id),
                )
            )

        handle.raise_if_cancelled()
        handle.progress(message=f"Writing the {export_set.target} library…")
        written = exporter.write(
            ExportPayload(
                name=export_set.name,
                tracks=payload_tracks,
                playlists=built.playlists,
            ),
            destination,
        )
        # Everything the exporter reported, not just the library: a target can
        # write a database PLUS a per-track analysis file, and anything left out
        # here is an orphan the next re-export cannot clear.
        created.extend(written.all_paths)

        library = written.library
        _write_manifest(
            destination,
            library=str(library.relative_to(destination)),
            files=sorted(
                {str(p.relative_to(destination)) for p in created if p != library}
            ),
            name=export_set.name,
        )
        return {
            "tracks": len(payload_tracks),
            "playlists": len(built.playlists),
            "skipped": len(built.tracks) - len(items),
            "destination": str(destination),
            "library": str(library),
        }
    except BaseException:
        # Cancelled or failed: leave no library file and no half-written copies.
        # Matches import, deliberately — two features whose cancel means
        # different things is worse than either behaviour on its own.
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            manifest_path(destination).unlink()
        except OSError:
            pass
        _prune_empty(destination)
        raise


def space_for(built: ExportPlan) -> int | None:
    return free_bytes(Path(built.destination)) if built.destination else None


__all__ = [
    "BLOCKED_EMPTY", "BLOCKED_NO_TARGET", "BLOCKED_OCCUPIED", "ExportPlan",
    "MANIFEST_NAME", "PlannedTrack", "destination_blocked", "is_ours", "plan",
    "run", "space_for",
]
