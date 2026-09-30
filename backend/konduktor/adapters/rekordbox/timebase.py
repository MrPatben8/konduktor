"""Pioneer's clock vs the decoded audio's — one rule, used at every boundary.

**On MP3 and AAC, rekordbox's time 0 is NOT the decoded audio's time 0.** It does
not trim the codec delay a decoder removes, so every position it stores — beats,
cues, loops — sits a constant 1105 samples (~25 ms at 44.1 kHz) LATER than the
same moment in the decoded audio. Lossless files have no such delay.

Measured, not assumed: Konduktor's grid detector (which works in the decoded
time base) against rekordbox's own grids for the local library gave a median of
+25.0 ms on 16 MP3s, +24.5 ms on 4 M4As and 0.0 ms on 7 WAVs. 1105 samples is the
classic MP3 codec delay (576 encoder + 529 decoder).

The generic model is in the DECODED time base — it is what Konduktor's deck plays
and what the grid detector measures. (Traktor's clock differs from it too, by a
different rule: `adapters/traktor/timebase.py`.) So a
Pioneer adapter ADDS the offset on every write and SUBTRACTS it on every read.
Get it wrong and nothing fails: every cue and beat is simply 25 ms early in
rekordbox (as the first OneLibrary exports were), or late in Konduktor.

Suspected, not yet measured: on an MP3 WITHOUT a Xing/Info header the decoder
trims nothing either, so the offset there should be 0, not 1105 — the three
header-less MP3s imported from a stick are exactly the ones whose grids read
~25 ms off (`core/mp3_gapless.py` can tell them apart).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: The codec delay rekordbox does not trim.
CODEC_DELAY_SAMPLES = 1105
#: Formats that carry one. `.stem.m4a` (a Traktor STEM) is AAC.
LOSSY = frozenset({".mp3", ".m4a", ".mp4", ".aac"})
_DEFAULT_RATE = 44100


@lru_cache(maxsize=4096)
def _sample_rate(path: str) -> int:
    try:
        import mutagen

        rate = getattr(getattr(mutagen.File(path), "info", None), "sample_rate", None)
        return int(rate) if rate else _DEFAULT_RATE
    except Exception:  # an unreadable header: assume the overwhelmingly common rate
        return _DEFAULT_RATE


def offset(path: Path | str | None) -> float:
    """Seconds to ADD to a decoded-audio time to get rekordbox's; 0 if lossless.

    A missing path (a track whose audio cannot be located) gets 0: there is no
    file to say which format it is, and guessing lossy would move a lossless
    track's cues.
    """
    if not path:
        return 0.0
    p = Path(path)
    if p.suffix.lower() not in LOSSY:
        return 0.0
    return CODEC_DELAY_SAMPLES / _sample_rate(str(p))


def to_pioneer(seconds: float, off: float) -> float:
    return seconds + off


def from_pioneer(seconds: float, off: float) -> float:
    """Back to the decoded time base, never before the start of the track: a
    rekordbox position inside the codec delay is the track's first sample."""
    return max(0.0, seconds - off)
