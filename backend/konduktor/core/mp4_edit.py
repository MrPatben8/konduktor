"""The priming an MP4/M4A's edit list tells a decoder to skip.

An AAC encoder emits `priming` samples of lead-in before the audio proper —
1024 from FFmpeg's `aac`, 2112 from Apple's — and records it as the
`media_time` of the audio track's edit list (`moov/trak/edts/elst`). A decoder
that honours the edit list (FFmpeg, CoreAudio, Traktor) starts the audio after
it; one that ignores it (rekordbox) plays the priming as audio. What a platform
does with it lives in that platform's `timebase`; this only reads the box.

Only box headers are read and `mdat` is seeked past, so `moov` at the END of a
file (FFmpeg's default, and every commercial stem file) costs a few reads.
"""
from __future__ import annotations

import os
import struct
from functools import lru_cache
from pathlib import Path

_CONTAINERS = {b"moov", b"trak", b"mdia", b"edts"}


def _boxes(f, start: int, end: int):
    """(type, payload offset, payload end) for each box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        body = pos + 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8:
            return
        yield kind, body, min(pos + size, end)
        pos += size


def _audio_track_priming(f, trak_start: int, trak_end: int) -> tuple[int, int] | None:
    """(media_time, media timescale) of one trak if it is audio, else None."""
    handler = timescale = None
    media_time = 0  # no edit list: nothing to skip
    for kind, body, end in _boxes(f, trak_start, trak_end):
        if kind == b"mdia":
            for k2, b2, e2 in _boxes(f, body, end):
                f.seek(b2)
                if k2 == b"hdlr":
                    handler = f.read(12)[8:12]
                elif k2 == b"mdhd":
                    version = f.read(1)[0]
                    f.seek(b2 + (20 if version == 1 else 12))
                    timescale = struct.unpack(">I", f.read(4))[0]
        elif kind == b"edts":
            for k2, b2, e2 in _boxes(f, body, end):
                if k2 != b"elst":
                    continue
                f.seek(b2)
                version = f.read(1)[0]
                f.read(3)
                count = struct.unpack(">I", f.read(4))[0]
                for _ in range(count):
                    if version == 1:
                        _dur, mt = struct.unpack(">Qq", f.read(16))
                    else:
                        _dur, mt = struct.unpack(">Ii", f.read(8))
                    f.read(4)  # media rate
                    if mt >= 0:  # -1 is an EMPTY edit (a delay), not a skip
                        media_time = mt
                        break
    if handler != b"soun" or not timescale:
        return None
    return media_time, timescale


@lru_cache(maxsize=16384)
def _read_cached(path: str, size: int, mtime_ns: int) -> float | None:
    with open(path, "rb") as f:
        for kind, body, end in _boxes(f, 0, size):
            if kind != b"moov":
                continue
            for k2, b2, e2 in _boxes(f, body, end):
                if k2 == b"trak":
                    found = _audio_track_priming(f, b2, e2)
                    if found is not None:  # the FIRST audio track: a stem's master
                        media_time, timescale = found
                        return media_time / timescale
            return None
    return None


def priming_seconds(path: Path | str | None) -> float | None:
    """Seconds of priming the first audio track's edit list skips (0.0 when it
    has no edit list); None when the file is missing or not a readable MP4.
    Cached per (path, size, mtime), like `mp3_gapless.read`."""
    if not path:
        return None
    try:
        st = os.stat(path)
        return _read_cached(str(path), st.st_size, st.st_mtime_ns)
    except (OSError, struct.error, IndexError):
        return None
