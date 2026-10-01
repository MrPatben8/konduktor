"""A resumable, verified download — the engine's parts and the weights.

Big files over home connections get interrupted, so a download resumes from
its `.part` file with an HTTP `Range` request (and starts over if the server
ignores it), and a file only takes its final name once its SHA-256 matches.
GitHub release assets and Hugging Face files both redirect to a CDN; urllib
carries the `Range` header across the redirect.

TLS uses certifi's bundle: a frozen Python on macOS has no system CA store of
its own, so the default context would reject every HTTPS certificate.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Callable

CHUNK = 1 << 20
_UA = "Konduktor (stem engine download)"


class DownloadError(Exception):
    """User-facing."""


class DownloadCancelled(Exception):
    pass


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — fall back to the platform's own store
        return ssl.create_default_context()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, *, sha256: str, size: int | None = None,
          on_bytes: Callable[[int], None] = lambda n: None,
          cancelled: Callable[[], bool] = lambda: False,
          context: ssl.SSLContext | None = None, timeout: float = 60) -> None:
    """Download `url` to `dest`, resuming a previous `.part`, verified.

    `on_bytes` is told about every chunk INCLUDING what an earlier attempt
    already fetched (once, up front), so a progress bar resumes where it was.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (size is None or dest.stat().st_size == size) and sha256_file(dest) == sha256:
        on_bytes(dest.stat().st_size)
        return
    partial = dest.with_name(dest.name + ".part")
    have = partial.stat().st_size if partial.exists() else 0
    if size is not None and have > size:
        partial.unlink()
        have = 0
    if have:
        on_bytes(have)
    if size is None or have < size:
        headers = {"User-Agent": _UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
                if have and r.status != 206:
                    # The server ignored the range: start over, and un-count it.
                    on_bytes(-have)
                    have = 0
                with open(partial, "ab" if have else "wb") as f:
                    while True:
                        if cancelled():
                            raise DownloadCancelled()
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        on_bytes(len(chunk))
        except DownloadCancelled:
            raise
        except OSError as ex:  # URLError, timeouts, connection resets
            raise DownloadError(f"Could not download {dest.name}: {ex}") from ex
    got = sha256_file(partial)
    if got != sha256:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"{dest.name} did not arrive intact (checksum mismatch); try again")
    os.replace(partial, dest)
