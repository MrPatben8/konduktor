"""A stable identity for a library, surviving a move or a rename.

Everything Konduktor persists ABOUT a library currently keys off its OS path:
`history` hashes the resolved path to pick a repo directory, `prefs` stores
`path_mappings` and `last_collection` as path strings. That is fine for things
the user can shrug at losing. It is not fine for **export sets**, which are
curated work holding references into one specific library — moving a collection
would silently orphan every one of them.

## How identity survives a move

Two halves, and only the first is authoritative:

1. **A sidecar beside the library** — `.konduktor-library.json`, holding a random
   id. It lives in the library's own folder, so it MOVES WITH IT. Rename the
   folder, drag it to another disk, restore it from a backup: same id.
2. **A registry in app-data** — `libraries.json`, mapping id → last known path,
   platform and label. Derived and disposable; it exists so Konduktor can answer
   "which libraries do I know about" without touching every disk, and it is
   rebuilt from the sidecar whenever a library is opened.

The id is **random**, not derived. Deriving it from the path defeats the whole
purpose, and deriving it from the contents would change on every save.

## When a sidecar cannot be written

A removable or read-only library gets no sidecar — writing to a user's USB stick
to satisfy Konduktor's bookkeeping is not a trade worth making. Those fall back
to a **path-derived** id, which is stable for as long as the drive mounts at the
same place and is marked as such so nothing mistakes it for the durable kind.

Writing beside the user's library is not a new intrusion: saves already create a
`backups/` folder there. The sidecar is a hidden dotfile, so it does not show up
in the app's own file browser, which skips dotfiles.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

SIDECAR_NAME = ".konduktor-library.json"
_REGISTRY_NAME = "libraries.json"

# Path-derived ids carry this prefix so "durable" and "only as stable as this
# mount point" can never be confused — they behave differently on a move, and a
# caller that cares can ask.
VOLATILE_PREFIX = "path-"


def _registry_path() -> Path:
    return paths.app_data_dir() / _REGISTRY_NAME


def _sidecar_path(library_path: Path) -> Path:
    """Where the sidecar lives for a library.

    A library is a FILE on some platforms and a DIRECTORY on others, so the
    sidecar goes inside a directory library and beside a file one — either way,
    in the folder that travels with it.
    """
    library_path = Path(library_path)
    try:
        folder = library_path if library_path.is_dir() else library_path.parent
    except OSError:
        folder = library_path.parent
    return folder / SIDECAR_NAME


def _sidecar_key(library_path: Path) -> str:
    """Which entry in the sidecar belongs to this library.

    One sidecar per FOLDER, keyed by filename, because a folder can legitimately
    hold more than one library — a `collection.nml` beside an older one someone
    kept. A single id per folder would hand both the same identity and, once
    export sets exist, show one library's curated sets against another.

    A directory library is the folder itself, so it keys on ".".
    """
    library_path = Path(library_path)
    try:
        return "." if library_path.is_dir() else library_path.name
    except OSError:
        return library_path.name


def _volatile_id(library_path: Path) -> str:
    digest = hashlib.sha256(str(Path(library_path).resolve()).encode()).hexdigest()
    return f"{VOLATILE_PREFIX}{digest[:16]}"


def is_durable(library_id: str) -> bool:
    """Whether this id survives the library being moved."""
    return not library_id.startswith(VOLATILE_PREFIX)


# ---- the registry ------------------------------------------------------------


def known() -> dict[str, dict]:
    """Every library Konduktor has seen, by id. Derived data — safe to lose."""
    data = paths.read_json(_registry_path(), {})
    return data if isinstance(data, dict) else {}


def describe(library_id: str) -> dict | None:
    return known().get(library_id)


def _remember(library_id: str, library_path: Path, platform: str | None, label: str | None) -> None:
    registry = known()
    entry = registry.get(library_id) or {}
    entry.update(
        {
            "path": str(library_path),
            "platform": platform or entry.get("platform"),
            "label": label or entry.get("label") or Path(library_path).name,
            "last_seen": time.time(),
        }
    )
    entry.setdefault("first_seen", entry["last_seen"])
    registry[library_id] = entry
    try:
        paths.write_json(_registry_path(), registry)
    except OSError:
        # The registry is a convenience index; the sidecar is the source of
        # truth, so failing to write it must not fail opening a library.
        log.warning("could not update the library registry", exc_info=True)


def forget(library_id: str) -> None:
    """Drop a library from the registry. Does NOT touch its sidecar."""
    registry = known()
    if registry.pop(library_id, None) is not None:
        try:
            paths.write_json(_registry_path(), registry)
        except OSError:
            log.warning("could not update the library registry", exc_info=True)


# ---- identity ----------------------------------------------------------------


def for_path(
    library_path: Path,
    *,
    platform: str | None = None,
    label: str | None = None,
    sidecar: bool = True,
) -> str:
    """This library's id, creating and recording one on first sight.

    `sidecar=False` for a library Konduktor should not write next to — a
    removable drive, or one opened read-only. Those get a path-derived id.
    """
    library_path = Path(library_path)
    found = read_sidecar(library_path) if sidecar else None

    if found is None and sidecar:
        found = uuid.uuid4().hex
        if not _write_sidecar(library_path, found):
            found = None  # unwritable, e.g. a locked or read-only folder

    library_id = found or _volatile_id(library_path)
    _remember(library_id, library_path, platform, label)
    return library_id


def read_sidecar(library_path: Path) -> str | None:
    """The id recorded beside this library, or None."""
    entry = _sidecar_entries(library_path).get(_sidecar_key(library_path))
    if isinstance(entry, dict):
        value = entry.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _sidecar_entries(library_path: Path) -> dict:
    data = paths.read_json(_sidecar_path(library_path), {})
    entries = data.get("libraries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _write_sidecar(library_path: Path, library_id: str) -> bool:
    """Record an id beside the library. False when the location refuses it."""
    entries = _sidecar_entries(library_path)
    entries[_sidecar_key(library_path)] = {"id": library_id, "created": time.time()}
    try:
        paths.write_json(
            _sidecar_path(library_path),
            {
                # Only so a human finding this file knows what it is for. Never
                # read back: the path is exactly what must not be trusted here.
                "note": "Konduktor library ids. Keep this beside the library.",
                "libraries": entries,
            },
        )
        return True
    except OSError:
        log.info("no sidecar for %s — falling back to a path-derived id", library_path)
        return False
