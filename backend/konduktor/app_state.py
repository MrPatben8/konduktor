"""The loaded library, and the version-history bookkeeping around saving it.

Exactly one library is open **for editing** at a time: that keeps every edit and
every save unambiguous about what it targets, and it is why the adapter can own
its own projection without anyone asking which library a command meant.

A **source** may be open alongside it, and only alongside it. A source is a
library being read FROM — a plugged-in OneLibrary stick whose tracks are about to
be imported — and it is deliberately not symmetrical with the loaded library:

  * it is **never written**, so no command can be ambiguous about its target;
  * it is **never saved**, so version history has nothing to disambiguate;
  * it is **never the fallback** when nothing is loaded — a source with no
    destination is a browser, not a library, and the routes say so.

That asymmetry is what keeps "exactly one library is open" true in the sense that
mattered: there is still exactly one thing a command can mean.

Version history lives here rather than in the adapter. An adapter's job is to
write its own format faithfully; knowing that Konduktor keeps a git-backed
history of each library is app-level knowledge, and an adapter reaching up for it
would invert the dependency. So the adapter returns the bytes it wrote and a
human summary, and this module versions them.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import __version__, adapters, history, library_id, prefs  # noqa: F401 — registers adapters
from .core import registry
from .core.adapter import LibraryAdapter
from .core.pathmap import PathMapping


class AppState:
    """Holds the currently-loaded library (selectable at runtime)."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.adapter: LibraryAdapter | None = None
        # This library's stable identity, which survives it being moved. What
        # export sets are keyed by; see `library_id.py` for why a path is not
        # good enough for curated user work.
        self.library_id: str | None = None
        # The library being read FROM, if any. Read-only by contract; see the
        # module docstring for why it is not symmetrical with `adapter`.
        self.source_path: Path | None = None
        self.source: LibraryAdapter | None = None

    @property
    def loaded(self) -> bool:
        return self.adapter is not None

    @property
    def source_loaded(self) -> bool:
        return self.source is not None

    # ---- the source library ---------------------------------------------
    def open_source(self, path: Path) -> None:
        """Open a library to read from, alongside the loaded one.

        Refuses a source that is not read-only. Nothing downstream sends it a
        command, but "this is only ever read" is the assumption the whole slot
        rests on, and an adapter is the thing that knows whether it is true —
        so it is asserted here rather than assumed everywhere else.
        """
        adapter = registry.open_library(path)
        if adapter.capabilities().writable:
            if hasattr(adapter, "close"):
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass
            raise ValueError(
                f"{path} is a writable library; only read-only sources "
                "(such as a OneLibrary drive) can be opened as a source"
            )
        self.close_source()
        self.source_path, self.source = path, adapter

    def close_source(self) -> None:
        """Release the source. Load-bearing: a source lives on a REMOVABLE drive,
        and a held file handle is what stops a stick ejecting."""
        previous, self.source = self.source, None
        self.source_path = None
        if previous is not None and hasattr(previous, "close"):
            try:
                previous.close()
            except Exception:  # noqa: BLE001 — a failed close must not block
                pass

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
        caps = adapter.capabilities()
        # No sidecar beside a removable library: writing to a user's USB stick to
        # satisfy Konduktor's own bookkeeping is not a trade worth making. Those
        # get a path-derived id instead, which `is_durable()` reports honestly.
        driver = next(
            (d for d in registry.drivers() if d.platform == caps.platform), None
        )
        self.library_id = library_id.for_path(
            path,
            platform=caps.platform,
            label=Path(str(path)).name,
            sidecar=not getattr(driver, "removable", False),
        )
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
