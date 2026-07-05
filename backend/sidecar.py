"""PyInstaller entry point for the packaged backend sidecar.

The frozen build launches this. It picks a free localhost port, announces it on
stdout as `KONDUKTOR_PORT=<n>` (so the host process — Tauri — knows where to
send API calls), then serves the FastAPI app on loopback.

Dev is unchanged: `dev.sh` still runs `uvicorn konduktor.main:app` on :8000.
This file exists only for the frozen sidecar.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import uvicorn

from konduktor.main import app


def _watch_parent_and_exit() -> None:
    """Exit when our parent (the Tauri shell) goes away.

    Tauri spawns us with a piped stdin and holds the write end open without
    writing anything. If the host process quits — gracefully, via SIGKILL, or by
    crashing — the OS closes that pipe and our stdin reads EOF. That's our cue to
    shut down, so the backend can never be orphaned (the Rust-side kill-on-exit
    only covers the graceful path). When run standalone from a terminal, stdin is
    a TTY and this blocks harmlessly forever.
    """
    try:
        while sys.stdin.buffer.read(1):
            pass
    except Exception:
        pass
    os._exit(0)


def _free_port() -> int:
    """Grab an unused loopback port. There's a tiny bind→close→rebind race, but
    it's harmless on 127.0.0.1 for a single local sidecar."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    # Die with our host so the backend is never left orphaned.
    threading.Thread(target=_watch_parent_and_exit, daemon=True).start()
    # Honor an explicit port (tests / the host may pin one) else pick a free one.
    port = int(os.environ.get("KONDUKTOR_PORT") or _free_port())
    # Announce the port BEFORE uvicorn starts logging, and flush, so the parent
    # can read one clean line without racing the server's own output.
    print(f"KONDUKTOR_PORT={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
