"""One computer at a time: the server's session lock.

A library held by a server is edited by ONE computer at a time (decided: a
shared or merged session was rejected). That computer holds the session; every
command carries its token, and anything else is told who holds it.

  * **A lease, renewed by heartbeat.** The holder's app sends one every
    `HEARTBEAT_SECONDS`; `LEASE_SECONDS` without one and the session is free.
    A crashed laptop or a closed lid therefore never locks the library for
    good, and a Wi-Fi blip does not lose it.
  * **Takeover** is how a second computer gets in before the lease runs out:
    it is told who holds the session (and what batch is running there) and may
    take it. The old token is refused from then on, which is how the displaced
    computer finds out.
  * **The same computer coming back** — after sleep, after a reconnect through
    its fallback address — renews its expired lease silently, as long as
    nobody else has taken the session meanwhile.

Unsaved edits are NOT the session's: they live in the one native model the
server holds, so whoever holds the session next inherits them (and is asked to
save or discard them — `last_editor` says whose they were).
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..remote_protocol import LEASE_SECONDS


@dataclass
class Holder:
    token: str
    client_id: str
    machine: str
    since: str  # ISO 8601, UTC
    last_seen: float  # on the lock's clock
    batch: str | None = None  # the kind of batch job running there, if any
    uploads: set[str] = field(default_factory=set)


class InUse(Exception):
    """Another computer holds the session."""

    def __init__(self, holder: Holder):
        super().__init__(f"In use by {holder.machine}")
        self.holder = holder


class Lost(Exception):
    """This token no longer holds the session."""

    def __init__(self, holder: Holder | None):
        super().__init__("Another computer has taken over this library" if holder
                         else "This computer no longer holds the session")
        self.holder = holder


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionLock:
    def __init__(self, *, lease: float = LEASE_SECONDS, clock=time.monotonic):
        self.lease = lease
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: Holder | None = None
        #: The machine whose commands made the unsaved edits (see `touch`).
        self.last_editor: dict | None = None

    # ---- reading --------------------------------------------------------------
    def _live(self, holder: Holder | None) -> bool:
        return holder is not None and self._clock() - holder.last_seen < self.lease

    def holder(self) -> Holder | None:
        """Who holds the session right now (None: free, or the lease ran out)."""
        with self._lock:
            return self._holder if self._live(self._holder) else None

    def describe(self) -> dict | None:
        h = self.holder()
        return None if h is None else {"machine": h.machine, "since": h.since, "batch": h.batch}

    # ---- changing -------------------------------------------------------------
    def acquire(self, client_id: str, machine: str, *, takeover: bool = False) -> Holder:
        """Hold the session. Raises `InUse` when another computer holds it and
        `takeover` is not set. The same client re-acquiring keeps its uploads."""
        with self._lock:
            current = self._holder
            live = self._live(current)
            if live and current.client_id != client_id and not takeover:
                raise InUse(current)
            uploads = current.uploads if current is not None and current.client_id == client_id else set()
            self._holder = Holder(
                token=secrets.token_urlsafe(24), client_id=client_id, machine=machine,
                since=_now_iso(), last_seen=self._clock(), uploads=uploads,
            )
            return self._holder

    def check(self, token: str | None) -> Holder:
        """The holder this token names, renewing its lease. Raises `Lost`.

        A token whose lease ran out is still honoured when nobody else has
        taken the session since — the same computer back from sleep."""
        with self._lock:
            current = self._holder
            if current is None or not token or not secrets.compare_digest(current.token, token):
                raise Lost(current if self._live(current) else None)
            current.last_seen = self._clock()
            return current

    def heartbeat(self, token: str | None, batch: str | None) -> Holder:
        holder = self.check(token)
        holder.batch = batch
        return holder

    def release(self, token: str | None) -> None:
        with self._lock:
            current = self._holder
            if current is not None and token and secrets.compare_digest(current.token, token):
                self._holder = None

    def touch(self, holder: Holder) -> None:
        """Record that `holder` made an edit — whose the unsaved ones are."""
        self.last_editor = {"client_id": holder.client_id, "machine": holder.machine}

    def clear_editor(self) -> None:
        """After a save or discard, nobody's edits are pending."""
        self.last_editor = None
