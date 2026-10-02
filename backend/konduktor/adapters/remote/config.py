"""Saved remotes: the servers this computer knows how to reach.

A remote is where a Konduktor server is (an address, an optional port, and a
fallback address — typically the LAN address first and a VPN one second) and
who to sign in as. Everything but the password is a plain preference
(`userprefs.json`, key `remotes`); the password lives in the OS keychain
(macOS Keychain, Windows Credential Manager) through `keyring`, never in a JSON
file beside the app's other data.

This computer also has a stable `client_id`, which is how a server tells "the
same laptop back from sleep" (renew its session silently) from "another
computer" (offer a takeover).
"""
from __future__ import annotations

import socket
import uuid
from dataclasses import asdict, dataclass

from ... import prefs

DEFAULT_PORT = 8765
_PREFS_KEY = "remotes"
_CLIENT_KEY = "remoteClientId"
_KEYRING_SERVICE = "Konduktor"


@dataclass
class Remote:
    id: str
    name: str
    host: str
    port: int | None = None
    fallback_host: str | None = None
    fallback_port: int | None = None
    username: str = ""

    def addresses(self) -> list[tuple[str, str]]:
        """`(label, base url)`, primary first — the order every connect tries."""
        out = [("primary", _base(self.host, self.port))]
        if self.fallback_host:
            out.append(("fallback", _base(self.fallback_host, self.fallback_port or self.port)))
        return out

    def public(self) -> dict:
        return asdict(self)


def _base(host: str, port: int | None) -> str:
    host = host.strip()
    if host.startswith("http://") or host.startswith("https://"):
        # A full URL (behind the user's own reverse proxy): taken as given.
        return host.rstrip("/")
    bare, inline = split_host(host)
    if inline is not None:
        host, port = bare, port or inline
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # an IPv6 literal
    return f"http://{host}:{port or DEFAULT_PORT}"


def split_host(text: str) -> tuple[str, int | None]:
    """`"nas.local:9000"` → `("nas.local", 9000)`; a bare host keeps no port."""
    text = (text or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text, None
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host, int(port)
    return text, None


# ---- the list ------------------------------------------------------------------


def remotes() -> list[Remote]:
    raw = prefs.load_prefs().get(_PREFS_KEY) or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(Remote(**{k: item.get(k) for k in Remote.__dataclass_fields__ if k in item}))
        except TypeError:
            continue
    return out


def get(remote_id: str) -> Remote | None:
    return next((r for r in remotes() if r.id == remote_id), None)


def save(remote: Remote) -> Remote:
    items = [r for r in remotes() if r.id != remote.id] + [remote]
    prefs.update_prefs({_PREFS_KEY: [r.public() for r in items]})
    return remote


def delete(remote_id: str) -> bool:
    before = remotes()
    items = [r for r in before if r.id != remote_id]
    prefs.update_prefs({_PREFS_KEY: [r.public() for r in items]})
    forget_password(remote_id)
    return len(items) != len(before)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- this computer ---------------------------------------------------------------


def client_id() -> str:
    current = prefs.load_prefs().get(_CLIENT_KEY)
    if isinstance(current, str) and current:
        return current
    fresh = uuid.uuid4().hex
    prefs.update_prefs({_CLIENT_KEY: fresh})
    return fresh


def machine_name() -> str:
    """What another computer is told holds the library ("Ben's MacBook")."""
    try:
        import subprocess
        import sys

        if sys.platform == "darwin":
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                                  text=True, timeout=2).stdout.strip()
            if name:
                return name
    except Exception:  # noqa: BLE001
        pass
    name = socket.gethostname()
    return name[:-6] if name.endswith(".local") else name


# ---- the password ------------------------------------------------------------------
# A seam for tests (and any machine without a keychain): `use_password_store`.


class _Keyring:
    def get(self, account: str) -> str | None:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, account)

    def set(self, account: str, password: str) -> None:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, account, password)

    def delete(self, account: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(_KEYRING_SERVICE, account)
        except PasswordDeleteError:
            pass


_store = _Keyring()


def use_password_store(store) -> None:
    """Replace the keychain (`get`/`set`/`delete` by account) — tests only."""
    global _store
    _store = store if store is not None else _Keyring()


def _account(remote_id: str) -> str:
    return f"remote:{remote_id}"


def password(remote_id: str) -> str | None:
    try:
        return _store.get(_account(remote_id))
    except Exception:  # noqa: BLE001 — a locked keychain reads as "no password"
        return None


def set_password(remote_id: str, value: str) -> None:
    _store.set(_account(remote_id), value)


def forget_password(remote_id: str) -> None:
    try:
        _store.delete(_account(remote_id))
    except Exception:  # noqa: BLE001
        pass
