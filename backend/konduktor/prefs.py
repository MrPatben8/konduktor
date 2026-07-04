"""Tiny persisted user-preferences store.

Kept as a self-contained JSON file that travels with the app (resolved relative
to this package, not a per-OS config dir) so it works the same on every platform
and when the app is moved. Best-effort: any I/O error degrades to "no prefs".
"""
from __future__ import annotations

import json
from pathlib import Path

# backend/userprefs.json — one level up from the package (konduktor/).
_PREFS_PATH = Path(__file__).resolve().parent.parent / "userprefs.json"


def load_prefs() -> dict:
    try:
        data = json.loads(_PREFS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        _PREFS_PATH.write_text(json.dumps(prefs, indent=2))
    except OSError:
        pass  # non-fatal — prefs are a convenience, not a requirement


def update_prefs(patch: dict) -> dict:
    """Shallow-merge `patch` into the stored prefs and persist. Returns the
    full updated prefs dict."""
    prefs = load_prefs()
    prefs.update(patch)
    save_prefs(prefs)
    return prefs


def get_last_collection() -> str | None:
    val = load_prefs().get("last_collection")
    return val if isinstance(val, str) else None


def set_last_collection(path: str) -> None:
    prefs = load_prefs()
    prefs["last_collection"] = path
    save_prefs(prefs)
