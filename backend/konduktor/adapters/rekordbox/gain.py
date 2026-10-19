"""rekordbox's auto gain: one `djmdMixerParam` row per track.

MEASURED (kit G2, 2026-10-19 — `backend/dev_gain_kit_g2.py` and the
gain-normalisation discussion):

  * `GainHigh/GainLow` are the two 16-bit halves of a big-endian float32, a
    LINEAR gain; pyrekordbox's `DjmdMixerParam.Gain` reads and writes it in dB.
    It is what PERFORMANCE mode's GRID EDIT > Auto Gain (and EXPORT mode's
    DUAL PLAYER AUTO GAIN knob) shows, with Preferences > Controller > Mixer >
    Auto Gain enabled.
  * rekordbox's own analysis sets it to `-10 - LUFS` (BS.1770 integrated):
    exact on its 19 WAV samples (sd 0.13 dB), +0.24 dB (sd 0.17) on 5 MP3 songs.
  * `PeakHigh/PeakLow`: the linear SAMPLE peak, of a CLIPPED decode — never
    above 1.0 (a track with a true peak of +3.9 dBTP stored 0.00 dB).
  * A row Konduktor writes is kept on load. A track with NO row plays at
    Auto Gain 0.0 dB, and loading it creates none — so a track Konduktor adds
    gets its row at add time, as rekordbox's own add analyses one.
  * No USB export carries it (diffed across two exports with different gains).
"""

from __future__ import annotations

from ...core.loudness import Loudness

#: The level rekordbox's auto gain brings a track to, in LUFS.
REFERENCE_LUFS = -10.0


def values(loudness: Loudness) -> tuple[float, float]:
    """(gain dB, peak dB) for a measured track — the peak clipped at 0 dBFS as
    rekordbox's own is."""
    return REFERENCE_LUFS - loudness.lufs, min(0.0, loudness.true_peak)
