"""Driving the stem engine: one `serve` process per batch.

The engine's protocol is documented in `engine/konduktor_engine/serve.py`.
Audio crosses as raw planar float32 files in a work folder.

Lifetime is the part that must not go wrong — a batch can run for hours:
  * The engine ends itself when its stdin closes, so if this backend dies the
    engine follows. On Windows it is ALSO put in a Job Object that kills it
    when the last handle closes, which covers a hard kill of the backend.
  * Cancel is a message (the model stays loaded); `close()` ends the process.
  * The machine is kept awake for the batch — macOS `caffeinate` tied to the
    engine's pid, Windows `SetThreadExecutionState` on the calling thread —
    or a laptop sleeping at 1 am stops a 4-hour conversion at track 30.
  * stderr is drained on a thread (a full pipe would block the engine) and its
    tail kept, so an engine crash can be reported with its reason.
"""
from __future__ import annotations

import collections
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import numpy as np

from .engine_manager import EngineError

SOURCES = ("drums", "bass", "other", "vocals")


class EngineCancelled(Exception):
    pass


def _job_object_kill_on_close(pid_handle) -> object | None:
    """Windows: a Job Object with KILL_ON_JOB_CLOSE holding the engine. The
    returned handle must stay referenced for as long as the engine may live."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
        k32.AssignProcessToJobObject(job, int(pid_handle))
        return job
    except Exception:  # noqa: BLE001 — the stdin watchdog still covers the common case
        return None


@contextmanager
def keep_awake(pid: int | None = None):
    """Stop the computer sleeping while a batch runs (not the display)."""
    proc = None
    if sys.platform == "darwin" and pid:
        try:
            proc = subprocess.Popen(["caffeinate", "-i", "-w", str(pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            proc = None
    elif sys.platform == "win32":
        try:
            import ctypes

            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:  # noqa: BLE001
                pass


class EngineProcess:
    """One running `serve`. Not thread-safe: one batch drives it."""

    def __init__(self, executable: Path | str, weights: Path | str, *, device: str = "auto",
                 ready_timeout: float = 180) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [str(executable), "serve", "--weights", str(weights), "--device", device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, creationflags=flags,
        )
        self._job = _job_object_kill_on_close(getattr(self.proc, "_handle", 0))
        self._messages: "queue.Queue[dict | None]" = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        threading.Thread(target=self._read_stdout, daemon=True, name="engine-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="engine-stderr").start()
        self.device = device
        msg = self._next(ready_timeout)
        if msg is None or msg.get("event") != "ready":
            self.close()
            raise EngineError(f"The stem engine did not start: {self._reason(msg)}")
        self.device = msg.get("device", device)
        self._n = 0

    # ---- plumbing ---------------------------------------------------------------
    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            try:
                self._messages.put(json.loads(line))
            except ValueError:
                self._stderr.append(line.rstrip())
        self._messages.put(None)  # the engine closed its output: it has exited

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _next(self, timeout: float | None) -> dict | None:
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty:
            return {"event": "timeout"}

    def _reason(self, msg: dict | None) -> str:
        if msg and msg.get("message"):
            return msg["message"]
        tail = [l for l in self._stderr if l.strip()][-3:]
        return " / ".join(tail) or "no reason given"

    def _send(self, **msg) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as ex:
            raise EngineError(f"The stem engine has stopped: {self._reason(None)}") from ex

    # ---- the one command ------------------------------------------------------------
    def separate(self, pcm: np.ndarray, work: Path | str, *, seed: int = 0,
                 on_progress: Callable[[float], None] = lambda f: None,
                 cancelled: Callable[[], bool] = lambda: False) -> list[np.ndarray]:
        """Four stems in `SOURCES` order, each shaped like `pcm` (2, n)."""
        work = Path(work)
        work.mkdir(parents=True, exist_ok=True)
        self._n += 1
        rid = f"t{self._n}"
        mix = work / f"{rid}.f32"
        np.ascontiguousarray(pcm, dtype=np.float32).tofile(mix)
        out = work / rid
        self._send(cmd="separate", id=rid, input=str(mix), samples=int(pcm.shape[1]),
                   output_dir=str(out), seed=seed)
        asked_to_stop = False
        try:
            while True:
                if cancelled() and not asked_to_stop:
                    self._send(cmd="cancel", id=rid)
                    asked_to_stop = True
                msg = self._next(0.5)
                if msg is None:
                    raise EngineError(f"The stem engine stopped: {self._reason(None)}")
                if msg.get("event") == "timeout" or msg.get("id") not in (rid, None):
                    continue
                event = msg.get("event")
                if event == "progress":
                    on_progress(float(msg.get("fraction", 0.0)))
                elif event == "done":
                    stems = [np.fromfile(out / f"{name}.f32", dtype=np.float32).reshape(2, -1)
                             for name in SOURCES]
                    # ~100 MB of raw float per minute of audio: never leave it.
                    shutil.rmtree(out, ignore_errors=True)
                    return stems
                elif event == "cancelled":
                    raise EngineCancelled()
                elif event == "error":
                    raise EngineError(f"Separation failed: {self._reason(msg)}")
        finally:
            mix.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()  # the engine ends itself on EOF
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
