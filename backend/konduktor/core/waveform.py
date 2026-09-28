"""What a track's waveform is made of — measured once, drawn by whoever needs it.

Platform-independent on purpose: this decodes and measures, and stops there. How
a measurement becomes pixels — Pioneer's 5-bit heights, 3-band bytes, colour
bits — is the target's business (`adapters/rekordbox/anlz_writer.py`).

Two resolutions, from ONE decode:

  * `columns` — `n` equal slices of the whole track (loudness + brightness), for
    an overview such as Pioneer's blue `PWAV` preview;
  * `frames`  — 150 per second, each split into low / mid / high band energy,
    for a detail waveform and a 3-band overview (Pioneer's `PWV3`-`PWV7`).

The bands cross over at 200 Hz and 2.5 kHz: fitted against rekordbox's own 3-band
waveforms for ten local tracks (correlation 0.89 / 0.80 / 0.77 per band). Energy
is left unscaled — each target normalises it its own way.

Decoding goes through PyAV (FFmpeg, bundled in its wheel) and falls back to
`librosa`, like grid detection: libsndfile for most formats
and, for AAC (every Traktor `.stem.m4a`), `audioread`'s CoreAudio backend on macOS.
Where nothing can decode a file, `analyse()` returns None and the caller decides
what a missing waveform means.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: BUMP THIS whenever `analyse()`'s output changes (a constant below, the
#: decoder, the maths). Exports cache these measurements on the stick
#: (`analysis_cache`), keyed by this number — so an unbumped change keeps
#: serving the old measurement to every re-export.
ANALYSIS_VERSION = 1

#: Plenty for a waveform, and half the decode cost of 44.1 kHz. The high band
#: runs to 11 kHz, which is where most of its visible energy is anyway.
SR = 22050
#: Detail resolution. Pioneer's detail tags are 150 entries per second.
FPS = 150
N_FFT = 1024
#: Band crossovers, Hz.
LOW_MAX, MID_MAX = 200.0, 2500.0
#: "Bright" (for `columns`) = share of spectral energy above this.
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


@dataclass
class Frames:
    """150-per-second band energies, low / mid / high, shape (n, 3).

    Frame i covers `[i/150, (i+1)/150)` seconds on the clock the caller asked
    for — see `analyse(lead=…)`.
    """

    bands: np.ndarray
    #: Decoded length in seconds (of the audio, not counting any lead).
    duration: float


@dataclass
class Analysis:
    columns: Columns
    frames: Frames

    @property
    def duration(self) -> float:
        return self.columns.duration


def _decode_pyav(path: Path) -> np.ndarray:
    """Mono float32 at `SR`, via PyAV — FFmpeg's decoders, bundled in its wheel.

    The reason it is first: it decodes AAC (every Traktor `.stem.m4a`) on EVERY
    OS, where librosa needs CoreAudio (macOS only) or a system ffmpeg. Measured
    to start on exactly the same sample as librosa's decode for MP3 and AAC, so
    switching decoders moves nothing against the beats.
    """
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):         # flush
            chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _decode(path: Path) -> np.ndarray | None:
    try:
        return _decode_pyav(path)
    except Exception as ex:  # PyAV missing, or a file FFmpeg cannot open
        log.debug("PyAV could not decode %s (%s); trying librosa", path, ex)
    try:
        import librosa

        y, _ = librosa.load(str(path), sr=SR, mono=True)
        return y
    except Exception as ex:  # any decoder failure: no waveform, not a failed export
        log.warning("could not decode %s for a waveform: %s", path, ex)
        return None


def _columns(y: np.ndarray, n: int) -> Columns:
    import librosa

    rms = np.array([float(np.sqrt(np.mean(c * c))) for c in np.array_split(y, n)])
    spec = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=N_FFT)) ** 2
    freqs = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
    share = spec[freqs > _BRIGHT_HZ].sum(0) / (spec.sum(0) + 1e-12)
    brightness = np.array([float(c.mean()) if len(c) else 0.0
                           for c in np.array_split(share, n)])
    return Columns(rms=rms, brightness=brightness, duration=len(y) / SR)


def _frames(y: np.ndarray, lead: float) -> Frames:
    """Band energies at 150/s, with the frame grid starting `lead` s BEFORE the
    audio — how a target whose clock runs behind the decoded audio (rekordbox on
    MP3/AAC) gets frames that line up with its own positions."""
    import librosa

    duration = len(y) / SR
    pad = int(round(max(0.0, lead) * SR))
    padded = np.concatenate([np.zeros(pad, dtype=y.dtype), y]) if pad else y
    n = int(np.ceil((len(padded) / SR) * FPS))
    hop = SR / FPS                                   # 147 samples at 22050 Hz
    spec = np.abs(librosa.stft(padded, n_fft=N_FFT, hop_length=int(round(hop)),
                               center=True)) ** 2
    f = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
    bands = np.stack([
        np.sqrt(spec[f < LOW_MAX].sum(0)),
        np.sqrt(spec[(f >= LOW_MAX) & (f < MID_MAX)].sum(0)),
        np.sqrt(spec[f >= MID_MAX].sum(0)),
    ], axis=1)[:n]
    if len(bands) < n:
        bands = np.vstack([bands, np.zeros((n - len(bands), 3))])
    return Frames(bands=bands, duration=duration)


def analyse(path: Path, *, n_columns: int = 400, lead: float = 0.0) -> Analysis | None:
    """Decode `path` once and measure both resolutions; None if undecodable."""
    y = _decode(path)
    if y is None or len(y) < max(n_columns, N_FFT):
        return None
    return Analysis(columns=_columns(y, n_columns), frames=_frames(y, lead))


def columns(path: Path, n: int) -> Columns | None:
    """Just the overview columns — for a caller that needs no detail."""
    y = _decode(path)
    if y is None or len(y) < n:
        return None
    return _columns(y, n)
