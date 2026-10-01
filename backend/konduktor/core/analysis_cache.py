"""Waveform analysis kept on the export's own drive, so a re-export decodes only
what changed.

Decoding is the slow part of a Pioneer export (~1.5 s a track, where copying a
track to a stick is well under a second), so skipping unchanged copies alone
would leave most of a re-export's time in place. What is cached is the
MEASUREMENT (`waveform.Analysis`), not any platform's tags: every Pioneer target
turns the same measurement into its own files, and the Rekordbox Library target
names its analysis folders after a fresh UUID each time, so reusing old ANLZ
files by path could never have worked there.

## Where, and keyed by what

It lives on the destination (`.konduktor-cache/`), beside the manifest, for the
manifest's reason: it describes that drive, and should travel, be cloned or be
wiped with it. An entry is keyed by the SOURCE file's path, size and mtime —
the same facts the runner uses to decide a copy is unchanged — so an edited
track is re-measured exactly when it is re-copied, and the same stick plugged
into another computer (other source paths) simply misses and re-decodes.

`waveform.ANALYSIS_VERSION` is part of every key: change what `analyse()`
measures without bumping it and every re-export serves the old measurement.

Best-effort in both directions: an unreadable entry is a miss, and a failed
store is logged, never fatal — the cache can cost time, never an export.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import numpy as np

from . import waveform

log = logging.getLogger(__name__)

#: The folder, relative to the export's destination.
CACHE_DIR = ".konduktor-cache"


def fingerprint(source: Path | str, size: int, mtime_ns: int) -> str:
    """The identity of one version of one source file."""
    text = f"{waveform.ANALYSIS_VERSION}|{source}|{size}|{mtime_ns}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:24]


class AnalysisCache:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _path(self, key: str, lead: float, n_columns: int) -> Path:
        # One source can be measured with different leads (a target's clock
        # offset), so each variant is its own entry under the same key prefix.
        return self.folder / f"{key}-{int(round(lead * 1_000_000))}-{n_columns}.npz"

    def analyse(self, audio: Path, key: str, *, lead: float = 0.0,
                n_columns: int = 400) -> waveform.Analysis | None:
        """`waveform.analyse(audio, …)`, served from the cache when it can be."""
        path = self._path(key, lead, n_columns)
        hit, value = self._load(path, n_columns)
        if hit:
            return value
        value = waveform.analyse(audio, n_columns=n_columns, lead=lead)
        self._store(path, value)
        return value

    def prune(self, keep: set[str]) -> None:
        """Drop every entry whose key is not in `keep`."""
        try:
            for entry in self.folder.iterdir():
                if entry.name.split("-", 1)[0] not in keep:
                    entry.unlink()
            self.folder.rmdir()   # only succeeds when nothing is left
        except OSError:
            pass

    def clear(self) -> None:
        self.prune(set())

    # ---- storage -----------------------------------------------------------

    @staticmethod
    def _load(path: Path, n_columns: int) -> tuple[bool, waveform.Analysis | None]:
        try:
            with np.load(path, allow_pickle=False) as data:
                if bool(data["undecodable"]):
                    # Remembered too: a file nothing can decode would otherwise
                    # be retried, slowly, on every export.
                    return True, None
                rms = data["rms"]
                if len(rms) != n_columns:
                    return False, None
                return True, waveform.Analysis(
                    columns=waveform.Columns(
                        rms=rms, brightness=data["brightness"],
                        duration=float(data["duration"]),
                    ),
                    frames=waveform.Frames(bands=data["bands"], duration=float(data["duration"])),
                )
        except FileNotFoundError:
            return False, None
        except Exception as ex:  # noqa: BLE001 — a damaged entry is a miss
            log.debug("ignoring unreadable analysis cache entry %s: %s", path, ex)
            return False, None

    @staticmethod
    def _store(path: Path, value: waveform.Analysis | None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".partial")
            with partial.open("wb") as out:
                if value is None:
                    np.savez_compressed(out, undecodable=np.array(True))
                else:
                    # Full precision: a cached export must write the same bytes
                    # a fresh one would.
                    np.savez_compressed(
                        out, undecodable=np.array(False),
                        rms=value.columns.rms, brightness=value.columns.brightness,
                        bands=value.frames.bands, duration=np.array(value.columns.duration),
                    )
            os.replace(partial, path)
        except OSError as ex:
            log.warning("could not cache the analysis of %s: %s", path.name, ex)
