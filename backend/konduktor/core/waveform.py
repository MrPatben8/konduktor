"""Per-column loudness and brightness of a track, for drawing a waveform OVERVIEW.

Platform-independent on purpose: this decodes and measures, and stops there. How
a column becomes pixels — Pioneer's 5-bit height plus 3-bit "whiteness", say —
is the target's business (`adapters/rekordbox/anlz_writer.py`).

Why an export needs it at all: rekordbox shows a track's beatgrid and hot cues
ONLY when its `.DAT` also carries the preview waveform (and a `PVBR` tag).
Measured by stripping tags from a rekordbox-written stick one at a time — the
grid and cue tags alone, byte-for-byte rekordbox's own, show nothing.

Decoding goes through `librosa`, like grid detection: libsndfile for most
formats and, for AAC (every Traktor `.stem.m4a`), `audioread`'s CoreAudio
backend on macOS. Where nothing can decode a file, `columns()` returns None and
the caller decides what a missing overview means.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: Plenty for an overview, and half the decode cost of 44.1 kHz. Brightness is
#: measured below 11 kHz, which is where it is audible anyway.
_SR = 22050
#: "Bright" = share of spectral energy above this.
_BRIGHT_HZ = 2500.0


@dataclass
class Columns:
    """`n` equal slices of a track, start to end."""

    #: RMS amplitude of each slice, on the decoded signal's 0-1 full scale.
    rms: np.ndarray
    #: Share of each slice's spectral energy above ~2.5 kHz, 0-1.
    brightness: np.ndarray
    #: Decoded length in seconds.
    duration: float


def columns(path: Path, n: int) -> Columns | None:
    """Measure `n` columns of `path`, or None when it cannot be decoded."""
    try:
        import librosa

        y, sr = librosa.load(str(path), sr=_SR, mono=True)
    except Exception as ex:  # any decoder failure: no overview, not a failed export
        log.warning("could not decode %s for a waveform: %s", path, ex)
        return None
    if len(y) < n:
        return None

    import librosa

    rms = np.array([float(np.sqrt(np.mean(c * c))) for c in np.array_split(y, n)])
    spec = np.abs(librosa.stft(y, n_fft=1024, hop_length=1024)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=1024)
    share = spec[freqs > _BRIGHT_HZ].sum(0) / (spec.sum(0) + 1e-12)
    brightness = np.array([float(c.mean()) if len(c) else 0.0
                           for c in np.array_split(share, n)])
    return Columns(rms=rms, brightness=brightness, duration=len(y) / sr)
