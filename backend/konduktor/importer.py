"""Importing tracks from a source library into the loaded one.

App-level, not adapter-level, and deliberately so: this coordinates TWO
libraries, and neither adapter should know the other exists. The source is asked
for generic tracks and cues; the destination is handed generic `NewTrack`s. The
only thing this module adds is everything in between — the audio copy, the
pre-scan, and the playlist reconstruction.

**The copy is this module's job, not the adapter's.** `NewTrack.audio_path` must
already exist when it reaches the destination, because a file copy that failed
halfway is not something a library write should be discovering.

Scope, chosen deliberately: **everything is added as a new entry**. Nothing is
matched against tracks the destination already holds by metadata, so there is no
merge policy and no cross-platform identity problem. Re-importing the same stick
duplicates, and a preview warns about that rather than preventing it.

The one match that IS made is by **file**: a source file the destination already
points at is never added again. Copying never hits that (it writes a fresh
path), but **referencing in place** does — the folder browser can add
`~/Music/x.mp3` to a collection that already holds it — and on Traktor two
entries for one path share a primary key, which is a corrupt collection rather
than a duplicate track. Such a file maps to the EXISTING track instead, so a
playlist or export it was headed for still gets it.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .core.adapter import NewTrack
from .jobs import JobHandle

log = logging.getLogger(__name__)

# Copy in chunks so a cancel lands within a few milliseconds rather than at the
# end of a 50 MB file, and so progress can move within one big track.
COPY_CHUNK = 1024 * 1024

# Leave the destination some room rather than filling it exactly. A disk with
# zero bytes free is a broken machine, not a successful import.
SPACE_HEADROOM = 64 * 1024 * 1024


@dataclass
class PlannedTrack:
    track_id: str          # the id in the SOURCE library
    title: str
    source_path: Path | None
    size: int = 0
    missing: bool = False
    # True when the destination already holds a track with this filename. Only a
    # warning: the user chose "add everything as new entries".
    duplicate: bool = False
    # The destination's own id for this exact file, when it already holds it.
    # Not added again; it stands in for the new id wherever one is needed.
    existing_id: str | None = None


@dataclass
class ImportPlan:
    tracks: list[PlannedTrack] = field(default_factory=list)
    playlists: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    destination: Path | None = None

    @property
    def importable(self) -> list[PlannedTrack]:
        return [t for t in self.tracks if not t.missing and t.existing_id is None]

    @property
    def existing(self) -> list[PlannedTrack]:
        return [t for t in self.tracks if t.existing_id is not None]

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.importable)

    def as_dict(self, free_bytes: int | None = None) -> dict:
        return {
            "tracks": len(self.tracks),
            "importable": len(self.importable),
            "missing": [t.title for t in self.tracks if t.missing],
            "duplicates": [t.title for t in self.tracks if t.duplicate],
            "existing": [t.title for t in self.existing],
            "playlists": [name for _, name in self.playlists],
            "total_bytes": self.total_bytes,
            "destination": str(self.destination) if self.destination else None,
            "free_bytes": free_bytes,
            "enough_space": (
                None if free_bytes is None
                else free_bytes >= self.total_bytes + SPACE_HEADROOM
            ),
        }


def _existing_filenames(dest) -> set[str]:
    out: set[str] = set()
    for track in dest.tracks:
        path = getattr(track, "filepath", None)
        if path:
            out.add(Path(path).name.lower())
    return out


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _existing_files(dest) -> dict[str, str]:
    """Every audio file the destination points at → its track id.

    Resolved through the adapter's own `audio_facts`, which is the only answer
    that includes the volume and the active path mapping; the projected
    `filepath` is a display string and is not good enough to match on. Facts,
    not `audio_path`: on a library held by a server that would download every
    file it has.
    """
    out: dict[str, str] = {}
    try:
        facts = dest.audio_facts([t.id for t in dest.tracks])
    except Exception:  # noqa: BLE001
        return out
    for track_id, fact in facts.items():
        out.setdefault(_key(Path(fact.key)), track_id)
    return out


def plan(source, dest, destination: Path | None, *, track_ids=None, playlist_ids=None) -> ImportPlan:
    """Work out what an import would do, WITHOUT doing any of it.

    Computed fresh every time and never stored. Playlist membership on the source
    can change between planning and importing (a stick can be re-exported), and a
    stale preview is worse than no preview.

    Playlist selection AUTO-INCLUDES the tracks in those playlists, and the whole
    set is deduplicated, so a track in three chosen playlists is copied once.
    """
    plan = ImportPlan(destination=destination)
    wanted: list[str] = []
    seen: set[str] = set()

    def want(track_id: str) -> None:
        if track_id not in seen:
            seen.add(track_id)
            wanted.append(track_id)

    for node_id in playlist_ids or []:
        node_tracks = source.playlist_tracks(node_id) or []
        for t in node_tracks:
            want(t.id)
        name = _playlist_name(source, node_id)
        if name:
            plan.playlists.append((node_id, name))

    for track_id in track_ids or []:
        want(track_id)

    # Neither given: the whole drive.
    if not (track_ids or playlist_ids):
        for t in source.tracks:
            want(t.id)
        for node_id, name in _all_playlists(source):
            plan.playlists.append((node_id, name))

    existing = _existing_filenames(dest)
    held = _existing_files(dest)
    for track_id in wanted:
        track = source.track(track_id)
        if track is None:
            continue
        raw = getattr(track, "filepath", None)
        path = Path(raw) if raw else None
        planned = PlannedTrack(
            track_id=track_id,
            title=track.title or (path.name if path else track_id),
            source_path=path,
        )
        try:
            if path is not None and path.is_file():
                planned.size = path.stat().st_size
                planned.existing_id = held.get(_key(path))
                planned.duplicate = planned.existing_id is None and path.name.lower() in existing
            else:
                planned.missing = True
        except OSError:
            planned.missing = True
        plan.tracks.append(planned)
    return plan


def _playlist_name(source, node_id: str) -> str | None:
    for nid, name in _all_playlists(source):
        if nid == node_id:
            return name
    return None


def _all_playlists(source) -> list[tuple[str, str]]:
    """Every selectable playlist on the source, flattened.

    Folders are dropped rather than recreated. The destination gets ONE folder,
    named after the drive, and nesting a stick's folder tree inside it would bury
    the playlists a user is trying to find.
    """
    out: list[tuple[str, str]] = []

    def walk(nodes):
        for n in nodes:
            if n.kind == "playlist":
                out.append((n.id, n.name))
            walk(n.children or [])

    walk(source.playlist_tree())
    return out


def free_bytes(path: Path, dest=None) -> int | None:
    """Free space where the audio will land. On the DESTINATION's machine: a
    library held by a server reports its own disk (`free_space`), since the
    path names a folder there, not here."""
    remote_free = getattr(dest, "free_space", None) if dest is not None and uploads_audio(dest) else None
    if remote_free is not None:
        try:
            return remote_free(str(path))
        except Exception:  # noqa: BLE001 — unknown is not "full"
            return None
    try:
        target = path if path.exists() else path.parent
        return shutil.disk_usage(target).free
    except OSError:
        return None


def uploads_audio(dest) -> bool:
    """Whether added audio is SENT to the machine holding the library (a
    server) rather than copied on this one — `tracks.audio_destination`."""
    try:
        return dest.capabilities().tracks.audio_destination == "library" and hasattr(dest, "upload_audio")
    except Exception:  # noqa: BLE001
        return False


def _unique_target(folder: Path, name: str, taken: set[str]) -> Path:
    """A free filename in `folder`, suffixing on collision.

    `taken` covers names claimed earlier in THIS run, which the filesystem cannot
    answer for — two sticks can both hold `Track.mp3`, and the first copy has not
    necessarily been written yet when the second is planned.

    Known consequence of suffixing by processing order (accepted in the export
    design): re-importing may assign a different suffix to the same track.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate = name
    n = 1
    while candidate.lower() in taken or (folder / candidate).exists():
        n += 1
        candidate = f"{stem}-{n}{'.' + ext if ext else ''}"
    taken.add(candidate.lower())
    return folder / candidate


