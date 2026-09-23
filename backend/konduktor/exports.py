"""Export sets: a named, persisted slice of the library, bound to a destination.

An export set is what the user curates before exporting — a name, a target
platform, a destination folder, and the tracks and playlists to include. It is
**Konduktor's own data**, never written into the user's library: a Traktor
collection has nowhere to put this, and putting it there would mean a concept
only Konduktor understands living in a file Traktor rewrites.

Three things shape the design:

**References are LIVE.** A set stores playlist *ids*, not the tracks that were in
them when you dragged one in. They are resolved at export time, so adding a track
to a playlist next week means the next export picks it up. The consequence is
that the contents of a set can never be cached — `resolve()` asks the adapter
every time, and the UI's count changes underneath the user by design.

**A playlist is removable only as a whole.** A live reference and per-track
removal are in tension: honouring both needs an exclusion list, i.e. hidden state
that silently decides what a future export contains. Loose tracks are removable
individually; for a subset of a playlist, add the tracks instead of the playlist.

**Keyed by library id, not path.** These are curated work. Keying them on the
library's OS path — as everything else in the app still does — would orphan every
set the moment the user moves their collection. See `library_id.py`.

Storage is one JSON file per library under the app-data dir, written atomically
via `paths.write_json`. Deliberately NOT `prefs.py`, whose best-effort "any I/O
error degrades to no prefs" is right for a window size and wrong for this.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


@dataclass
class ExportSet:
    """One curated export. `destination` is bound at creation, not at run time.

    Bound early because it is not merely convenient: a Traktor `<LOCATION>` is a
    volume name plus a volume-relative path, both derived from where the export
    lands, so the library file cannot be written until the destination is known.
    """

    id: str
    name: str
    target: str            # platform id of the export TARGET, e.g. "traktor"
    destination: str       # OS path of the folder the export is written into
    playlist_ids: list[str] = field(default_factory=list)
    track_ids: list[str] = field(default_factory=list)   # loose tracks only
    created: float = 0.0
    modified: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResolvedPlaylist:
    """A referenced playlist, as it stands right now."""

    id: str
    name: str
    missing: bool               # the playlist has since been deleted
    track_ids: list[str] = field(default_factory=list)


@dataclass
class ResolvedSet:
    """What an export set contains at this instant.

    Never stored. The references are live, so a cached answer would be a lie the
    moment the user edits a playlist.
    """

    playlists: list[ResolvedPlaylist] = field(default_factory=list)
    loose_track_ids: list[str] = field(default_factory=list)
    # Ids in the set that the library no longer has. Surfaced rather than
    # silently dropped: a gig stick quietly missing four tracks is the worst
    # failure this feature has.
    dangling_track_ids: list[str] = field(default_factory=list)

    @property
    def track_ids(self) -> list[str]:
        """Every track this export would carry, DEDUPED, in a stable order.

        A track referenced both loosely and through a playlist is one track and
        one file copy. Playlists come first so the order follows the tree the
        user sees.
        """
        seen: set[str] = set()
        out: list[str] = []
        for playlist in self.playlists:
            for track_id in playlist.track_ids:
                if track_id not in seen:
                    seen.add(track_id)
                    out.append(track_id)
        for track_id in self.loose_track_ids:
            if track_id not in seen:
                seen.add(track_id)
                out.append(track_id)
        return out


# ---- storage -----------------------------------------------------------------


def _store_path(library_id: str) -> Path:
    return paths.app_data_dir() / "exports" / f"{library_id}.json"


def _load(library_id: str) -> dict[str, ExportSet]:
    data = paths.read_json(_store_path(library_id), {})
    raw = data.get("sets") if isinstance(data, dict) else None
    out: dict[str, ExportSet] = {}
    if isinstance(raw, dict):
        for set_id, values in raw.items():
            if not isinstance(values, dict):
                continue
            try:
                out[set_id] = ExportSet(
                    id=set_id,
                    name=values.get("name") or "Untitled",
                    target=values.get("target") or "traktor",
                    destination=values.get("destination") or "",
                    playlist_ids=list(values.get("playlist_ids") or []),
                    track_ids=list(values.get("track_ids") or []),
                    created=float(values.get("created") or 0.0),
                    modified=float(values.get("modified") or 0.0),
                )
            except (TypeError, ValueError):
                log.warning("skipping unreadable export set %s", set_id)
    return out


def _save(library_id: str, sets: dict[str, ExportSet]) -> None:
    paths.write_json(
        _store_path(library_id),
        {"library_id": library_id, "sets": {k: v.as_dict() for k, v in sets.items()}},
    )


# ---- reading -----------------------------------------------------------------


def all_sets(library_id: str) -> list[ExportSet]:
    """Every export set for one library, oldest first so the list is stable."""
    return sorted(_load(library_id).values(), key=lambda s: (s.created, s.name.lower()))


def get(library_id: str, set_id: str) -> ExportSet | None:
    return _load(library_id).get(set_id)


def destination_conflict(library_id: str, destination: str, *, ignore: str | None = None) -> str | None:
    """The name of another set already pointing at `destination`, if any.

    An export CLEARS its destination before writing, so two sets sharing one
    folder means the second silently wipes the first's output. Caught when the
    destination is chosen rather than at run time, where it would already be too
    late to be a warning.
    """
    target = _normalise(destination)
    if not target:
        return None
    for existing in _load(library_id).values():
        if existing.id != ignore and _normalise(existing.destination) == target:
            return existing.name
    return None


def _normalise(destination: str) -> str:
    try:
        return str(Path(destination).expanduser().resolve())
    except (OSError, ValueError):
        return destination.strip()


# ---- writing -----------------------------------------------------------------


def create(library_id: str, *, name: str, target: str, destination: str) -> ExportSet:
    sets = _load(library_id)
    now = time.time()
    created = ExportSet(
        id=uuid.uuid4().hex,
        name=name.strip() or "Untitled",
        target=target,
        destination=destination,
        created=now,
        modified=now,
    )
    sets[created.id] = created
    _save(library_id, sets)
    return created


def update(library_id: str, set_id: str, **fields) -> ExportSet | None:
    """Rename, retarget or relocate a set. Unknown fields are ignored."""
    sets = _load(library_id)
    found = sets.get(set_id)
    if found is None:
        return None
    for key in ("name", "target", "destination"):
        if key in fields and fields[key] is not None:
            setattr(found, key, fields[key])
    found.modified = time.time()
    _save(library_id, sets)
    return found


def delete(library_id: str, set_id: str) -> bool:
    """Forget a set. Deliberately does NOT touch its destination folder."""
    sets = _load(library_id)
    if sets.pop(set_id, None) is None:
        return False
    _save(library_id, sets)
    return True


def add(library_id: str, set_id: str, *, track_ids=None, playlist_ids=None) -> ExportSet | None:
    """Add tracks and/or playlists, preserving order and ignoring duplicates."""
    sets = _load(library_id)
    found = sets.get(set_id)
    if found is None:
        return None
    for incoming, existing in (
        (track_ids or [], found.track_ids),
        (playlist_ids or [], found.playlist_ids),
    ):
        have = set(existing)
        for item in incoming:
            if item not in have:
                have.add(item)
                existing.append(item)
    found.modified = time.time()
    _save(library_id, sets)
    return found


def remove(library_id: str, set_id: str, *, track_ids=None, playlist_ids=None) -> ExportSet | None:
    """Remove loose tracks and/or whole playlists.

    There is no "remove a track from a referenced playlist": see the module
    docstring. A track id passed here is dropped from the LOOSE list only, so
    removing one that a referenced playlist also supplies is a no-op — which is
    honest, because the playlist still puts it in the export.
    """
    sets = _load(library_id)
    found = sets.get(set_id)
    if found is None:
        return None
    if track_ids:
        drop = set(track_ids)
        found.track_ids = [t for t in found.track_ids if t not in drop]
    if playlist_ids:
        drop = set(playlist_ids)
        found.playlist_ids = [p for p in found.playlist_ids if p not in drop]
    found.modified = time.time()
    _save(library_id, sets)
    return found


def retarget(library_id: str, mapping: dict[str, str]) -> None:
    """Follow track ids that changed, e.g. after an OS-path remap.

    A Traktor track id IS its path, so a remap renames every id in the library.
    Without this a set's references would dangle silently — and silently is the
    problem, since the set would still look fine and export fewer tracks.
    """
    if not mapping:
        return
    sets = _load(library_id)
    touched = False
    for found in sets.values():
        renamed = [mapping.get(t, t) for t in found.track_ids]
        if renamed != found.track_ids:
            found.track_ids = renamed
            found.modified = time.time()
            touched = True
    if touched:
        _save(library_id, sets)


# ---- resolution --------------------------------------------------------------


def resolve(adapter, export_set: ExportSet) -> ResolvedSet:
    """What this set contains RIGHT NOW, against the loaded library.

    Computed on every call and never stored, because the references are live —
    the whole point is that a playlist edited after curation is picked up.
    """
    resolved = ResolvedSet()
    names = _playlist_names(adapter)

    for playlist_id in export_set.playlist_ids:
        entries = adapter.playlist_entries(playlist_id)
        resolved.playlists.append(
            ResolvedPlaylist(
                id=playlist_id,
                name=names.get(playlist_id, playlist_id),
                # A playlist deleted since curation. Shown, not dropped: the set
                # is the user's record of intent and losing it silently is worse
                # than showing something broken.
                missing=entries is None,
                track_ids=list(entries or []),
            )
        )

    for track_id in export_set.track_ids:
        if adapter.track(track_id) is None:
            resolved.dangling_track_ids.append(track_id)
        else:
            resolved.loose_track_ids.append(track_id)
    return resolved


def _playlist_names(adapter) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(nodes) -> None:
        for node in nodes or []:
            out[node.id] = node.name
            walk(getattr(node, "children", None))

    walk(adapter.playlist_tree())
    return out
