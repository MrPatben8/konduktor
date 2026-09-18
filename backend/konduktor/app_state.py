"""The one loaded library, and the version-history bookkeeping around saving it.

Exactly one library is open at a time: that keeps every edit and every save
unambiguous about what it targets, and it is why the adapter can own its own
projection without anyone asking which library a command meant.

Version history lives here rather than in the adapter. An adapter's job is to
write its own format faithfully; knowing that Konduktor keeps a git-backed
history of each library is app-level knowledge, and an adapter reaching up for it
would invert the dependency. So the adapter returns the bytes it wrote and a
human summary, and this module versions them.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import __version__, adapters, history, prefs  # noqa: F401 — registers adapters
from .core import registry
from .core.adapter import LibraryAdapter
from .core.pathmap import PathMapping


class AppState:
    """Holds the currently-loaded library (selectable at runtime)."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.adapter: LibraryAdapter | None = None

    @property
    def loaded(self) -> bool:
        return self.adapter is not None

    def open(self, path: Path) -> None:
        # The registry picks the adapter by probing the file, not by extension —
        # a Rekordbox library is a .db and a Serato one is a directory. Raises on
        # an unrecognised or unparseable file; only commit once it has parsed.
        # One parse: the adapter builds its read projection from the same object
        # graph the commands mutate.
        adapter = registry.open_library(path)
        # Release the previous library first. A file-backed platform (Rekordbox
        # holds master.db open) would otherwise leak a handle — and a lingering
        # write-ahead log beside a file another process replaces is how a SQLite
        # library gets corrupted. Only close once the new one has parsed, so a
        # failed open leaves the current library intact.
        previous = self.adapter
        if previous is not None and hasattr(previous, "close"):
            try:
                previous.close()
            except Exception:  # noqa: BLE001 — a failed close must not block the open
                pass
        # Apply this library's saved OS-path remapping (per-machine, keyed by the
        # library's local path) so runtime translation is live on open.
        saved = prefs.get_path_mapping(str(path))
        if saved:
            adapter.set_path_mapping(PathMapping.make(saved["from"], saved["to"]))
        self.path, self.adapter = path, adapter
        # Version history: record an "as I found it" baseline (deduped, so
        # re-opening an unchanged library is a no-op). Best-effort.
        history.ensure_baseline(path)

    def save(self):
        """Write the library, then record the saved bytes in version history.

        Returns ``(outcome, commit_sha | None)``. Every path that saves must come
        through here — a save that skips it leaves a gap in the history the user
        cannot restore from.
        """
        assert self.adapter is not None and self.path is not None
        outcome = self.adapter.save()
        # Not every platform is versioned. A library that is more than one file
        # has no single blob that IS the library, and it says so through its
        # capabilities rather than this module knowing which platforms those are.
        versioned = self.adapter.capabilities().save.history
        commit = None
        if versioned and outcome.snapshot is not None:
            commit = history.commit(
                self.path, outcome.snapshot, outcome.summary, __version__
            )
        return outcome, commit


STATE = AppState()

# Optional auto-load for dev/tests.
_env_nml = os.environ.get("KONDUKTOR_NML")
if _env_nml and Path(_env_nml).exists():
    try:
        STATE.open(Path(_env_nml))
    except Exception:  # noqa: BLE001 — bad env path shouldn't crash startup
        pass