def copy_file(src: Path, dst: Path, handle: JobHandle, on_bytes) -> None:
    """Copy a file, checking for cancellation between chunks.

    Public because export copies audio the same way and for the same reason: a
    second chunked-copy loop would be a second place for the cancel check to be
    forgotten, and a forgotten cancel check is invisible until someone tries to
    stop a multi-gigabyte copy.
    """
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            handle.raise_if_cancelled()
            chunk = fin.read(COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            on_bytes(len(chunk))
    shutil.copystat(src, dst, follow_symlinks=True)


def run(
    source,
    dest,
    plan: ImportPlan,
    handle: JobHandle,
    *,
    folder_name: str | None = None,
    reference: bool = False,
    into_playlist: str | None = None,
) -> dict:
    """Perform the import. Returns a summary dict for the job result.

    Ordering is the safety property. Audio is copied FIRST and the library is
    written LAST, so a cancel or a failure during the long part leaves the
    collection untouched and the copied files are removed on the way out. Once
    copying is done the library write is fast and is allowed to finish.

    This is a COMMITTED action — it saves at the end — rather than an in-memory
    edit the Save button flushes. A multi-gigabyte copy is not a pending tweak,
    and audio on disk with unsaved entries pointing at it is the worst of both.

    `reference` adds each file WHERE IT IS instead of copying it, so there is no
    destination and nothing to roll back. `into_playlist` appends every planned
    track — the new ones and those the destination already held — to one of the
    destination's playlists before the save, so both land in one commit.

    A destination that PLACES ITS OWN AUDIO (`tracks.places_audio`: a OneLibrary
    stick) overrides both: `dest.place_audio` names each copy's target, or says
    the file can be used where it is, and `reference`/`plan.destination` are
    ignored.
    """
    destination = plan.destination
    places = places_audio(dest)
    # A library held by a server: each file is UPLOADED into `destination`,
    # a folder on the server, instead of copied here. Nothing can be left in
    # place — the server cannot see this computer's files.
    uploads = not places and uploads_audio(dest)
    if uploads and reference:
        raise ValueError("A remote library cannot reference files on this computer")
    if not reference and not places and not uploads:
        assert destination is not None
        destination.mkdir(parents=True, exist_ok=True)

    importable = plan.importable
    total = plan.total_bytes or len(importable)
    handle.progress(done=0, total=total, message="Copying audio…")

    copied: list[tuple[PlannedTrack, Path]] = []
    # Every path this run has opened for writing, including the one being written
    # when a cancel lands. `copied` holds only COMPLETED copies, so cleaning up
    # from it leaves the half-written file behind — which then makes the next
    # attempt suffix around a file that is not really there.
    created: list[Path] = []
    # Files sent to the server, which a failure asks it to delete again.
    uploaded: list[Path] = []
    taken: set[str] = set()
    moved = 0

    def bump(n: int) -> None:
        nonlocal moved
        moved += n
        handle.progress(done=moved)

    try:
        for planned in importable:
            handle.raise_if_cancelled()
            assert planned.source_path is not None
            if places:
                target = dest.place_audio(planned.source_path, source.track(planned.track_id))
                if target is None:
                    copied.append((planned, planned.source_path))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
            elif reference:
                copied.append((planned, planned.source_path))
                continue
            elif uploads:
                handle.progress(message=f"Uploading {planned.title}")

                def sent(n: int) -> None:
                    handle.raise_if_cancelled()  # mid-file: the server drops the partial
                    bump(n)

                # The server never overwrites: a taken name comes back `-2`…
                target = dest.upload_audio(planned.source_path, str(destination),
                                           planned.source_path.name, progress=sent)
                uploaded.append(target)
                copied.append((planned, target))
                continue
            else:
                target = _unique_target(destination, planned.source_path.name, taken)
            handle.progress(message=f"Copying {planned.title}")
            created.append(target)  # registered BEFORE the write, so a cancel
            copy_file(planned.source_path, target, handle, bump)  # mid-file is cleaned
            copied.append((planned, target))

        handle.raise_if_cancelled()
        handle.progress(message="Adding tracks to the library…")

        items: list[NewTrack] = []
        for planned, target in copied:
            track = source.track(planned.track_id)
            if track is None:
                continue
            cues = None
            try:
                cues = source.track_cues(planned.track_id)
            except Exception:  # noqa: BLE001 — prep is a bonus, not a blocker
                log.debug("could not read cues for %s", planned.track_id, exc_info=True)
            art = None
            cover_art = getattr(source, "cover_art", None)
            if cover_art is not None:
                try:
                    art = cover_art(planned.track_id)
                except Exception:  # noqa: BLE001 — art is a bonus too
                    log.debug("could not read art for %s", planned.track_id, exc_info=True)
            items.append(NewTrack(track=track, audio_path=target, cues=cues, art=art))

        def checkpoint(message: str, *, step: int, of: int) -> None:
            # A platform that analyses what it adds (Rekordbox) is slow here,
            # and calls this before touching the library — so a cancel still
            # leaves it untouched and the copies are cleaned up below.
            handle.raise_if_cancelled()
            handle.progress(done=step - 1, total=of, message=message)

        new_ids = dest.add_tracks(items, checkpoint=checkpoint)
        by_source = {
            planned.track_id: new_id
            for (planned, _), new_id in zip(copied, new_ids)
        }
        by_source.update({t.track_id: t.existing_id for t in plan.existing})
        # Every planned track's id in the destination, in plan order.
        all_ids = [by_source[t.track_id] for t in plan.tracks if t.track_id in by_source]

        if into_playlist is not None:
            handle.progress(message="Adding to the playlist…")
            current = dest.playlist_entries(into_playlist) or []
            have = set(current)
            dest.set_playlist_entries(
                into_playlist, current + [i for i in all_ids if i not in have]
            )

        playlists_made = _rebuild_playlists(
            source, dest, plan, by_source, folder_name, handle
        )

        handle.progress(message="Saving…")
        outcome, _commit = _save(dest)
        return {
            "tracks": len(new_ids),
            "track_ids": all_ids,
            "already_held": len(plan.existing),
            "playlists": playlists_made,
            "skipped_missing": len([t for t in plan.tracks if t.missing]),
            "destination": str(destination) if destination else None,
            "summary": getattr(outcome, "summary", ""),
        }
    except BaseException:
        # A cancelled or failed import must not leave orphaned audio behind: the
        # library was never written, so those files reference nothing.
        for target in created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                log.debug("could not clean up %s", target, exc_info=True)
        for target in uploaded:
            try:
                dest.delete_upload(target)  # refused once the library names it
            except Exception:  # noqa: BLE001
                log.debug("could not clean up upload %s", target, exc_info=True)
        raise


def places_audio(dest) -> bool:
    """Whether the destination decides where added audio goes (a stick)."""
    try:
        return bool(dest.capabilities().tracks.places_audio) and hasattr(dest, "place_audio")
    except Exception:  # noqa: BLE001
        return False


def placed_bytes(dest, plan: ImportPlan) -> int:
    """What an import into a self-placing library will COPY: a file already under
    its `audio_home()` is used in place and costs nothing."""
    home = dest.audio_home()
    total = 0
    for planned in plan.importable:
        path = planned.source_path
        if path is not None and not path.resolve().is_relative_to(home.resolve()):
            total += planned.size
    return total


def _save(dest):
    """Save through AppState so the version-history commit is not skipped."""
    from .app_state import STATE

    if STATE.adapter is dest:
        return STATE.save()
    return dest.save(), None


def _rebuild_playlists(source, dest, plan, by_source, folder_name, handle) -> int:
    """Recreate the source's playlists in the destination.

    They go into ONE folder named after the drive, so a re-import is easy to find
    and easy to delete, and the collection's root does not accumulate. A platform
    that cannot make folders puts them at the root instead — the capability says
    which, rather than this guessing.
    """
    if not plan.playlists:
        return 0
    parent = None
    if folder_name and dest.capabilities().playlists.folders:
        try:
            parent = dest.create_folder(folder_name)
        except Exception:  # noqa: BLE001 — a missing folder is not worth failing over
            log.debug("could not create folder %r", folder_name, exc_info=True)

    made = 0
    for node_id, name in plan.playlists:
        handle.progress(message=f"Recreating playlist {name}")
        source_tracks = source.playlist_tracks(node_id) or []
        ids = [by_source[t.id] for t in source_tracks if t.id in by_source]
        if not ids:
            continue
        new_id = dest.create_playlist(name, parent)
        dest.set_playlist_entries(new_id, ids)
        made += 1
    return made
