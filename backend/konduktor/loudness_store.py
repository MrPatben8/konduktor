"""Where loudness measurements live: Konduktor's own store, keyed by file PATH.

Not the library and not the file's tags (decided 2026-10-19,
`.claude/discussions/discuss-loudness-analysis-2026-10-19.md`): a measurement
can always be made again from the audio, so it is cache-shaped, and nothing but
Konduktor would read a tag. Keyed by the audio file's path — on a remote
library the SERVER's path, in the server's own store — with the file's size and
mtime recorded beside the values. Those facts are NOT part of the key: a file
whose facts changed keeps its values, shown STALE until re-analysed, because a
changed mtime almost always means another app rewrote the tags (keying on the
facts, as `analysis_cache` / `stream_cache` do, would orphan the value on every
tag edit). Konduktor's own tag writes `refresh` the facts, so they never stale
a value; a path remap `remap`s the keys.

Writing is immediate and outside the edit/Save cycle: a measurement is not an
edit, so it never makes the library dirty and Discard does not remove it.

SQLite (stdlib, so the lightweight server image has it too) rather than JSON:
a batch writes one row per track over a store that holds the whole library.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .core import loudness
from .core.adapter import AudioFacts
from .core.loudness import Loudness
from .core.pathmap import PathMapping

_FILE = "loudness.sqlite3"
_CHUNK = 500                                    # paths per IN (...) query


@dataclass(frozen=True)
class Entry:
    values: Loudness
    size: int
    mtime_ns: int
    version: int

    def stale(self, facts: AudioFacts | None) -> bool:
        """The file changed since it was measured, or the measurement has."""
        if self.version < loudness.VERSION:
            return True
        return facts is not None and (facts.size != self.size or facts.mtime_ns != self.mtime_ns)

    def wire(self, path: str) -> dict:
        return {"path": path, "size": self.size, "mtime_ns": self.mtime_ns,
                "version": self.version, **self.values.as_dict()}

    @classmethod
    def from_wire(cls, d: dict) -> "Entry":
        return cls(values=Loudness.from_dict(d), size=int(d["size"]),
                   mtime_ns=int(d["mtime_ns"]), version=int(d["version"]))


class LoudnessStore(Protocol):
    #: False only for a remote server too old to hold measurements.
    available: bool

    def get(self, paths: list[str]) -> dict[str, Entry]: ...

    def put(self, facts: AudioFacts, values: Loudness) -> None: ...

    def refresh(self, paths: list[str]) -> None: ...

    def remap(self, mapping: PathMapping) -> int: ...


def _norm(path: str) -> str:
    return os.path.normpath(path)


class LocalLoudnessStore:
    """The store on this machine (on a server: the server's)."""

    available = True

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS loudness ("
                " path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
                " version INTEGER NOT NULL, lufs REAL NOT NULL, rms REAL NOT NULL,"
                " true_peak REAL NOT NULL, lra REAL NOT NULL, plr REAL NOT NULL)")

    def get(self, paths: list[str]) -> dict[str, Entry]:
        """Entries for those of `paths` that were measured, keyed as ASKED."""
        asked = {_norm(p): p for p in paths}
        keys = list(asked)
        out: dict[str, Entry] = {}
        with self._lock:
            for i in range(0, len(keys), _CHUNK):
                chunk = keys[i:i + _CHUNK]
                rows = self._db.execute(
                    "SELECT path, size, mtime_ns, version, lufs, rms, true_peak, lra, plr"
                    f" FROM loudness WHERE path IN ({','.join('?' * len(chunk))})", chunk)
                for path, size, mtime_ns, version, *values in rows:
                    out[asked[path]] = Entry(Loudness(*values), size, mtime_ns, version)
        return out

    def put(self, facts: AudioFacts, values: Loudness) -> None:
        self.put_many([(facts, values)])

    def put_many(self, items: list[tuple[AudioFacts, Loudness]]) -> None:
        rows = [(_norm(f.key), f.size, f.mtime_ns, loudness.VERSION, v.lufs, v.rms, v.true_peak, v.lra, v.plr)
                for f, v in items]
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO loudness VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def put_entries(self, entries: list[dict]) -> None:
        """Wire entries (a remote client's measurements), versions as sent."""
        rows = []
        for d in entries:
            e = Entry.from_wire(d)
            v = e.values
            rows.append((_norm(str(d["path"])), e.size, e.mtime_ns, e.version,
                         v.lufs, v.rms, v.true_peak, v.lra, v.plr))
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO loudness VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def refresh(self, paths: list[str]) -> None:
        """Konduktor itself just rewrote these files (tags at Save): take their
        current facts, so its own writes never stale a value. Only rows that
        exist are touched."""
        rows = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            rows.append((st.st_size, st.st_mtime_ns, _norm(p)))
        if rows:
            with self._lock, self._db:
                self._db.executemany("UPDATE loudness SET size = ?, mtime_ns = ? WHERE path = ?", rows)

    def remap(self, mapping: PathMapping) -> int:
        """A path remap moved the library's files from `from` to `to`: carry
        every value measured under `from` across. A value already measured at
        the new path wins. Returns how many moved."""
        if mapping.empty:
            return 0
        moved = 0
        with self._lock, self._db:
            paths = [r[0] for r in self._db.execute("SELECT path FROM loudness")]
            for old in paths:
                if not mapping.matches(Path(old)):
                    continue
                new = _norm(str(mapping.apply(Path(old))))
                if new == old:
                    continue
                if self._db.execute("SELECT 1 FROM loudness WHERE path = ?", (new,)).fetchone():
                    self._db.execute("DELETE FROM loudness WHERE path = ?", (old,))
                else:
                    self._db.execute("UPDATE loudness SET path = ? WHERE path = ?", (new, old))
                    moved += 1
        return moved

    def close(self) -> None:
        with self._lock:
            self._db.close()


_LOCAL: dict[str, LocalLoudnessStore] = {}
_LOCAL_LOCK = threading.Lock()


def local() -> LocalLoudnessStore:
    """This machine's store — one per data directory, so a test (or the server)
    pointing `KONDUKTOR_DATA_DIR` elsewhere gets its own."""
    from . import paths

    db = paths.app_data_dir() / _FILE
    with _LOCAL_LOCK:
        store = _LOCAL.get(str(db))
        if store is None:
            store = _LOCAL[str(db)] = LocalLoudnessStore(db)
        return store


def file_facts(path: Path | str) -> AudioFacts:
    st = os.stat(path)
    return AudioFacts(key=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns)
