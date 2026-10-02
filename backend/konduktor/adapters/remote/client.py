"""Talking to a Konduktor server: addresses, sign-in, and the error vocabulary.

Every request tries the address that last worked, then the others in the
remote's order (primary, then fallback) — so a laptop that leaves the house
moves from the LAN address to the VPN one without being asked, and back again.
Only a CONNECT failure moves on: the request never reached that server, so
sending it to the other one cannot apply it twice. A request that reached a
server and then failed is that server's answer.

The server's refusals come back as the adapter's own error classes (the same
status codes `main.py` maps them from), so a route on this computer reports a
remote library's refusal exactly as it would a local one's.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx

from ... import remote_protocol as rp
from ...core.adapter import (
    AdapterError,
    InvalidCommand,
    LibraryNotSupported,
    NotFound,
    Unavailable,
    Unsupported,
)
from .config import Remote

log = logging.getLogger(__name__)

_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class Unreachable(Unavailable):
    """No address of the remote answered."""


class AuthFailed(LibraryNotSupported):
    """The server refused the username or password."""


class VersionMismatch(LibraryNotSupported):
    def __init__(self, update: str, server_version: str):
        side = "Konduktor on this computer" if update == "app" else "the Konduktor server"
        super().__init__(
            f"This server speaks a different version of Konduktor's protocol "
            f"(server {server_version}): update {side}."
        )
        self.update = update


class InUse(AdapterError):
    """Another computer holds the session: `holder` = {machine, since, batch}."""

    def __init__(self, holder: dict):
        super().__init__(f"In use by {holder.get('machine') or 'another computer'}")
        self.holder = holder


class SessionLost(Unavailable):
    """This computer no longer holds the session (another one took over)."""

    def __init__(self, holder: dict | None):
        who = (holder or {}).get("machine")
        super().__init__(
            f"{who} has taken over this library — it is read-only here" if who
            else "This computer no longer holds the library's session"
        )
        self.holder = holder


def _detail(response: httpx.Response):
    try:
        return response.json().get("detail")
    except Exception:  # noqa: BLE001
        return response.text[:300]


class RemoteClient:
    def __init__(self, remote: Remote, password: str, *, timeout: float = 30.0):
        self.remote = remote
        self._addresses = remote.addresses()
        self._active: int | None = None
        self.token: str | None = None
        self._lock = threading.Lock()
        self._http = httpx.Client(
            auth=(remote.username, password),
            timeout=httpx.Timeout(timeout, connect=4.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def via(self) -> str | None:
        """Which address is in use: "primary" / "fallback" (None until one answers)."""
        return None if self._active is None else self._addresses[self._active][0]

    # ---- transport ----------------------------------------------------------------
    def _order(self) -> list[int]:
        everything = list(range(len(self._addresses)))
        if self._active is None:
            return everything
        return [self._active] + [i for i in everything if i != self._active]

    def request(self, method: str, path: str, *, retry: bool = True, session: bool = True,
                **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", None) or {})
        if session and self.token:
            headers[rp.SESSION_HEADER] = self.token
        order = self._order() if retry else ([self._active] if self._active is not None else [0])
        last: Exception | None = None
        for i in order:
            base = self._addresses[i][1]
            try:
                response = self._http.request(method, base + path, headers=headers, **kw)
            except _CONNECT_ERRORS as ex:
                last = ex
                continue
            except httpx.TimeoutException as ex:
                self._active = i
                raise Unreachable(f"The server at {base} stopped answering") from ex
            except httpx.TransportError as ex:
                last = ex
                continue
            self._active = i
            return self.check(response)
        names = " or ".join(b for _, b in (self._addresses[i] for i in order))
        raise Unreachable(f"Cannot reach the server at {names}") from last

    def stream(self, method: str, path: str, **kw):
        """A streaming request on the address in use (a download)."""
        headers = dict(kw.pop("headers", None) or {})
        if self.token:
            headers[rp.SESSION_HEADER] = self.token
        # Pick the address first with a cheap call, so the stream itself is not
        # the request that discovers the primary is gone.
        if self._active is None:
            self.request("GET", "/v1/hello", session=False)
        base = self._addresses[self._active][1]
        return self._http.stream(method, base + path, headers=headers, **kw)

    @staticmethod
    def check(response: httpx.Response) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        detail = _detail(response)
        if status == 401:
            raise AuthFailed("The server refused the username or password")
        if status == 423:
            raise SessionLost((detail or {}).get("holder") if isinstance(detail, dict) else None)
        if status == 409 and isinstance(detail, dict) and detail.get("code") == "in_use":
            raise InUse(detail)
        text = detail if isinstance(detail, str) else (detail or {}).get("message", str(detail))
        cls = {404: NotFound, 400: InvalidCommand, 422: Unsupported, 503: Unavailable}.get(status)
        if cls is not None:
            raise cls(text)
        raise AdapterError(f"The server answered {status}: {text}")

    # ---- handshake + session ----------------------------------------------------------
    def hello(self, client_id: str | None = None) -> dict:
        info = self.request("GET", "/v1/hello", params={"client_id": client_id} if client_id else None,
                            session=False).json()
        server_version = tuple(info.get("api_version") or (0, 0))
        update = rp.compatible(rp.API_VERSION, (int(server_version[0]), int(server_version[1])))
        if update:
            raise VersionMismatch(update, info.get("app_version") or ".".join(map(str, server_version)))
        return info

    def acquire(self, client_id: str, machine: str, *, takeover: bool = False) -> dict:
        body = self.request("POST", "/v1/session/acquire", session=False,
                            json={"client_id": client_id, "machine": machine, "takeover": takeover}).json()
        self.token = body["token"]
        return body

    def heartbeat(self, batch: str | None) -> dict:
        return self.request("POST", "/v1/session/heartbeat", json={"batch": batch}).json()

    def release(self, *, timeout: float | None = None) -> None:
        if not self.token:
            return
        try:
            kw = {"timeout": timeout} if timeout is not None else {}
            self.request("POST", "/v1/session/release", retry=False, **kw)
        except AdapterError:
            pass
        self.token = None

    # ---- the protocol -------------------------------------------------------------------
    def rpc(self, method: str, /, *, timeout=..., **args) -> dict:
        """Call one protocol method; the envelope `{result, rev, changed, removed, reset}`
        with `result` already deserialised."""
        kw = {} if timeout is ... else {"timeout": timeout}
        body = self.request("POST", f"/v1/rpc/{method}", json={"args": rp.dump_args(method, args)}, **kw).json()
        body["result"] = rp.load_result(method, body.get("result"))
        return body

    # ---- bytes -----------------------------------------------------------------------------
    def download(self, track_id: str, target: Path, *, progress=None) -> dict:
        """Stream a track's audio into `target`; its server-side size and mtime."""
        with self.stream("GET", "/v1/audio", params={"track_id": track_id}, timeout=None) as response:
            if response.status_code >= 400:
                response.read()
                self.check(response)
            with open(target, "wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
                    if progress is not None:
                        progress(len(chunk))
            return {
                "size": int(response.headers.get("X-Konduktor-Size") or 0),
                "mtime_ns": int(response.headers.get("X-Konduktor-Mtime-Ns") or 0),
            }

    def upload(self, source: Path, folder: str, name: str, *, progress=None) -> dict:
        def body():
            with open(source, "rb") as f:
                while chunk := f.read(1 << 20):
                    if progress is not None:
                        progress(len(chunk))
                    yield chunk

        return self.request("POST", "/v1/upload", params={"dir": folder, "name": name},
                            content=body(), retry=False, timeout=None).json()

    def delete_upload(self, path: str) -> None:
        self.request("DELETE", "/v1/upload", params={"path": path})

    def art(self, track_id: str) -> tuple[bytes, str] | None:
        try:
            r = self.request("GET", "/v1/art", params={"track_id": track_id})
        except NotFound:
            return None
        return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]

    def put_art(self, track_id: str, data: bytes, mime: str) -> dict:
        return self.request("PUT", "/v1/art", params={"track_id": track_id, "mime": mime},
                            content=data).json()

    def fs_list(self, path: str | None) -> dict:
        return self.request("GET", "/v1/fs/list", params={"path": path} if path else None).json()

    def fs_space(self, path: str | None) -> int:
        return int(self.request("GET", "/v1/fs/space", params={"path": path} if path else None).json()["free"])

    # ---- Convert to Stems ----------------------------------------------------------------
    def stems_plan(self, body: dict) -> dict:
        return self.request("POST", "/v1/stems/plan", json=body, timeout=None).json()

    def stems_stage(self, track_id: str, target: str, source: Path, *, sha256: str,
                    bit_rate: int, duration: float, progress=None) -> dict:
        def body():
            with open(source, "rb") as f:
                while chunk := f.read(1 << 20):
                    if progress is not None:
                        progress(len(chunk))
                    yield chunk

        return self.request(
            "PUT", "/v1/stems/stage",
            params={"track_id": track_id, "target": target, "sha256": sha256,
                    "bit_rate": bit_rate, "duration": duration},
            content=body(), retry=False, timeout=None,
        ).json()

    def stems_abandon(self, targets: list[str]) -> None:
        self.request("POST", "/v1/stems/abandon", json={"targets": targets})

    def stems_publish(self, items: list[dict], options: dict) -> dict:
        return self.request("POST", "/v1/stems/publish", json={"items": items, "options": options},
                            timeout=None).json()

    def stems_pending(self) -> dict:
        return self.request("GET", "/v1/stems/pending").json()

    # ---- save / history / export sets -------------------------------------------------
    def save(self) -> dict:
        return self.request("POST", "/v1/save", timeout=None).json()

    def discard(self) -> dict:
        return self.request("POST", "/v1/discard", timeout=None).json()

    def history(self) -> list[dict]:
        return self.request("GET", "/v1/history").json()

    def restore(self, commit_id: str) -> dict:
        return self.request("POST", f"/v1/history/{commit_id}/restore", timeout=None).json()

    def clear_history(self) -> None:
        self.request("DELETE", "/v1/history")

    def export_sets(self) -> dict:
        return self.request("GET", "/v1/export-sets").json()

    def put_export_sets(self, doc: dict) -> None:
        self.request("PUT", "/v1/export-sets", json=doc)
