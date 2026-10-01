"""`serve`: one engine process per batch, the model loaded once.

Requests are JSON lines on stdin, replies JSON lines on the protocol stream
(a private copy of stdout — see `__main__._protocol`):

    -> {"id": "t1", "cmd": "separate", "input": "mix.f32", "samples": N,
        "output_dir": "out/", "seed": 0}
    <- {"id": "t1", "event": "progress", "fraction": 0.42}
    <- {"id": "t1", "event": "done", "seconds": 61.2, "device": "mps"}
       (or "error" with "message", or "cancelled")
    -> {"cmd": "cancel", "id": "t1"}   the running separation stops at its next
                                       progress step; the model stays loaded
    -> {"cmd": "ping"}                 <- {"event": "pong"}

**stdin closing means the parent is gone** — the backend exits without killing
its children — so a reader THREAD watches it and ends the process at once, even
mid-separation (the main thread is busy inside torch and could not notice).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path


class Cancelled(Exception):
    pass


def _lower_priority() -> None:
    """A batch may run for hours: stay out of the way of the DJ app itself."""
    try:
        if sys.platform == "win32":
            import ctypes

            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001 — a hint, never a failure
        pass


def run(out, emit, weights: str, device_request: str) -> int:
    import numpy as np
    import torch

    from . import core

    _lower_priority()
    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))

    requests: "queue.Queue[dict]" = queue.Queue()
    cancel: set[str] = set()
    lock = threading.Lock()

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                emit(event="error", message=f"not JSON: {line[:80]}")
                continue
            if msg.get("cmd") == "cancel":
                with lock:
                    cancel.add(str(msg.get("id")))
            else:
                requests.put(msg)
        os._exit(0)  # the parent is gone: do not linger, whatever is running

    threading.Thread(target=reader, name="stdin", daemon=True).start()

    device = core.pick_device(device_request)
    t0 = time.time()
    model = core.load_model(weights)
    emit(event="ready", device=device, seconds=round(time.time() - t0, 2))

    while True:
        msg = requests.get()
        cmd = msg.get("cmd")
        if cmd == "ping":
            emit(event="pong")
            continue
        if cmd == "shutdown":
            return 0
        if cmd != "separate":
            emit(event="error", message=f"unknown command {cmd!r}")
            continue
        rid = str(msg.get("id"))
        started = time.time()
        try:
            n = int(msg["samples"])
            pcm = np.fromfile(msg["input"], dtype=np.float32)
            if pcm.size != 2 * n:
                raise ValueError(f"expected {2 * n} floats, found {pcm.size}")
            pcm = pcm.reshape(2, n)

            def progress(f: float) -> None:
                with lock:
                    if rid in cancel:
                        raise Cancelled()
                emit(id=rid, event="progress", fraction=round(f, 4))

            used = device
            try:
                stems = core.separate(model, pcm, device, seed=int(msg.get("seed", 0)), on_progress=progress)
            except torch.OutOfMemoryError:
                # A long track on a small GPU: finish it on the CPU rather than fail.
                if device == "cpu":
                    raise
                torch.cuda.empty_cache() if device == "cuda" else None
                used = "cpu"
                emit(id=rid, event="fallback", device="cpu")
                stems = core.separate(model, pcm, "cpu", seed=int(msg.get("seed", 0)), on_progress=progress)
            folder = Path(msg["output_dir"])
            folder.mkdir(parents=True, exist_ok=True)
            for name, x in stems.items():
                x.tofile(folder / f"{name}.f32")
            emit(id=rid, event="done", seconds=round(time.time() - started, 2), device=used)
        except Cancelled:
            emit(id=rid, event="cancelled")
        except BaseException as ex:  # noqa: BLE001 — reported, the process lives on
            # demucs re-raises a callback's exception wrapped; unwrap a cancel.
            if isinstance(ex.__cause__, Cancelled) or isinstance(ex.__context__, Cancelled):
                emit(id=rid, event="cancelled")
            else:
                emit(id=rid, event="error", message=f"{type(ex).__name__}: {ex}")
        finally:
            with lock:
                cancel.discard(rid)
