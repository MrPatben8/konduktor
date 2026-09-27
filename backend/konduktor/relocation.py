"""The open-time missing-files check: search on open, ask once, apply for the session.

When a library opens, a thread asks the adapter which stored volumes resolve
nothing and searches for where their tracks went (`core/relocate.py`). The
result waits here until the user answers the confirmation dialog — Apply or
Not now — and is then settled for the rest of this open. Reopening the library
creates a new adapter and so a new check, which is how "ask again when the
library is reopened" holds without anything being remembered.

It lives on `AppState` rather than in the UI because "once per open" is a fact
about the open, which happens here: a UI deciding it would re-ask on every
refetch of the query that shows the dialog.

The mappings it applies are session-only by design. They are never written to
the library (that would change every Traktor track id) nor to prefs; the saved
mapping in the Path Remapping dialog is the way to make one stick.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .core import places
from .core.pathmap import PathMapping
from .core.relocate import Proposal, search

log = logging.getLogger(__name__)


def candidate_roots() -> list[Path]:
    """Where a library's missing audio might now be: every drive, plus the
    user's home and Music folders (a library copied into ~/Music keeps its own
    layout beneath it)."""
    roots = [Path(d["path"]) for d in places.drives()]
    roots += [Path(p["path"]) for p in places.user_places() if p["kind"] in ("home", "music")]
    return roots


class RelocationCheck:
    """One library open's check. Starts searching as soon as it is made."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self._done = threading.Event()
        self._proposals: list[Proposal] = []
        self._answered = False
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="relocation-check", daemon=True).start()

    def _run(self) -> None:
        try:
            groups = self.adapter.unresolved_path_groups()
            if groups:
                self._proposals = search(groups, candidate_roots())
        except Exception:  # noqa: BLE001 — a failed check must never break an open
            log.exception("missing-files check failed")
        finally:
            self._done.set()

    def wait(self, timeout: float) -> bool:
        """Whether the search has finished, waiting up to `timeout` seconds."""
        return self._done.wait(timeout)

    @property
    def pending(self) -> list[Proposal]:
        """What still needs the user's answer: nothing once answered."""
        if not self._done.is_set() or self._answered:
            return []
        return self._proposals

    def answer(self, mappings: list[PathMapping]) -> int:
        """Apply the user's choice (empty = Not now) and settle the check.

        Returns how many tracks the applied mappings resolve. Refuses a mapping
        the search did not propose, and two for one volume.
        """
        with self._lock:
            if not self._done.is_set():
                raise ValueError("The missing-files check is still running")
            if self._answered:
                raise ValueError("The missing-files check has already been answered")
            chosen = []
            volumes: set[str] = set()
            for m in mappings:
                match = next(
                    (
                        (p, c)
                        for p in self._proposals
                        for c in p.candidates
                        if c.mapping == m
                    ),
                    None,
                )
                if match is None:
                    raise ValueError(f"Not a proposed mapping: {m.from_prefix} → {m.to_prefix}")
                if match[0].root in volumes:
                    raise ValueError(f"Two mappings chosen for {match[0].label}")
                volumes.add(match[0].root)
                chosen.append(match[1])
            self.adapter.set_session_mappings([c.mapping for c in chosen])
            self._answered = True
            return sum(c.found for c in chosen)
