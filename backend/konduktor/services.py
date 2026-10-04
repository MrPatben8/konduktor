"""The app-level bookkeeping around ONE open library, wherever it is kept.

Version history belongs to the app, not the adapter (see `app_state.py`), and
until a library could live on a server it could only mean one thing: a git repo
in this computer's app-data, keyed by the library's local path. A library held
by a server is versioned THERE, beside the file it describes — so the open
library carries the services that know where its history lives, and the routes
ask them rather than the history module directly.

`LocalServices` is the whole of what used to be inline: nothing about a library
on this computer changes. The remote implementation lives with the remote
adapter (`adapters/remote/services.py`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import __version__, history
from .core import registry


class LibraryServices(Protocol):
    #: Whether this library is held by a server (its history lives there).
    remote: bool

    def baseline(self) -> None: ...
    def commit_save(self, adapter, outcome) -> str | None: ...
    def history(self) -> list[history.HistoryEntry]: ...
    def restore(self, commit_id: str) -> bool: ...
    def clear_history(self) -> None: ...


class LocalServices:
    """History in this computer's app-data, keyed by the library's path."""

    remote = False

    def __init__(self, path: Path):
        self.path = path

    def baseline(self) -> None:
        # An "as I found it" version (deduped, so re-opening an unchanged
        # library is a no-op). Best-effort.
        history.ensure_baseline(self.path)

    def commit_save(self, adapter, outcome) -> str | None:
        # Not every platform is versioned. A library that is more than one file
        # has no single blob that IS the library, and it says so through its
        # capabilities rather than this module knowing which platforms those are.
        if adapter.capabilities().save.history and outcome.snapshot is not None:
            return history.commit(self.path, outcome.snapshot, outcome.summary, __version__)
        return None

    def history(self) -> list[history.HistoryEntry]:
        return history.list_history(self.path)

    def restore(self, commit_id: str) -> bool:
        """Write a past version back as a NEW forward save. False if unknown.
        The caller reopens the library afterwards."""
        data = history.read_version(self.path, commit_id)
        if data is None:
            return False
        # The adapter's driver owns writing its own format back, even for a restore.
        registry.driver_for(self.path).restore(self.path, data)
        history.commit(self.path, data, f"Restored version {commit_id[:8]}", __version__)
        return True

    def clear_history(self) -> None:
        history.clear_history(self.path)
