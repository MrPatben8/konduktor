"""This computer's copies of a remote library's audio.

Everything that needs a track's audio as a FILE runs here, not on the server —
the deck's decode, grid and cue analysis, stem separation and extraction, an
export's copy onto a stick. One cache serves them all, so a track analysed and
then exported downloads once.

  * **Keyed by what the server says the file IS** — its path there, size and
    mtime (`AudioFacts`) — never by when it was fetched: an edited file (Save
    writes tags into it) is a new entry, and the same file fetched again after
    an eviction, or by another computer exporting the same set, is recognised.
  * **A partial, renamed into place**, so a cut connection never leaves a
    truncated file under a real entry's name, and a download whose size does
    not match the facts is refused rather than kept.
  * **Least recently used, under a cap** (`remoteCacheBytes` in prefs). The
    entry just written and anything touched in the last few minutes are never
    the ones evicted: they are being played or copied right now.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

from ... import paths, prefs
from ...core.adapter import AudioFacts, Unavailable

DEFAULT_CAP = 10 * 1024 ** 3
_RECENT = 300.0  # seconds: an entry touched this recently is in use
_PART = ".part"


class StaleFacts(Unavailable):
    """The server's file changed between asking about it and downloading it."""


def cache_root() -> Path:
    return paths.app_data_dir() / "remote-cache"


def cap_bytes() -> int:
    try:
        value = int(prefs.load_prefs().get("remoteCacheBytes") or DEFAULT_CAP)
    except (TypeError, ValueError):
        value = DEFAULT_CAP
    return max(value, 256 * 1024 ** 2)


class RemoteAudioCache:
    def __init__(self, root: Path | None = None, *, cap=None, clock=time.time):
        self.root = Path(root) if root is not None else cache_root()
        self._cap = cap if cap is not None else cap_bytes
        self._clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        #: Recent download speeds (bytes/s), newest last — for time estimates.
        self._speeds: list[float] = []

    # ---- naming ----------------------------------------------------------------
    @staticmethod
    def key(facts: AudioFacts) -> str:
        text = f"{facts.key}|{facts.size}|{facts.mtime_ns}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def path_for(self, facts: AudioFacts) -> Path:
        # The original's suffix, so a MIME type and a decoder's guess still work.
        suffix = Path(facts.key.replace("\\", "/")).suffix.lower()
        return self.root / f"{self.key(facts)}{suffix}"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # ---- use -------------------------------------------------------------------
    def get(self, facts: AudioFacts) -> Path | None:
        path = self.path_for(facts)
        if path.is_file():
            self._touch(path)
            return path
        return None

    def fetch(self, facts: AudioFacts, download) -> Path:
        """The cached copy, downloading it first if needed. `download(partial)`
        writes the file and returns the server's `{size, mtime_ns}` for it."""
        path = self.path_for(facts)
        with self._lock_for(path.name):
            if path.is_file():
                self._touch(path)
                return path
            self.root.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}{_PART}")
            started = time.monotonic()
            try:
                meta = download(partial) or {}
                got = partial.stat().st_size
                if got != facts.size or (meta.get("size") and meta["size"] != facts.size):
                    raise StaleFacts(f"{Path(facts.key).name} changed on the server while downloading")
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)
            elapsed = max(time.monotonic() - started, 1e-3)
            if facts.size > 1 << 20:
                self._speeds = (self._speeds + [facts.size / elapsed])[-8:]
            self._touch(path)
        self.prune(keep={path})
        return path

    def speed(self) -> float | None:
        """A recent download speed in bytes/s, or None before the first."""
        if not self._speeds:
            return None
        ordered = sorted(self._speeds)
        return ordered[len(ordered) // 2]

    def _touch(self, path: Path) -> None:
        now = self._clock()
        try:
            os.utime(path, (now, now))
        except OSError:
            pass

    # ---- housekeeping ------------------------------------------------------------
    def _entries(self) -> list[tuple[float, int, Path]]:
        out = []
        if not self.root.is_dir():
            return out
        for p in self.root.iterdir():
            if p.name.endswith(_PART) or not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
        return out

    def prune(self, keep: set[Path] | None = None) -> int:
        """Evict least-recently-used entries until under the cap. Returns bytes freed."""
        cap = self._cap() if callable(self._cap) else self._cap
        entries = sorted(self._entries())
        total = sum(size for _, size, _ in entries)
        freed = 0
        now = self._clock()
        for used, size, p in entries:
            if total <= cap:
                break
            if (keep and p in keep) or now - used < _RECENT:
                continue
            try:
                p.unlink()
            except OSError:
                continue  # held open (Windows): next time
            total -= size
            freed += size
        return freed

    def usage(self) -> dict:
        entries = self._entries()
        cap = self._cap() if callable(self._cap) else self._cap
        return {"bytes": sum(s for _, s, _ in entries), "files": len(entries), "cap": cap,
                "free": _free(self.root)}

    def clear(self) -> int:
        freed = 0
        for _, size, p in self._entries():
            try:
                p.unlink()
                freed += size
            except OSError:
                pass
        return freed


def _free(folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


_shared: RemoteAudioCache | None = None


def shared() -> RemoteAudioCache:
    global _shared
    if _shared is None:
        _shared = RemoteAudioCache()
    return _shared
