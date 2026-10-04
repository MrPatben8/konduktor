"""Remote libraries: a library held by a Konduktor server, opened from here.

NOT a registered driver: a remote is not a file `can_open` could probe, and it
is chosen from the picker's own "Remote" step, by the saved remote's id. What
opening one returns is everything `AppState.open_remote` installs — the
adapter, the services that keep history and export sets on the server, and the
session that keeps this computer's hold on it.

Imports no platform adapter (`test_layering.py`): what platform the server
holds is the server's business, and reaches this side only as capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ... import exports
from . import config
from .adapter import RemoteAdapter
from .client import AuthFailed, InUse, RemoteClient, SessionLost, Unreachable, VersionMismatch
from .services import RemotePending, RemoteServices, RemoteSetStore
from .session import RemoteSession

__all__ = [
    "AuthFailed", "InUse", "Opened", "RemoteAdapter", "SessionLost", "Unreachable",
    "VersionMismatch", "handshake", "open_remote",
]


@dataclass
class Opened:
    remote: config.Remote
    path: Path
    adapter: RemoteAdapter
    services: RemoteServices
    session: RemoteSession
    library_id: str
    pending: object | None
    #: Edits another computer left unsaved: {machine, summary}, or None.
    pending_edits: dict | None


def open_remote(remote: config.Remote, *, takeover: bool = False, batch_probe=None,
                password: str | None = None, cache=None) -> Opened:
    """Connect, take the session, and mirror the library. Raises `InUse` when
    another computer holds it (and `takeover` is not set), `AuthFailed`,
    `VersionMismatch` or `Unreachable` — all before anything is installed."""
    secret = password if password is not None else config.password(remote.id)
    if not secret:
        raise AuthFailed("No password is saved for this remote — edit it to enter one")
    client = RemoteClient(remote, secret)
    try:
        client_id = config.client_id()
        hello = client.hello(client_id)
        machine = config.machine_name()
        acquired = client.acquire(client_id, machine, takeover=takeover)
        session = RemoteSession(client, batch_probe=batch_probe, identity=(client_id, machine))
        adapter = RemoteAdapter(client, session, hello, cache=cache)
        session.on_reacquired = adapter.refresh
    except Exception:
        client.release()
        client.close()
        raise
    # The server's id for the library, namespaced: this computer's export-set
    # store for it is the SERVER (`RemoteSetStore`), and nothing this computer
    # keeps under the server's own id may be mistaken for it.
    library_id = f"remote:{hello['library_id']}"
    exports.use_store(library_id, RemoteSetStore(client))
    session.start()
    return Opened(
        remote=remote, path=Path(hello.get("path") or remote.name), adapter=adapter,
        services=RemoteServices(client), session=session, library_id=library_id,
        pending=RemotePending(client), pending_edits=acquired.get("pending_edits"),
    )


def handshake(remote: config.Remote, password: str) -> list[dict]:
    """Try each of the remote's addresses on its own: what a Save of the
    remote's settings reports. Each result is {address, url, ok, error, kind},
    `kind` one of ok / unreachable / auth / version / other."""
    results = []
    for label, url in remote.addresses():
        single = config.Remote(id=remote.id, name=remote.name, host=url, username=remote.username)
        client = RemoteClient(single, password, timeout=8.0)
        try:
            info = client.hello()
            results.append({"address": label, "url": url, "ok": True, "error": None, "kind": "ok",
                            "name": info.get("name"), "platform": info.get("platform")})
        except Unreachable as ex:
            results.append({"address": label, "url": url, "ok": False, "error": str(ex), "kind": "unreachable"})
        except AuthFailed as ex:
            results.append({"address": label, "url": url, "ok": False, "error": str(ex), "kind": "auth"})
        except VersionMismatch as ex:
            results.append({"address": label, "url": url, "ok": False, "error": str(ex), "kind": "version"})
        except Exception as ex:  # noqa: BLE001 — anything else is that address's answer
            results.append({"address": label, "url": url, "ok": False, "error": str(ex), "kind": "other"})
        finally:
            client.close()
    return results
