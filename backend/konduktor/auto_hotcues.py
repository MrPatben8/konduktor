"""Auto Hotcues — structural boundary detection for DJ cue placement.

Detection uses the McFee & Ellis (2014) Laplacian structural segmentation as
implemented on top of librosa: beat-synchronous CQT features → a
sequence-augmented recurrence Laplacian → spectral clustering of beats into
section *types* → boundaries at type changes. This is repetition/homogeneity
aware (unlike a raw energy-novelty curve), so boundaries land on real musical
transitions. Validated on real house tracks: clean, phrase-scale boundaries in
~1.5s each; pure-Python (~150MB), no torch/native build; runs on Python 3.14.

The module is split so the DSP (`detect_boundaries`, needs the audio) is
separate from the placement logic (`select_hotcues`, pure arithmetic on the
beatgrid) — the latter is unit-testable without audio.
"""

from __future__ import annotations

import numpy as np

# Traktor's hotcue bank. Parameterised (not hardcoded at call sites) so it can
# grow if a future Traktor allows more than 8 triggerable hotcues.
MAX_HOTCUES = 8

_SR = 22050
_N_EIGENVECTORS = 6  # section-type clusters for the Laplacian embedding


def detect_boundaries(audio_path: str, sr: int = _SR) -> tuple[list[tuple[float, float]], float]:
    """Detect structural boundaries in a track.

    Returns ``(boundaries, duration)`` where ``boundaries`` is a list of
    ``(time_sec, energy)`` pairs — one per detected section start — and
    ``energy`` is that section's mean RMS normalised to 0..1 across the track
    (used downstream for positional naming). Sorted by time.
    """
    import librosa
    from scipy.ndimage import median_filter
    from scipy.sparse.csgraph import laplacian as csgraph_laplacian
    from scipy.linalg import eigh
    import sklearn.cluster

    y, sr = librosa.load(audio_path, sr=sr, mono=True)
    duration = len(y) / sr
    if duration < 1.0:
        return [], duration

    # Beat-synchronous CQT — the harmonic/timbre backbone of the method.
    cqt = librosa.amplitude_to_db(np.abs(librosa.cqt(y=y, sr=sr)), ref=np.max)
    _, beats = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    if len(beats) < 4:
        return [], duration
    csync = librosa.util.sync(cqt, beats, aggregate=np.median)
    beat_times = librosa.frames_to_time(beats, sr=sr)

    # Sequence-augmented recurrence Laplacian (McFee & Ellis 2014):
    # a repetition graph (which beats sound alike) blended with a local path
    # graph (consecutive-beat timbral continuity).
    rec = librosa.segment.recurrence_matrix(csync, width=3, mode="affinity", sym=True)
    rec = median_filter(rec, size=(1, 7))
    mfcc = librosa.feature.mfcc(y=y, sr=sr)
    msync = librosa.util.sync(mfcc, beats)
    path_dist = np.sum(np.diff(msync, axis=1) ** 2, axis=0)
    path_sim = np.exp(-path_dist / (np.median(path_dist) + 1e-9))
    path = np.diag(path_sim, 1) + np.diag(path_sim, -1)
    deg_path, deg_rec = path.sum(1), rec.sum(1)
    denom = np.sum((deg_path + deg_rec) ** 2) + 1e-9
    mu = deg_path.dot(deg_path + deg_rec) / denom
    graph = mu * rec + (1 - mu) * path

    # Symmetric-normalised Laplacian → eigenvectors → k-means over beats.
    lap = csgraph_laplacian(graph, normed=True)
    _, evecs = eigh(lap)
    evecs = median_filter(evecs, size=(9, 1))
    k = min(_N_EIGENVECTORS, evecs.shape[1])
    x = librosa.util.normalize(evecs[:, :k], norm=2, axis=1)
    ids = sklearn.cluster.KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(x)

    # Boundaries = beats where the section-type label changes (plus the start).
    bound_beats = [0] + [i for i in range(1, len(ids)) if ids[i] != ids[i - 1]]

    # Per-section mean RMS (energy) for positional naming, normalised 0..1.
    rms = librosa.feature.rms(y=y)[0]
    rms_t = librosa.times_like(rms, sr=sr)
    rms_max = float(rms.max()) or 1.0
    out: list[tuple[float, float]] = []
    for j, bi in enumerate(bound_beats):
        t = float(beat_times[min(bi, len(beat_times) - 1)])
        t_next = (
            float(beat_times[min(bound_beats[j + 1], len(beat_times) - 1)])
            if j + 1 < len(bound_beats)
            else duration
        )
        seg = rms[(rms_t >= t) & (rms_t < max(t_next, t + 1e-3))]
        energy = float(seg.mean() / rms_max) if len(seg) else 0.0
        out.append((t, energy))
    return out, duration


