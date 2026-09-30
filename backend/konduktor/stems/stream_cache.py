"""The deck's stem audio: one stream of a stem file, as a file of its own.

A browser decodes only the FIRST audio stream of an MP4, so the deck cannot get
at a stem file's stems from the file itself. Each is copied out (packets, not
re-encoded — `stem_file.extract_stream`) into a small cache in app-data, keyed
by the source's path, size and mtime, so an edited or replaced file is never
served stale and loading the same track twice costs nothing the second time.

The cache is bounded (`LIMIT` bytes, least recently used first) — a stem is
2-10 MB, so the limit holds a few hundred tracks' worth. Files are written as a
partial then renamed, so two requests racing for the same stem, or a crash
mid-write, can never serve a truncated file.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from .. import paths
from ..core import stem_file

LIMIT = 2 * 1024 ** 3
_lock = threading.Lock()


def root() -> Path:
    d = paths.app_data_dir() / "stems" / "playback"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(path: Path, stream: int) -> str:
    st = path.stat()
    raw = f"{path.resolve()}\0{st.st_size}\0{st.st_mtime_ns}\0{stream}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def stem_file_for(path: Path, stem: int) -> Path:
    """The cached single-stream file for stem `stem` (0-based: stream stem+1)."""
    path = Path(path)
    stream = stem + 1
    target = root() / f"{_key(path, stream)}.m4a"
    if target.exists():
        os.utime(target)  # most recently used
        return target
    partial = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        with open(partial, "wb") as fp:
            stem_file.extract_stream(path, stream, fp)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    _prune(keep=target)
    return target


def _prune(keep: Path) -> None:
    with _lock:
        files = []
        for p in root().glob("*.m4a"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files):
            if total <= LIMIT:
                break
            if p == keep:
                continue
            p.unlink(missing_ok=True)
            total -= size
