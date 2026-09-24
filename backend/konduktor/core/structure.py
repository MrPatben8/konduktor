"""Track structure on a KNOWN beatgrid: sections, and the events a DJ cues.

This is what Auto Hotcues resolves its slots against ("first drop", "start of
the second breakdown", …). It replaced a librosa Laplacian segmentation that
ran its own beat tracker and ignored the track's grid; against Rekordbox's
phrase analysis its cues scored WORSE than placing one every 16 bars.

Why it is built the way it is — each choice was measured against the phrase
analysis Rekordbox stores for 20 local tracks (PSSI; see the Auto Hotcues
section of CLAUDE.md for the numbers):

- **Everything is per BAR, on the track's own grid.** Bar 1 is the grid's first
  marker (Traktor's convention: a marker is beat 1). Detecting the bar start
  from audio was tried and never beat that rule — DJ tracks start on a
  downbeat — so a wrong bar 1 is fixed by moving the marker, not second-guessed.
- **The features are the ones a DJ hears**: is the kick in, is the bass in,
  how loud and bright is it — plus timbre (MFCC), for changes that keep the
  level (a lead swapping out). Timbre-only novelty scored below this: the
  sections that get cued are defined by energy, not by chord changes.
- **Sections are an optimal segmentation, and the boundary price depends on
  where it falls**: cheap on a multiple of 8 bars, dearer on 4, very dear
  anywhere else. That is the phrase bias, built into the objective rather than
  applied as a snap afterwards — a snap can only move a boundary, never decide
  that an off-phrase change was not worth a boundary at all.
- **A drop is a high section that follows a low one** — not merely a loud one.
  A house intro is often as loud as its drop (the full groove, for mixing), and
  what makes the drop is the breakdown before it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

_SR = 22050
_FR = 100            # envelope frame rate for per-bar levels (Hz)

# Boundary cost multipliers by bar position (x the variance of ~4 bars).
_LAMBDA_8 = 0.2
_LAMBDA_4 = 1.0
_LAMBDA_OTHER = 12.0
_MIN_SECTION = 4     # bars
_HIGH_DB = 4.0       # a section within this of the track's loud parts is "high"
_TIMBRE_WEIGHT = 1.0
_OUTRO_BARS = 16     # the outro is looked for this far before the music ends
_BUILD_MAX = 16      # a longer low stretch is not all build: take its last 8 bars

# The events a slot can be bound to, in the order the dialog lists them.
EVENTS: tuple[str, ...] = (
    "first_beat", "intro_end",
    "build_1", "drop_1", "breakdown_1",
    "build_2", "drop_2", "breakdown_2",
    "build_3", "drop_3", "breakdown_3",
    "outro", "last_beat",
)


@dataclass(frozen=True)
class Section:
    start: int   # bar index (0 = the grid's first marker)
    end: int     # exclusive
    high: bool


@dataclass
class Structure:
    beats: np.ndarray          # every grid beat time (s), including before the first marker
    bar0: int                  # index into `beats` of bar 1's downbeat
    n_bars: int
    duration: float
    sections: list[Section] = field(default_factory=list)
    events: dict[str, int] = field(default_factory=dict)  # event -> BEAT index

    def beat_of_bar(self, bar: int) -> int:
        return self.bar0 + 4 * bar

    def beat_time(self, index: int) -> float:
        """Time of beat ``index``, extrapolated past either end at the local tempo."""
        b = self.beats
        if 0 <= index < len(b):
            return float(b[index])
        if index < 0:
            step = b[1] - b[0] if len(b) > 1 else 0.5
            return float(b[0] + index * step)
        step = b[-1] - b[-2] if len(b) > 1 else 0.5
        return float(b[-1] + (index - len(b) + 1) * step)


# ---- the grid -----------------------------------------------------------------
def grid_beats(markers: list[tuple[float, float]], duration: float) -> tuple[np.ndarray, int]:
    """Every beat of a (possibly flexible) grid, and the index of the first marker.

    Each marker's tempo governs until the next marker, as the deck draws it;
    beats before the first marker are extrapolated back to the track start.
    """
    markers = sorted(markers)
    start0, bpm0 = markers[0]
    step0 = 60.0 / bpm0
    before = np.arange(start0 - step0, -1e-9, -step0)[::-1]
    beats = list(before)
    bar0 = len(beats)
    for i, (start, bpm) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else duration
        beats.extend(np.arange(start, end - 1e-6, 60.0 / bpm))
    return np.asarray(beats, dtype=np.float64), bar0


# ---- per-bar features ----------------------------------------------------------
def _power_env(y: np.ndarray, lo: float | None, hi: float | None) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt

    if lo and hi:
        sos = butter(4, [lo, hi], btype="bandpass", fs=_SR, output="sos")
    elif hi:
        sos = butter(4, hi, btype="lowpass", fs=_SR, output="sos")
    else:
        sos = butter(4, lo, btype="highpass", fs=_SR, output="sos")
    x = sosfiltfilt(sos, y) ** 2
    h = _SR // _FR
    n = len(x) // h
    return x[: n * h].reshape(n, h).mean(axis=1)


def _db(x):
    return 10.0 * np.log10(np.asarray(x) + 1e-10)


def _bar_features(y: np.ndarray, beats: np.ndarray, bar0: int, n_bars: int):
    """``(D, T)``: per-bar [kick, bass, mid, high, loud] and mean MFCC.

    Levels are dB BELOW the track's own 90th percentile, so every track is on
    the same scale whatever its mastering; kick is 0..~1 of the track's usual.
    """
    import librosa

    from . import grid_detect

    bass = _power_env(y, 30.0, 120.0)
    mid = _power_env(y, 200.0, 2000.0)
    high = _power_env(y, 5000.0, None)
    full = _power_env(y, 20.0, 10000.0)
    # The kick is read as the grid detector reads it: the sharp low-band onset,
    # peaked within ±15 ms of each beat. Comparing on-beat to off-beat ENERGY
    # does not work: a house bassline lives on the off-beat.
    kick_on = grid_detect._onset(grid_detect._band_env(y, _SR, 30.0, 150.0))
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    mel = librosa.power_to_db(librosa.feature.melspectrogram(S=S**2, sr=_SR, n_mels=40))
    mfcc = librosa.feature.mfcc(S=mel, n_mfcc=13)
    mfcc_t = librosa.frames_to_time(np.arange(mfcc.shape[1]), sr=_SR, hop_length=512)

    rows, timbre = [], []
    for b in range(n_bars):
        i = bar0 + 4 * b
        t0, t1 = beats[i], beats[i + 4]
        ks = []
        for k in range(4):
            c = int(round(beats[i + k] * 1000))
            seg = kick_on[max(0, c - 15) : c + 16]
            ks.append(float(seg.max()) if len(seg) else 0.0)
        a, z = int(t0 * _FR), max(int(t1 * _FR), int(t0 * _FR) + 1)
        rows.append([
            float(np.median(ks)),
            _db(bass[a:z].mean()), _db(mid[a:z].mean()), _db(high[a:z].mean()), _db(full[a:z].mean()),
        ])
        ia, iz = np.searchsorted(mfcc_t, t0), np.searchsorted(mfcc_t, t1)
        timbre.append(mfcc[:, ia : max(iz, ia + 1)].mean(axis=1))
    D = np.asarray(rows)
    D[:, 0] /= np.percentile(D[:, 0], 90) + 1e-9
    D[:, 1:] -= np.percentile(D[:, 1:], 90, axis=0)
    D[:, 1:] = np.maximum(D[:, 1:], -30.0)  # silence is only "very quiet"
    return D, np.asarray(timbre), _db(full)


# ---- segmentation ---------------------------------------------------------------
def _segment(F: np.ndarray, penalty: np.ndarray) -> list[int]:
    """Section start bars minimising squared deviation from each section's mean
    plus a price per boundary (exact dynamic programme; n is ~100–250 bars)."""
    n = len(F)
    cs = np.vstack([np.zeros(F.shape[1]), np.cumsum(F, axis=0)])
    cs2 = np.r_[0.0, np.cumsum((F**2).sum(axis=1))]

    def cost(i: int, j: int) -> float:
        s = cs[j] - cs[i]
        return float(cs2[j] - cs2[i] - (s @ s) / (j - i))

    best = np.full(n + 1, np.inf)
    best[0] = 0.0
    arg = np.zeros(n + 1, dtype=np.int64)
    for j in range(1, n + 1):
        for i in range(0, j):
            # every section is at least _MIN_SECTION long, except a short tail
            if j - i < _MIN_SECTION and j != n:
                continue
            if 0 < i < _MIN_SECTION:
                continue
            v = best[i] + cost(i, j) + (penalty[i] if i else 0.0)
            if v < best[j]:
                best[j], arg[j] = v, i
    starts, j = [], n
    while j > 0:
        starts.append(int(arg[j]))
        j = int(arg[j])
    return sorted(set(starts))


def _sections(D: np.ndarray, T: np.ndarray) -> list[Section]:
    Z = np.column_stack([D[:, 0] * 10, D[:, 1] / 3, D[:, 4] / 3, D[:, 3] / 6, D[:, 2] / 6])
    Tz = (T - T.mean(axis=0)) / (T.std(axis=0) + 1e-9) * _TIMBRE_WEIGHT
    F = np.column_stack([Z, Tz])
    n = len(F)
    idx = np.arange(n)
    scale = float(F.var(axis=0).sum()) * 4.0
    penalty = scale * np.where(idx % 8 == 0, _LAMBDA_8, np.where(idx % 4 == 0, _LAMBDA_4, _LAMBDA_OTHER))
    starts = _segment(F, penalty)
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else n
        d = D[s:e].mean(axis=0)
        # High = near the track's loud parts on BOTH bass and loudness. The kick
        # is deliberately not required: drum & bass has no kick on every beat.
        out.append(Section(s, e, bool(d[1] > -_HIGH_DB and d[4] > -_HIGH_DB)))
    return out


# ---- events ----------------------------------------------------------------------
def _events(secs: list[Section], music_end_bar: int) -> dict[str, int]:
    """Event -> BAR. Drops are high sections entered from a low one."""
    ev: dict[str, int] = {}
    drops: list[int] = []
    seen_low = False
    for i, s in enumerate(secs):
        if s.high:
            if seen_low and (i == 0 or not secs[i - 1].high):
                drops.append(i)
        else:
            seen_low = True

    def run_end(i: int) -> int:
        while i + 1 < len(secs) and secs[i + 1].high:
            i += 1
        return i

    for n, di in enumerate(drops, start=1):
        ev[f"drop_{n}"] = secs[di].start
        prev = secs[di - 1]
        ev[f"build_{n}"] = prev.start if prev.end - prev.start <= _BUILD_MAX else secs[di].start - 8
        # A breakdown is the end of a drop that ANOTHER drop follows; the end of
        # the last one is the outro's business.
        if n < len(drops):
            ev[f"breakdown_{n}"] = secs[run_end(di) + 1].start

    if len(secs) > 1:
        ev["intro_end"] = secs[1].start
    after = drops[-1] if drops else 0
    cands = [s.start for s in secs[after + 1 :]] or [s.start for s in secs[1:]]
    if cands:
        ev["outro"] = min(cands, key=lambda b: abs(b - (music_end_bar - _OUTRO_BARS)))
    return ev


# ---- entry point ---------------------------------------------------------------
def analyse(
    audio_path: str,
    markers: list[tuple[float, float]],
    *,
    y: np.ndarray | None = None,
) -> Structure:
    """Sections and cue events for a track, on its own beatgrid.

    ``markers`` is the grid as ``[(start_sec, bpm), …]``. Raises ``ValueError``
    when there is no grid or too little music to have a structure.
    """
    if not markers:
        raise ValueError("Set a beatgrid first")
    if y is None:
        import librosa
        y, _ = librosa.load(audio_path, sr=_SR, mono=True)
    y = np.asarray(y, dtype=np.float32)
    duration = len(y) / _SR
    beats, bar0 = grid_beats(markers, duration)
    n_bars = max(0, (len(beats) - bar0 - 1) // 4)
    if n_bars < 2 * _MIN_SECTION:
        raise ValueError("Track is too short to find a structure")

    D, T, full_db = _bar_features(y, beats, bar0, n_bars)
    st = Structure(beats=beats, bar0=bar0, n_bars=n_bars, duration=duration)
    st.sections = _sections(D, T)

    # Where the music starts and ends: the first/last beat within 30 dB of the
    # track's loud parts, so a silent lead-in or tail is not cued.
    loud = full_db >= np.percentile(full_db, 90) - 30.0
    frames = np.flatnonzero(loud)
    t_first = frames[0] / _FR if len(frames) else 0.0
    t_last = frames[-1] / _FR if len(frames) else duration
    first_beat = int(np.searchsorted(beats, t_first - 0.05))
    last_beat = max(first_beat, int(np.searchsorted(beats, t_last, side="right")) - 1)
    music_end_bar = min(n_bars, max(0, (last_beat - bar0) // 4))

    st.events = {k: st.beat_of_bar(b) for k, b in _events(st.sections, music_end_bar).items()}
    st.events["first_beat"] = first_beat
    st.events["last_beat"] = last_beat
    return st
