"""This computer's hold on a remote library's session.

The server lets ONE computer edit at a time (`server/session.py`); this keeps
this computer's hold alive with a heartbeat every `HEARTBEAT_SECONDS`, and is
the one place that knows how this computer stands:

  connected     — holding the session; edits go through
  reconnecting  — a heartbeat failed to reach the server; edits still try
                  (each request walks the addresses again)
  offline       — several in a row failed; read-only until one gets through
  taken_over    — another computer took the session; read-only, for good

Coming back from `reconnecting` or `offline` needs no new sign-in: the server
renews a lapsed lease for the computer that held it, as long as nobody else
has taken it meanwhile — and says so (423) if somebody has.

The heartbeat names the batch running here, so a computer asking to take over
is told what it would cancel. Being taken over cancels it (`on_change`).
"""
from __future__ import annotations

import logging
import threading

from ...core.adapter import Unavailable
from ...remote_protocol import HEARTBEAT_SECONDS
from .client import RemoteClient, SessionLost

log = logging.getLogger(__name__)

#: Missed heartbeats in a row before `reconnecting` becomes `offline`.
OFFLINE_AFTER = 3


class RemoteSession:
    def __init__(self, client: RemoteClient, *, batch_probe=None, interval: float = HEARTBEAT_SECONDS,
                 identity: tuple[str, str] | None = None):
        self.client = client
        self.interval = interval
        self._batch_probe = batch_probe or (lambda: None)
        #: (client_id, machine) — to take the session back when the server has
        #: forgotten it (a restart) and nobody else holds it.
        self.identity = identity
        self.state = "connected"
        self.machine: str | None = None  # who took it over
        #: The last thing worth telling the user that is not a state ("the
        #: server restarted"), with a counter so the UI shows each once.
        self.notice: str | None = None
        self.notices = 0
        #: Called after the session was taken back: the library must be re-read.
        self.on_reacquired = None
        self._failures = 0
        self._listeners: list = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- facts ---------------------------------------------------------------------
    @property
    def writable(self) -> bool:
        return self.state in ("connected", "reconnecting")

    @property
    def readonly_cause(self) -> str | None:
        return {"taken_over": "taken_over", "offline": "offline"}.get(self.state)

    def describe(self) -> dict:
        return {"state": self.state, "via": self.client.via if self.state != "offline" else None,
                "machine": self.machine, "notice": self.notice, "notices": self.notices}

    def on_change(self, listener) -> None:
        """`listener(state)` after every change of state (from the heartbeat thread)."""
        self._listeners.append(listener)

    # ---- running -------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="konduktor-remote-heartbeat",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.state == "taken_over":
                return
            self.beat()

    def beat(self) -> None:
        try:
            self.client.heartbeat(self._batch_probe())
        except SessionLost as ex:
            self.lost(ex.holder)
            return
        except Unavailable:
            self._failures += 1
            self._set("offline" if self._failures >= OFFLINE_AFTER else "reconnecting")
            return
        except Exception:  # noqa: BLE001 — a bad answer is a missed beat, not a crash
            log.exception("heartbeat failed")
            self._failures += 1
            self._set("offline" if self._failures >= OFFLINE_AFTER else "reconnecting")
            return
        self._failures = 0
        self._set("connected")

    def _take_back(self) -> bool:
        """The server no longer knows this computer's token and nobody holds the
        session — it restarted (unsaved edits went with it, an accepted loss).
        Take the session back without asking, unless the library now holds
        someone ELSE's unsaved edits: those deserve the Save / Discard question
        that reopening asks, so then this stays read-only."""
        if self.identity is None:
            return False
        try:
            got = self.client.acquire(*self.identity)
        except Exception:  # noqa: BLE001 — someone got there first, or no server
            return False
        if got.get("pending_edits"):
            self.client.release()
            return False
        if self.on_reacquired is not None:
            try:
                self.on_reacquired()
            except Exception:  # noqa: BLE001
                log.exception("re-reading the library after a reconnect failed")
        self.notice = "The server restarted — any edits not yet saved there were lost"
        self.notices += 1
        self._failures = 0
        self._set("connected")
        return True

    def lost(self, holder: dict | None) -> None:
        """Another computer holds the session — from a heartbeat, or a command
        the server refused with 423. Nobody holding it means the server forgot
        this computer's token (a restart): take it back if that is safe."""
        if holder is None and self.state != "taken_over" and self._take_back():
            return
        self.machine = (holder or {}).get("machine")
        self._set("taken_over")

    def reached(self) -> None:
        """A request got through: whatever the heartbeat last saw, the server is there."""
        if self.state in ("reconnecting", "offline"):
            self._failures = 0
            self._set("connected")

    def _set(self, state: str) -> None:
        with self._lock:
            if state == self.state:
                return
            self.state = state
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:  # noqa: BLE001
                log.exception("remote session listener failed")
