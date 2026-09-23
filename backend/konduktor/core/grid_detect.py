"""Beatgrid detection: one constant tempo + anchor, fitted to the whole track.

Why not a beat tracker. The previous detector was ``librosa.beat.beat_track``,
and it could not be right: its tempo comes from an autocorrelation over
~23 ms frames, so it can only return ``60 / (k * 23 ms)`` — between 115 and
140 BPM the ONLY values it can produce are 117.45, 123.05, 129.20 and 136.00.
A 125 BPM track came back as 123.05 and drifted ~2 beats per minute, and its
first beat carried the onset curve's own ~23 ms lag. Against 29 grids
Rekordbox analysed (``bench_grid_detect.py``) it got 1 BPM and 0 grids right.

Dance music makes a much stronger assumption available, the one Traktor's and
Rekordbox's analysers make: the tempo is CONSTANT. So instead of tracking
beats, this fits one pulse train to every onset in the track:

1. **Onset curves at 1 kHz from zero-phase band filters** (``sosfiltfilt``
   adds no group delay): a kick band, a mid band and the full signal.
2. **Tempo from the whole track's spectrum.** A pulse train at ``f`` Hz has
   energy at ``f, 2f, 3f …``; the onset spectrum's magnitude summed over those
   harmonics peaks at the tempo, as finely as ``1 / track length`` allows,
   then parabolic refinement goes further.
3. **Octave by comb score**: a comb at half the tempo hits every kick too, so
   the FASTEST candidate scoring nearly as well as the best wins, inside
   ``bpm_range``. Halftime genres stay genuinely ambiguous — ×2 / ÷2 exist.
4. **Phase from attacks that are followed by low end.** Timing comes from a
   1–8 kHz attack band, whose onset is sharp (a kick's low end swells for ~25 ms after
   its attack, so the kick band alone lands late). Which attack is the beat is
   decided by whether low end follows it: that rejects the off-beat hat (no low
   end) and the syncopated bassline (no attack), each of which won a benchmark
   track when judged by one band alone.
5. **Round BPMs are snapped** only if the round tempo keeps every section of
   the track on the beat — a direct test, rather than a score ratio.

Positions are in the DECODED audio's time base — the one Konduktor's deck
plays (libsndfile and macOS CoreAudio agree to the sample on MP3). A platform
may disagree for lossy files: Rekordbox's grids sit a constant ~25 ms later on
every MP3/AAC in the benchmark (the decoder delay it does not trim) and at 0 on
WAV. That is a platform time-base difference, not a detection error, and it is
deliberately not corrected here.

``drift_ms`` reports how far the fitted grid strays from the beat in its
worst section — the number that says a slowly wandering tempo is not being
described by one marker. A sudden tempo change is out of its reach (that is
flexible-grid detection, not built).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_SR = 22050          # plenty for a kick; halves decode + filter cost vs 44.1k
_FR = 1000           # onset-curve frame rate (Hz) — 1 ms timing resolution
_TEMPO_FR = 250      # the tempo spectrum only needs content up to ~15 Hz
_HARMONICS = 8       # pulse-train harmonics summed for the tempo score

# The band an attack's timing is read from. A kick's beater click reaches well
# above 1 kHz; a bass note's leakage does not, which is what stops a
# syncopated bassline (whose low end is as loud as the kick's) from posing as
# one. The hat lives here too, but has no low end to follow it.
_ATTACK_LO_HZ = 1000.0
_ATTACK_HI_HZ = 8000.0
# How long after an attack a kick's low end is looked for (ms).
_KICK_FOLLOW_MS = 45
# Measured, not derived: on WAV loops that start exactly on the beat, the
# mid-band onset peak lands this far before the true attack (the centred
# smoothing's leading edge). Added back so the grid sits ON the transient.
_ONSET_BIAS_S = 0.002

# The detected range. Anything outside is folded in by octaves, which is where
# half/double-tempo answers come from — and what ×2 / ÷2 are for.
DEFAULT_BPM_RANGE = (70.0, 180.0)


@dataclass(frozen=True)
class GridResult:
    bpm: float
    anchor: float          # seconds: the first beat at or after the music starts
    duration: float        # seconds
    # Worst distance between the grid and the beat over any ~30 s section with
    # a clear beat. A few ms on a produced track; tens of ms means the tempo
    # wanders (a live drummer, a vinyl rip). A sudden tempo CHANGE is not seen.
    drift_ms: float | None = None


# ---- onset curves -------------------------------------------------------------
def _band_env(
    y: np.ndarray, sr: int, lo: float | None, hi: float | None, smooth_ms: int = 11
) -> np.ndarray:
    """Log-energy envelope of one band at ``_FR`` Hz, zero-phase throughout."""
    from scipy.signal import butter, sosfiltfilt

    if lo and hi:
        sos = butter(4, [lo, hi], btype="bandpass", fs=sr, output="sos")
    elif hi:
        sos = butter(4, hi, btype="lowpass", fs=sr, output="sos")
    elif lo:
        sos = butter(4, lo, btype="highpass", fs=sr, output="sos")
    else:
        sos = None
    x = sosfiltfilt(sos, y) if sos is not None else y
    hop = sr / _FR
    n = int(len(x) / hop)
    # Block energy on a fractional hop, so frame i is centred on time i / _FR.
    edges = np.round(np.arange(n + 1) * hop).astype(np.int64)
    c = np.concatenate(([0.0], np.cumsum(x.astype(np.float64) ** 2)))
    e = (c[edges[1:]] - c[edges[:-1]]) / np.maximum(1, np.diff(edges))
    # A centred smooth removes a band's carrier ripple (a 50 Hz kick needs
    # ~10 ms) without moving anything in time.
    if smooth_ms > 1:
        e = np.convolve(e, np.ones(smooth_ms) / smooth_ms, mode="same")
    return np.log1p(e / (np.percentile(e, 90) + 1e-12) * 100.0)


def _onset(env: np.ndarray) -> np.ndarray:
    """Positive slope of a log envelope, centred so a peak sits ON the attack."""
    d = np.zeros_like(env)
    d[1:-1] = (env[2:] - env[:-2]) / 2
    d = np.maximum(d, 0.0)
    # Remove a slowly varying floor so long crescendos don't read as onsets.
    from scipy.ndimage import uniform_filter1d
    d = np.maximum(d - uniform_filter1d(d, size=_FR // 4), 0.0)
    return d / (d.max() + 1e-12)


# ---- tempo --------------------------------------------------------------------
def _decimate(o: np.ndarray, factor: int) -> np.ndarray:
    n = len(o) // factor
    return o[: n * factor].reshape(n, factor).sum(axis=1)


def _dft(o: np.ndarray, fr: float, freqs: np.ndarray) -> np.ndarray:
    """|DFT| of ``o`` at arbitrary frequencies (Hz), chunked to bound memory."""
    t = np.arange(len(o)) / fr
    out = np.empty(len(freqs))
    step = max(1, 4_000_000 // max(1, len(o)))
    for i in range(0, len(freqs), step):
        f = freqs[i : i + step, None]
        out[i : i + step] = np.abs(np.exp(-2j * np.pi * f * t) @ o)
    return out


def _harmonic_score(o: np.ndarray, fr: float, bpms: np.ndarray, n_harm: int = _HARMONICS) -> np.ndarray:
    f0 = bpms / 60.0
    return sum(_dft(o, fr, f0 * k) for k in range(1, n_harm + 1))


def _coarse_candidates(o_t: np.ndarray, lo: float, hi: float) -> list[float]:
    """Local maxima of the harmonic tempo score over [lo/2, hi*2] BPM."""
    n = 1 << int(np.ceil(np.log2(len(o_t) * 8)))
    spec = np.abs(np.fft.rfft(o_t - o_t.mean(), n))
    df = _TEMPO_FR / n
    bpms = np.arange(lo / 2, hi * 2, 0.05)
    score = np.zeros_like(bpms)
    for k in range(1, _HARMONICS + 1):
        idx = np.round(bpms / 60.0 * k / df).astype(np.int64)
        idx = idx[idx < len(spec)]
        score[: len(idx)] += spec[idx]
    from scipy.signal import find_peaks
    peaks, _ = find_peaks(score, distance=int(1.0 / 0.05))
    peaks = peaks[np.argsort(score[peaks])[::-1][:8]]
    return [float(bpms[p]) for p in peaks]


def _refine(o_t: np.ndarray, bpm: float, width: float, step: float) -> tuple[float, float]:
    grid = np.arange(bpm - width, bpm + width + step / 2, step)
    s = _harmonic_score(o_t, _TEMPO_FR, grid)
    i = int(np.argmax(s))
    # Parabolic interpolation between the three samples around the max.
    if 0 < i < len(s) - 1:
        a, b, c = s[i - 1], s[i], s[i + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den != 0 else 0.0
        return float(grid[i] + off * step), float(b)
    return float(grid[i]), float(s[i])


def _fold(o: np.ndarray, period_s: float, smooth_ms: int = 11, start: int = 0) -> np.ndarray:
    """Onset curve folded onto one period: mean strength at each phase (1 ms bins).

    ``start`` is the frame ``o`` begins at, so a slice of the track folds onto
    the same phase axis as the whole of it.
    """
    bins = max(8, int(round(period_s * _FR)))
    t = (np.arange(len(o)) + start) / _FR
    ph = ((t / period_s) % 1.0 * bins).astype(np.int64) % bins
    h = np.bincount(ph, weights=o, minlength=bins)
    cnt = np.bincount(ph, minlength=bins)
    h = h / np.maximum(cnt, 1)
    # Circular triangle smoothing, centred, so one jittery frame cannot win.
    w = np.bartlett(smooth_ms + 2)[1:-1]
    w /= w.sum()
    kern = np.roll(np.pad(w, (0, bins - len(w))), -(len(w) // 2))
    return np.real(np.fft.ifft(np.fft.fft(h) * np.fft.fft(kern)))


def _comb(o: np.ndarray, bpm: float) -> float:
    """Peak of the folded onset curve: the mean onset strength ON the beat."""
    return float(_fold(o, 60.0 / bpm).max())


def _in_range(bpm: float, lo: float, hi: float) -> float:
    while bpm < lo:
        bpm *= 2
    while bpm >= hi:
        bpm /= 2
    return bpm


# ---- entry point --------------------------------------------------------------
def detect_grid(
    audio_path: str,
    bpm_range: tuple[float, float] = DEFAULT_BPM_RANGE,
    *,
    y: np.ndarray | None = None,
    sr: int = _SR,
) -> GridResult:
    """Detect a track's constant tempo and first beat.

    ``y``/``sr`` let a caller (or a test) pass decoded mono audio directly.
    Raises ``ValueError`` when there is no pulse to fit (silence, a one-shot).
    """
    if y is None:
        import librosa
        y, sr = librosa.load(audio_path, sr=sr, mono=True)
    y = np.asarray(y, dtype=np.float32)
    duration = len(y) / sr
    if duration < 1.5:
        raise ValueError("Track is too short to detect a tempo")

    lo, hi = bpm_range
    low = _onset(_band_env(y, sr, 30.0, 150.0))
    mid = _onset(_band_env(y, sr, 150.0, 4000.0, smooth_ms=3))
    attack = _onset(_band_env(y, sr, _ATTACK_LO_HZ, _ATTACK_HI_HZ, smooth_ms=3))
    full = _onset(_band_env(y, sr, None, None, smooth_ms=3))
    o_tempo = low + mid + 0.5 * full
    if not np.isfinite(o_tempo).all() or o_tempo.max() <= 0:
        raise ValueError("No onsets found")

    o_t = _decimate(o_tempo, _FR // _TEMPO_FR)
    cands = _coarse_candidates(o_t, lo, hi)
    if not cands:
        raise ValueError("Could not detect a tempo")

    # Refine every coarse candidate, fold each into the range, and score it
    # with the comb (onset strength on the beat, best phase).
    refined: dict[float, tuple[float, float]] = {}
    for c in cands:
        b, _ = _refine(o_t, c, 0.1, 0.01)
        b = _in_range(b, lo, hi)
        key = round(b, 1)
        if key not in refined:
            refined[key] = (b, _comb(o_tempo, b))
    best = max(s for _, s in refined.values())
    # Octave rule: prefer the FASTEST tempo that explains the onsets nearly as
    # well as the best — a slower comb hits a subset of the same beats.
    viable = [(b, s) for b, s in refined.values() if s >= 0.9 * best]
    bpm = max(viable)[0]

    # Final refinement at full precision around the chosen tempo.
    bpm, _ = _refine(o_t, bpm, 0.02, 0.001)
    phase = _phase(low, attack, 60.0 / bpm)
    drift = _drift(attack, bpm, phase)
    bpm, phase, drift = _snap_round(low, attack, bpm, phase, drift, duration)

    period = 60.0 / bpm
    return GridResult(
        bpm=round(bpm, 4),
        anchor=round(_first_beat(low + attack, phase, period), 4),
        duration=duration,
        drift_ms=round(drift * 1000.0, 1),
    )


def _peaks(f: np.ndarray, k: int, min_sep: int) -> list[int]:
    """The ``k`` highest local maxima of a circular curve, ``min_sep`` apart."""
    n = len(f)
    is_max = (f >= np.roll(f, 1)) & (f >= np.roll(f, -1))
    out: list[int] = []
    for i in np.flatnonzero(is_max)[np.argsort(f[is_max])[::-1]]:
        if all(min(abs(i - j), n - abs(i - j)) >= min_sep for j in out):
            out.append(int(i))
            if len(out) == k:
                break
    return out


def _phase(low: np.ndarray, attack: np.ndarray, period: float) -> float:
    """Seconds into the beat period at which the beat lands.

    Timing comes from the attack band, whose onset is sharp; the kick band's
    is not — a kick's low end swells for tens of ms after its attack. But the
    loudest attack is not always the beat (an off-beat hat), and the loudest
    low-band onset is not always the kick (a bassline note between beats). So
    each sharp attack is scored by whether LOW END FOLLOWS it: a kick has both,
    a hat has no low end, a bass note has no attack.
    """
    fine = _fold(attack, period, smooth_ms=3)
    low_f = _fold(low, period)
    n = len(fine)
    floor = 0.1 * low_f.max()  # a track with no kick still picks its best attack

    def score(c: int) -> float:
        follow = max(low_f[(c + d) % n] for d in range(-5, _KICK_FOLLOW_MS + 1))
        return float(fine[c] * (floor + follow))

    best = max(_peaks(fine, 6, 20), key=score)
    return (best / n * period + _ONSET_BIAS_S) % period


def _segment_offsets(attack: np.ndarray, period: float, phase: float) -> list[float]:
    """Where the beat lands in each ~30 s section, relative to ``phase`` (s).

    Only sections with a clear attack within 40 ms of the grid count. That
    makes this a measure of SLOW drift — a fit a few hundredths of a BPM out,
    which is what the round-BPM snap must judge. It cannot see a tempo change
    (a section whose beat is far off the grid looks the same as a kickless
    breakdown with hats over it, and on real music treating those as drift
    reported 100–250 ms on tracks whose grid was right end to end). Detecting
    tempo changes is flexible-grid detection, and a different job.
    """
    n = len(attack)
    seg = min(30 * _FR, n // 4)
    if seg < 6 * _FR:
        return []
    out = []
    for s0 in range(0, n - seg + 1, seg):
        f = _fold(attack[s0 : s0 + seg], period, smooth_ms=3, start=s0)
        bins = len(f)
        c = int(round(phase / period * bins)) % bins
        i = max(((c + d) % bins for d in range(-40, 41)), key=lambda j: f[j])
        if f[i] < 2.0 * np.median(f):
            continue
        d = (i - c + bins / 2) % bins - bins / 2
        out.append(d / bins * period)
    return out


def _drift(attack: np.ndarray, bpm: float, phase: float) -> float:
    """Distance (s) between the grid and the beat in the worst section but one.

    One section is allowed to disagree: an intro whose nearest attack is not
    the kick shows up as a single ~30 ms outlier on otherwise flat tracks, and
    blocked two round-BPM snaps on the benchmark. Real drift cannot hide behind
    that — it grows with distance from the anchor, so it is at both ends.
    """
    offs = sorted((abs(o) for o in _segment_offsets(attack, 60.0 / bpm, phase)), reverse=True)
    if len(offs) >= 4:
        offs = offs[1:]
    return offs[0] if offs else 0.0


def _snap_round(low, attack, bpm: float, phase: float, drift: float, duration: float):
    """Snap to a round (or half) BPM if it keeps the whole track on the beat.

    The test is the thing that matters, not a score ratio: at the round tempo,
    does every section of the track stay within a few ms of its onsets? A real
    124.01 BPM track snapped to 124 would drift ~30 ms over six minutes and
    fails; a 124.0 track whose fit came out at 124.012 passes.

    How far to look scales with 1/duration, because so does the fit's
    precision: the window is the tempo error that would move the LAST beat by
    10 ms, floored at 0.05 BPM. A four-bar loop gets a wide one; a track that
    is long enough to have a precise fit relies on the drift test instead.
    """
    tol = max(0.05, 0.01 * bpm / max(duration, 1.0))
    for r in (float(round(bpm)), round(bpm * 2) / 2):
        if abs(bpm - r) < tol:
            ph = _phase(low, attack, 60.0 / r)
            dr = _drift(attack, r, ph)
            if dr <= max(drift, 0.004) + 0.003:
                return r, ph, dr
    return bpm, phase, drift


def _first_beat(o: np.ndarray, phase: float, period: float) -> float:
    """The first grid beat at which the music has started.

    A grid anchored in the silence before a track is still correct, but a DJ
    reads the first marker as "the track starts here", as Traktor draws it.
    """
    pos = o[o > 0]
    thr = 0.15 * np.percentile(pos, 99) if len(pos) else 0.0
    hits = np.flatnonzero(o > thr)
    start = hits[0] / _FR if len(hits) else 0.0
    # The beat nearest the music's first onset, never before the track starts.
    n = round((start - phase) / period)
    return max(phase + n * period, phase % period)
