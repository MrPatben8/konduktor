"""Is a DJ app running on this computer right now?

Asked before a push writes into the app's own library, which must not happen
under a running app: Traktor writes `collection.nml` back when it QUITS, so a
push made while it runs is silently undone, and rekordbox holds `master.db`
open. Answered from the process list (`psutil`), by executable name:

* rekordbox — exactly `rekordbox` (pyrekordbox's own test, minus `.exe`). Its
  helpers (`rekordboxAgent`, `rb_http_server`, `edb_streamd`) are other names,
  and `rekordboxAgent` runs whenever rekordbox is installed, so a prefix test
  would refuse forever.
* Traktor — any name starting with `traktor`: the macOS executable is
  `Traktor Pro 4` (read from the 4.5 bundle, 2026-10-20); the Windows one is
  not measured, and a prefix covers `Traktor.exe` and `Traktor Pro 4.exe`
  alike. Konduktor's own processes start with `konduktor`, not `traktor`.

A process list that cannot be read answers "not running": this is a guard
against a mistake, and refusing every push on a locked-down machine would make
the feature unusable rather than safe.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _names() -> list[str]:
    try:
        import psutil
    except ImportError:  # the server image does not ship it, and never pushes
        return []
    out = []
    try:
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name")
            if name:
                out.append(os.path.splitext(name)[0].lower())
    except Exception as ex:  # noqa: BLE001 — see the module docstring
        log.warning("could not read the process list: %s", ex)
    return out


def _matches(platform: str, name: str) -> bool:
    if platform == "traktor":
        return name.startswith("traktor")
    if platform == "rekordbox":
        return name == "rekordbox"
    return False


def running(platforms: list[str]) -> set[str]:
    """Which of `platforms` (platform ids) have their app running now."""
    names = _names()
    return {p for p in platforms if any(_matches(p, n) for n in names)}
