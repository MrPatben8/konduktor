"""A remote library's app-level bookkeeping — kept on the server, beside it.

Version history, export sets and the stems ledger belong with the library, not
with whichever computer opened it: they are what every computer that opens it
should see. So for a remote library these are calls to the server, and this
computer's app-data holds none of them (`services.py` is the local half).
"""
from __future__ import annotations

from ...core.adapter import NotFound
from ...history import HistoryEntry
from .client import RemoteClient


class RemoteServices:
    remote = True

    def __init__(self, client: RemoteClient):
        self.client = client

    def baseline(self) -> None:
        pass  # the server records its own "as I found it" version when it opens

    def commit_save(self, adapter, outcome) -> str | None:
        # The server versioned the save itself; its commit rides on the outcome.
        return getattr(outcome, "commit", None)

    def history(self) -> list[HistoryEntry]:
        return [HistoryEntry(**e) for e in self.client.history()]

    def restore(self, commit_id: str) -> bool:
        try:
            self.client.restore(commit_id)
        except NotFound:
            return False
        return True

    def clear_history(self) -> None:
        self.client.clear_history()

    def discard(self) -> None:
        self.client.discard()


class RemoteSetStore:
    """`exports.use_store`'s store for a remote library: its sets, on the server."""

    def __init__(self, client: RemoteClient):
        self.client = client

    def read(self) -> dict:
        return self.client.export_sets()

    def write(self, doc: dict) -> None:
        self.client.put_export_sets(doc)


class RemotePending:
    """`AppState.pending` for a remote library: the SERVER keeps the ledger of
    converted tracks awaiting Save (and commits, restores and recovers it
    itself), so on this computer there is nothing to commit or restore — only
    the server's summary to report, and its "blocking" to honour."""

    items: list = []

    def __init__(self, client: RemoteClient):
        self.client = client

    def summary(self) -> dict:
        try:
            return self.client.stems_pending()
        except Exception:  # noqa: BLE001 — out of reach: nothing known to be pending
            return {"tracks": 0, "parked": 0, "bytes": 0}

    def blocking(self) -> bool:
        return bool(self.summary().get("tracks"))

    def swapped(self) -> list:
        return []

    def restore(self) -> int:
        return 0

    def commit(self) -> dict:
        return {}
