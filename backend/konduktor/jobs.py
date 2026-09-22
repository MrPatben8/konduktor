"""Long-running operations, with progress and a working cancel.

Every route in this app is a synchronous `def` that finishes in milliseconds,
which has been fine while every operation was "edit an object in memory". Import
is not: it copies audio off a USB stick, and a DJ's stick is tens of gigabytes.
A synchronous copy would hold an HTTP request open for minutes and give the user
no way to stop it or see how far it had got.

So this is the smallest thing that fixes that, and deliberately no more:

  * a **thread per job** — the work is IO-bound (file copying), so threads are
    the right shape and asyncio would mean rewriting the routes;
  * **polling**, not SSE or websockets — a progress bar wants a number a few
    times a second, and adding a streaming transport to an app that has none is
    a lot of new failure modes for a smoother number;
  * **cooperative cancel** — the job checks a flag between units of work. There
    is no way to kill a thread in Python, and there should not be: a copy
    interrupted mid-file needs to clean up after itself, which it can only do if
    it is the one deciding to stop.

Jobs are kept in memory and die with the process. That is correct rather than
lazy: a job describes work happening right now, and a "running" job whose thread
no longer exists would be a lie that survived a restart.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

log = logging.getLogger(__name__)

JobState = Literal["running", "done", "failed", "cancelled"]

# Finished jobs are kept so a client that polls a moment late still sees the
# result. Bounded, so a long session cannot grow this without limit.
MAX_FINISHED = 20


@dataclass
class Job:
    id: str
    kind: str
    state: JobState = "running"
    # `total` is 0 until the job knows its size — a copy has to scan first — and
    # the UI is expected to show an indeterminate bar until it is positive.
    total: int = 0
    done: int = 0
    message: str = ""
    result: Any = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobHandle:
    """What the running work is given: report progress, ask whether to stop.

    Deliberately the only thing a job function touches, so the work cannot reach
    into the registry and, say, mark itself done — that is the registry's call,
    and it is what makes "the state always reflects the thread" true.
    """

    def __init__(self, job: Job) -> None:
        self._job = job

    def progress(self, done: int | None = None, total: int | None = None,
                 message: str | None = None) -> None:
        if done is not None:
            self._job.done = done
        if total is not None:
            self._job.total = total
        if message is not None:
            self._job.message = message

    @property
    def cancelled(self) -> bool:
        return self._job._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled()


class JobCancelled(Exception):
    """Raised inside a job when the user asked it to stop.

    An exception rather than a return value so a job can unwind through whatever
    it was doing — and so its cleanup can be an ordinary `finally`.
    """


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[JobHandle], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()

        def run() -> None:
            handle = JobHandle(job)
            try:
                job.result = fn(handle)
                job.state = "cancelled" if handle.cancelled else "done"
            except JobCancelled:
                job.state = "cancelled"
            except Exception as ex:  # noqa: BLE001 — surfaced to the client
                log.exception("job %s (%s) failed", job.id, kind)
                job.state = "failed"
                job.error = str(ex) or ex.__class__.__name__
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, name=f"job-{kind}-{job.id[:8]}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Returns False for an unknown or finished job.

        Only a REQUEST: the job stops at its next checkpoint and cleans up after
        itself, so the state does not become "cancelled" here.
        """
        job = self.get(job_id)
        if job is None or job.finished:
            return False
        job._cancel.set()
        return True

    def active(self, kind: str | None = None) -> list[Job]:
        with self._lock:
            return [
                j for j in self._jobs.values()
                if not j.finished and (kind is None or j.kind == kind)
            ]

    def _prune(self) -> None:
        """Drop the oldest finished jobs. Caller holds the lock."""
        finished = sorted(
            (j for j in self._jobs.values() if j.finished),
            key=lambda j: j.finished_at or 0.0,
        )
        for job in finished[: max(0, len(finished) - MAX_FINISHED)]:
            self._jobs.pop(job.id, None)


JOBS = JobRegistry()