def select_hotcues(
    boundaries: list[tuple[float, float]],
    *,
    bpm: float,
    anchor: float,
    duration: float,
    free_slots: list[int],
    existing_times: list[float],
    max_cues: int = MAX_HOTCUES,
    phrase_bars: int = 16,
) -> list[dict]:
    """Turn raw boundaries into hotcue specs, phrase-snapped to the beatgrid.

    Snaps each boundary to the nearest ``phrase_bars``-bar grid point (which
    also merges near-duplicates), drops any coinciding with an existing hotcue,
    caps to the free slots (down-sampling evenly if there are too many), and
    assigns positional names. Returns dicts with slot/start/name/type/length.
    """
    if not boundaries or not free_slots or bpm <= 0:
        return []

    beat = 60.0 / bpm
    phrase = phrase_bars * 4 * beat  # seconds per phrase

    # Snap to phrase grid; dedupe (keep the strongest energy seen at each point).
    snapped: dict[float, float] = {}
    for t, energy in boundaries:
        p = anchor + round((t - anchor) / phrase) * phrase
        p = max(0.0, min(p, duration - beat))
        snapped[p] = max(snapped.get(p, 0.0), energy)

    # Drop points that collide with an existing (hand-placed) hotcue.
    def near_existing(t: float) -> bool:
        return any(abs(t - e) < beat for e in existing_times)

    points = sorted((t, e) for t, e in snapped.items() if not near_existing(t))
    if not points:
        return []

    # If more candidates than free slots, down-sample evenly across the track
    # (keeps intro..outro coverage rather than just the first N).
    n = min(len(free_slots), max_cues)
    if len(points) > n:
        idx = np.linspace(0, len(points) - 1, n).round().astype(int)
        points = [points[i] for i in sorted(set(idx))]

    names = _name_positionally([t for t, _ in points], [e for _, e in points], duration)
    slots = sorted(free_slots)[: len(points)]
    return [
        {"slot": slot, "start": round(t, 3), "name": name, "type": 0, "length": 0.0}
        for slot, (t, _), name in zip(slots, points, names)
    ]


def _name_positionally(times: list[float], energies: list[float], duration: float) -> list[str]:
    """Positional-hint names from track position + energy trend (not true
    structural classification — the names are hints; value is the position)."""
    if not times:
        return []
    med = float(np.median(energies))
    raw: list[str] = []
    n = len(times)
    for i, (t, e) in enumerate(zip(times, energies)):
        frac = t / duration if duration else 0.0
        prev = energies[i - 1] if i > 0 else e
        if i == 0 or frac < 0.08:
            name = "Intro"
        elif i == n - 1 or frac > 0.85:
            name = "Outro"
        elif e - prev > 0.12 and e >= med:
            name = "Drop"
        elif prev - e > 0.12:
            name = "Break"
        elif e > prev:
            name = "Build"
        else:
            name = "Mid"
        raw.append(name)
    # De-duplicate repeated labels with ordinals (Drop, Drop 2, ...).
    counts: dict[str, int] = {}
    out: list[str] = []
    for name in raw:
        counts[name] = counts.get(name, 0) + 1
        out.append(name if counts[name] == 1 else f"{name} {counts[name]}")
    return out
