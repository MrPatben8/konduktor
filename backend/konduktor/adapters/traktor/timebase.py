"""Traktor's clock vs the decoded audio's — one rule, used at every boundary.

**On an MP3 with a gapless header, Traktor's time 0 is NOT the decoded audio's
time 0.** Traktor decodes MP3 naively: it ignores the LAME gapless fields and
decodes the Xing/Info header frame as if it were audio. A gapless decoder skips
that frame and trims the encoder's start padding. So every position Traktor
stores — grid markers, cues, loops — sits

    samples_per_frame + encoder_delay + 529      (= 2257 for LAME's usual 576)

samples LATER than the same moment in the decoded audio: ~51.2 ms at 44.1 kHz,
~47.0 ms at 48 kHz. The rule is in SAMPLES. An MP3 WITHOUT a header is decoded
the same way by both sides (nothing to skip, nothing to trim), so its offset is
0 — as is every other format: WAV, AIFF, FLAC and AAC (Traktor honours the MP4
edit list) line up to the sample.

Measured 2026-09-30 in Traktor 4.5 ("kit 4", see the stem-conversion discussion
log): hotcues Konduktor wrote on a synthetic kick, blinded, landed on it only
at this offset, and hotcues placed by hand in Traktor read back at it (+2255 at
44.1 kHz, +2242 at 48 kHz, header-less −6, WAV −4, AAC −13). Traktor's AutoGrid
agrees, and `bench_grid_detect.py` shows it on every Traktor-analysed MP3.
Derived, not measured: a header frame whose delay field a gapless decoder does
not trust (no LAME/Lavf/Lavc tag, or a VBRI tag) — the decoder then skips the
frame but trims nothing, so the offset is one frame.

The generic model is in the DECODED time base — what Konduktor's deck plays and
the grid detector measures. So the Traktor store ADDS the offset on every write
and the projection SUBTRACTS it on every read. A read is NOT clamped at 0: a
Traktor grid anchor can sit inside the header frame (a beat before the decoded
audio starts), and clamping it would move the grid on its next write-back.
Get it wrong and nothing fails: every MP3 cue and beat is simply ~51 ms early in
Traktor, or late in Konduktor — which is what every release before this did.
"""
from __future__ import annotations

from pathlib import Path

from ...core import mp3_gapless


def offset_samples(path: Path | str | None) -> tuple[int, int]:
    """(samples Traktor runs behind the decoded audio, sample rate).

    (0, 0) for anything that is not an MP3 with a header frame — including a
    file that cannot be read: with no header to go by, 0 is the answer that
    cannot move a cue, since a position read at 0 is written back at 0.
    """
    if not path or Path(path).suffix.lower() != ".mp3":
        return 0, 0
    start = mp3_gapless.read(path)
    if start is None or not start.header_frame:
        return 0, 0
    return start.samples_per_frame + start.trimmed, start.sample_rate


def offset_ms(path: Path | str | None) -> float:
    """Milliseconds to ADD to a decoded-audio time to get Traktor's."""
    samples, rate = offset_samples(path)
    return samples * 1000.0 / rate if samples else 0.0


def to_traktor_ms(seconds: float, off_ms: float) -> float:
    """A decoded-audio time in seconds -> a Traktor START in ms (never < 0:
    Traktor cannot store a position before its own time 0)."""
    return max(0.0, seconds * 1000.0 + off_ms)


def from_traktor_ms(start_ms: float, off_ms: float) -> float:
    """A Traktor START in ms -> decoded-audio seconds. Deliberately unclamped."""
    return (start_ms - off_ms) / 1000.0
