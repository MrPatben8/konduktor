"""Tiny persisted user-preferences store.

A self-contained JSON file in the app's per-OS user-writable data dir (see
``paths.app_data_dir``) so it survives a frozen/packaged build and works the same
on every platform. Best-effort: any I/O error degrades to "no prefs".

**Every read-modify-write holds ``_LOCK`` and every write is atomic.** FastAPI
runs ``PATCH /api/prefs`` on a thread pool, and the UI sends several at once on
launch. Unlocked, with ``write_text`` truncating before it wrote, a reader could
catch the file empty, take it for "no prefs" and write back only its own key —
wiping everything else (measured: 249 of 300 trials of three concurrent patches).
A file that exists but will not parse is moved aside before it is replaced, so
it is never silently overwritten.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import paths

# userprefs.json in the app-data dir. Older versions kept it package-relative
# (backend/userprefs.json); _migrate_legacy() copies that in once on first use.
_PREFS_PATH = paths.app_data_dir() / "userprefs.json"
_LEGACY_PREFS_PATH = Path(__file__).resolve().parent.parent / "userprefs.json"

# Reentrant: the helpers below read and write inside one critical section.
_LOCK = threading.RLock()


def _migrate_legacy() -> None:
    """One-time: if the new file is absent but a legacy package-relative one
    exists, copy it into the app-data dir. Best-effort."""
    try:
        if not _PREFS_PATH.exists() and _LEGACY_PREFS_PATH.exists():
            _PREFS_PATH.write_bytes(_LEGACY_PREFS_PATH.read_bytes())
    except OSError:
        pass


def _read(*, for_write: bool) -> dict:
    """The stored prefs, or {} if there are none or they cannot be read.

    With ``for_write``, a file that exists but is not a JSON object is renamed
    to ``userprefs.json.corrupt`` first, since the caller is about to replace it.
    """
    try:
        _migrate_legacy()
        text = _PREFS_PATH.read_text()
    except OSError:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    if for_write:
        try:
            _PREFS_PATH.replace(_PREFS_PATH.with_name(_PREFS_PATH.name + ".corrupt"))
        except OSError:
            pass
    return {}


def load_prefs() -> dict:
    with _LOCK:
        return _read(for_write=False)


def save_prefs(prefs: dict) -> None:
    with _LOCK:
        try:
            paths.write_json(_PREFS_PATH, prefs)
        except OSError:
            pass  # non-fatal — prefs are a convenience, not a requirement


def update_prefs(patch: dict) -> dict:
    """Shallow-merge `patch` into the stored prefs and persist. Returns the
    full updated prefs dict."""
    with _LOCK:
        prefs = _read(for_write=True)
        prefs.update(patch)
        save_prefs(prefs)
        return prefs


def get_path_mapping(collection_path: str) -> dict | None:
    """The saved ``{from, to}`` OS-path remapping for a collection, or None.

    Keyed by the collection's current OS path (the mapping is per-machine data,
    so the local path is a natural, machine-specific key)."""
    mappings = load_prefs().get("path_mappings")
    if isinstance(mappings, dict):
        m = mappings.get(collection_path)
        if isinstance(m, dict):
            return {"from": m.get("from") or "", "to": m.get("to") or ""}
    return None


def set_path_mapping(
    collection_path: str, from_prefix: str | None, to_prefix: str | None
) -> None:
    """Persist (or, when either prefix is blank, clear) a collection's mapping."""
    with _LOCK:
        prefs = _read(for_write=True)
        mappings = prefs.get("path_mappings")
        if not isinstance(mappings, dict):
            mappings = {}
        if from_prefix and to_prefix:
            mappings[collection_path] = {"from": from_prefix, "to": to_prefix}
        else:
            mappings.pop(collection_path, None)
        prefs["path_mappings"] = mappings
        save_prefs(prefs)


def get_last_collection(platform: str | None = None) -> str | None:
    """The last library opened — overall, or on one platform.

    Per-platform because the picker asks which platform FIRST: offering a
    Rekordbox ``master.db`` under "Open last" to someone who just chose Traktor
    is an offer that cannot be taken. A platform with no record returns None
    rather than falling back to the global one, for the same reason.

    The flat ``last_collection`` stays: it is the global answer, and it is what
    libraries opened before this existed are recorded in.
    """
    prefs = load_prefs()
    if platform:
        by_platform = prefs.get("last_collection_by_platform")
        if isinstance(by_platform, dict):
            val = by_platform.get(platform)
            return val if isinstance(val, str) else None
        return None
    val = prefs.get("last_collection")
    return val if isinstance(val, str) else None


def set_last_collection(path: str, platform: str | None = None) -> None:
    """Record a library as the last opened, globally and for its platform."""
    with _LOCK:
        prefs = _read(for_write=True)
        prefs["last_collection"] = path
        if platform:
            by_platform = prefs.get("last_collection_by_platform")
            if not isinstance(by_platform, dict):
                by_platform = {}
            by_platform[platform] = path
            prefs["last_collection_by_platform"] = by_platform
        save_prefs(prefs)
