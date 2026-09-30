"""Pioneer's clock vs the decoded audio's — one rule, used at every boundary.

**rekordbox's time 0 is not always the decoded audio's time 0**, and by how much
depends on the FILE, not just its format. rekordbox does not trim the start
padding a gapless decoder removes, so every position it stores — beats, cues,
loops — sits LATER than the same moment in the decoded audio by exactly that
padding:

  * **MP3 with a Xing/Info header**: the header's encoder delay + 529 decoder
    delay — 1105 samples (~25 ms at 44.1 kHz) for LAME's usual 576. rekordbox
    SKIPS the header frame (Traktor does not — see `adapters/traktor/timebase.py`),
    it just does not trim.
  * **MP3 without a header**: 0. No decoder knows the delay, so none trims it.
  * **AAC / M4A** (incl. a Traktor `.stem.m4a`): the edit list's priming, which
    rekordbox ignores — 1024 samples from FFmpeg's encoder, 2112 from Apple's.
  * **Lossless**: 0.

Measured 2026-09-30 in rekordbox 7 with kit 4 (see the stem-conversion discussion
log): blinded hotcues Konduktor wrote on a synthetic kick landed on it only at
these offsets, hot cues placed by hand read back at them to rekordbox's 1 ms
resolution, and rekordbox's own grids agree. It replaced a fixed 1105 samples
for every lossy file, fitted to a library of MP3s that all had headers and
FFmpeg-encoded M4As — right for those, 25 ms wrong on a header-less MP3 and
22 ms wrong on Apple-encoded AAC.

Derived, not measured: an MP3 header whose delay field a decoder does not trust
(no LAME/Lavf/Lavc tag, or VBRI) — nothing is trimmed, so 0.

A file that cannot be READ (offline, or a path that does not resolve) falls back
to the old 1105 samples for a lossy suffix: the overwhelmingly common case, and
what every position written before this rule assumed.

The generic model is in the DECODED time base — what Konduktor's deck plays and
the grid detector measures. So a Pioneer adapter ADDS the offset on every write
and SUBTRACTS it on every read. Get it wrong and nothing fails: every cue and
beat is simply early in rekordbox (as the first OneLibrary exports were), or
late in Konduktor.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ...core import mp3_gapless, mp4_edit

#: The fallback for a lossy file that cannot be read: MP3's classic codec delay.
CODEC_DELAY_SAMPLES = 1105
#: Formats that carry a start padding. `.stem.m4a` (a Traktor STEM) is AAC.
LOSSY = frozenset({".mp3", ".m4a", ".mp4", ".aac"})
_MP4 = frozenset({".m4a", ".mp4"})
_DEFAULT_RATE = 44100


@lru_cache(maxsize=4096)
def _sample_rate(path: str) -> int:
    try:
        import mutagen

        rate = getattr(getattr(mutagen.File(path), "info", None), "sample_rate", None)
        return int(rate) if rate else _DEFAULT_RATE
    except Exception:  # an unreadable header: assume the overwhelmingly common rate
        return _DEFAULT_RATE


def _measured(p: Path) -> float | None:
    """The per-file offset, or None when the file cannot be read."""
    suffix = p.suffix.lower()
    if suffix == ".mp3":
        start = mp3_gapless.read(p)
        if start is None:
            return None
        return start.trimmed / start.sample_rate if start.header_frame else 0.0
    if suffix in _MP4:
        return mp4_edit.priming_seconds(p)
    return None  # raw ADTS .aac: nothing to read, keep the fallback


def offset(path: Path | str | None) -> float:
    """Seconds to ADD to a decoded-audio time to get rekordbox's; 0 if lossless.

    A missing path (a track whose audio cannot be located) gets 0: there is no
    file to say which format it is, and guessing lossy would move a lossless
    track's cues. A path to a lossy file that cannot be read gets the fallback.
    """
    if not path:
        return 0.0
    p = Path(path)
    if p.suffix.lower() not in LOSSY:
        return 0.0
    measured = _measured(p)
    if measured is not None:
        return measured
    return CODEC_DELAY_SAMPLES / _sample_rate(str(p))


def to_pioneer(seconds: float, off: float) -> float:
    """A decoded-audio time -> rekordbox's, never before its own time 0: the
    generic model can hold a position before the decoded audio starts (a
    Traktor grid anchored inside an MP3's header frame), which no Pioneer field
    can store."""
    return max(0.0, seconds + off)


def from_pioneer(seconds: float, off: float) -> float:
    """Back to the decoded time base, never before the start of the track: a
    rekordbox position inside the codec delay is the track's first sample."""
    return max(0.0, seconds - off)
