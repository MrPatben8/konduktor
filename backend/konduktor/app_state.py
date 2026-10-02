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
import threading
from pathlib import Path

from . import adapters, library_id, prefs  # noqa: F401 — registers adapters
from .core import registry
from .core.adapter import LibraryAdapter
from .core.pathmap import PathMapping
from .relocation import RelocationCheck
from .services import LibraryServices, LocalServices


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
        # This open's missing-files check (see `relocation.py`). A new one per
        # open, which is what makes the dialog ask again on a reopen.
        self.relocation: RelocationCheck | None = None
        # Serialises the operations that REPLACE or COMMIT the whole library —
        # open, save, discard (and a stem batch's swap step). A save landing in
        # the middle of another would commit a half-applied state; reentrant so
        # one of them may call another.
        self.mutation = threading.RLock()
        # Converted-to-stem tracks awaiting Save or Discard (see stems/pending.py),
        # and what the last open's crash recovery did about any left over.
        self.pending = None
        self.recovery: dict | None = None
        # Where this library's history (and, for a remote, its export sets and
        # ledger) are kept — see `services.py`.
        self.services: LibraryServices | None = None
        # The open remote's connection (`adapters/remote/session.py`), or None
        # for a library on this computer.
        self.remote = None
        # …and the saved remote it was opened through (`adapters/remote/config.py`).
        self.remote_config = None
        # Called with the adapter at the end of every open (and so after a
        # restore, which reopens): how a Konduktor SERVER re-applies the path
        # mapping its configuration names, which lives in no prefs file.
        self.open_hooks: list = []

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
        adapter = registry.open_library(path, read_only=True)
        if adapter.capabilities().writable:
            if hasattr(adapter, "close"):
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass
            raise ValueError(
                f"{path} cannot be opened read-only; only a library that can "
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
        with self.mutation:
            self._open(path)

    def _open(self, path: Path) -> None:
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
        self._release_previous()
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
        # A crash may have left converted tracks mid-lifecycle: settle them from
        # what the SAVED library says, BEFORE the missing-files check — a parked
        # original is exactly what that check would report as missing.
        from .stems.pending import Pending

        self.pending = Pending(self.library_id)
        self.recovery = None
        if self.pending.items:
            self.recovery = self.pending.recover(lambda item: adapter.track(item["new_id"]) is not None)
        # Started after the saved mapping is applied, so a volume that mapping
        # already fixes is not asked about.
        self.relocation = RelocationCheck(adapter)
        self.services = LocalServices(path)
        self.services.baseline()
        for hook in self.open_hooks:
            hook(adapter)

    def _release_previous(self) -> None:
        previous = self.adapter
        # Leaving a library discards its unsaved edits (the UI has confirmed), so
        # a stem conversion's parked originals go back under their own names.
        if self.pending is not None and self.pending.items:
            self.pending.restore()
        if previous is not None and hasattr(previous, "close"):
            try:
                previous.close()
            except Exception:  # noqa: BLE001 — a failed close must not block the open
                pass
        if self.remote is not None and self.library_id:
            # A remote library's export sets were the server's; stop asking it.
            from . import exports

            exports.use_store(self.library_id, None)
        self.adapter = None
        self.remote = None
        self.remote_config = None

    def open_remote(self, opened) -> None:
        """Make an already-connected remote library THE library.

        `opened` is what `adapters.remote.open_remote` returns: the adapter, its
        services and session, and the server's identity for the library. The
        connection (handshake, version check, session) is made BEFORE this, so
        a refusal leaves the current library intact, as a failed parse does.
        """
        with self.mutation:
            self._release_previous()
            self.path = opened.path
            self.adapter = opened.adapter
            self.services = opened.services
            self.remote = opened.session
            self.remote_config = opened.remote
            # The server's id for the library: export sets are keyed by it, and
            # they live on the server too, so every computer sees the same ones.
            self.library_id = opened.library_id
            # The server keeps its own ledger of converted tracks; nothing of a
            # remote library's lives in this computer's.
            self.pending = opened.pending
            self.recovery = None
            # A remote library's files are not on this computer's drives.
            self.relocation = None

    def save(self):
        """Write the library, then record the saved bytes in version history.

        Returns ``(outcome, commit_sha | None)``. Every path that saves must come
        through here — a save that skips it leaves a gap in the history the user
        cannot restore from.
        """
        assert self.adapter is not None and self.path is not None
        with self.mutation:
            return self._save()

    def _save(self):
        outcome = self.adapter.save()
        commit = self.services.commit_save(self.adapter, outcome)
        # The swap is now on disk: what it replaced can go. After the library
        # write, so a failed save deletes nothing; here rather than in the save
        # route, because import and path remap save too.
        if self.pending is not None and self.pending.swapped():
            renames = self.pending.commit()
            if renames and self.library_id:
                from . import exports

                exports.retarget(self.library_id, renames)
        return outcome, commit

    def discard(self) -> None:
        """Drop every unsaved edit: the library re-reads itself from disk.

        Through the adapter's own `reload`, NOT `open` — reopening would start a
        new missing-files check and forget this session's relocation answers,
        which are not edits and survive a reload (the store keeps them).
        """
        assert self.adapter is not None
        with self.mutation:
            if self.pending is not None and self.pending.items:
                self.pending.restore()
            if self.services is not None and self.services.remote:
                # The server holds the edits; it is what drops them.
                self.services.discard()
            self.adapter.reload()

    def restore_version(self, commit_id: str) -> bool:
        """Write a past version back as a new save and re-read the library.
        False when the version is unknown."""
        assert self.adapter is not None and self.services is not None
        with self.mutation:
            if not self.services.restore(commit_id):
                return False
            if self.services.remote:
                # The server reopened its own library; follow it.
                self.adapter.reload()
            else:
                self._open(self.path)  # rebuild read + edit models from the restored file
            return True


STATE = AppState()

# Optional auto-load for dev/tests.
_env_nml = os.environ.get("KONDUKTOR_NML")
if _env_nml and Path(_env_nml).exists():
    try:
        STATE.open(Path(_env_nml))
    except Exception:  # noqa: BLE001 — bad env path shouldn't crash startup
        pass
