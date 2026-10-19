"""Traktor's auto gain: `<LOUDNESS PEAK_DB PERCEIVED_DB ANALYZED_DB>` per ENTRY.

MEASURED in Traktor 4.5 (kit G1, 2026-10-19 — `backend/dev_gain_kit.py` and
the gain-normalisation discussion):

  * The deck's gain knob reads **PERCEIVED_DB** (an entry written with
    PERCEIVED -6 / ANALYZED +6 / PEAK 2.5 loaded at -6.0).
  * ANALYZED_DB is Traktor's own measurement. On an entry with an empty
    AUDIO_ID, Traktor's first analysis overwrites only ANALYZED_DB and keeps
    PERCEIVED_DB and PEAK_DB; its Analyze > Gain overwrites all three.
  * Traktor's own value is `-9.2 - LUFS` (BS.1770 integrated; stem files
    measured as their stems summed): sd 0.8 dB over 120 tracks
    (`backend/bench_traktor_gain.py`), the miss following nothing measurable.
  * PEAK_DB is the gain that brings the peak to 0 dBFS (= -peak; Traktor's is
    a sample peak, ours a true peak — within ~0.4 dB).

Konduktor writes PERCEIVED and ANALYZED equal, as Traktor's own analysis does.
"""

from __future__ import annotations

from ...core.loudness import Loudness

#: The level Traktor's auto gain brings a track to, in LUFS.
REFERENCE_LUFS = -9.2


def values(loudness: Loudness) -> tuple[float, float, float]:
    """(PEAK_DB, PERCEIVED_DB, ANALYZED_DB) for a measured track, rounded to
    Traktor's six decimals so a re-write of the same measurement is a no-op."""
    gain = round(REFERENCE_LUFS - loudness.lufs, 6)
    return round(-loudness.true_peak, 6), gain, gain
