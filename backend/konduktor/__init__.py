"""Konduktor — Traktor library management & track preparation."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _read_version() -> str:
    """Return the app version — the single source of truth is
    ``frontend/package.json`` (also what Tauri names the installers from).

    - Dev / source checkout: read it from the repo, two levels up from this
      package (``<repo>/frontend/package.json``).
    - Frozen sidecar: ``package.json`` is bundled at the PyInstaller root (see
      ``konduktor-sidecar.spec``), reachable via ``sys._MEIPASS``.

    Falls back to ``"0.0.0"`` if it can't be located (the FastAPI version string
    is cosmetic, so a miss must never crash startup).
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "package.json")
    candidates.append(Path(__file__).resolve().parents[2] / "frontend" / "package.json")
    for path in candidates:
        try:
            return json.loads(path.read_text(encoding="utf-8"))["version"]
        except Exception:
            continue
    return "0.0.0"


__version__ = _read_version()
