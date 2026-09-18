"""Rekordbox's beatgrid <-> the generic marker list.

**Rekordbox does not store markers.** There is no beatgrid table in
``master.db`` at all; the grid lives in the track's ANLZ analysis file as a
``PQTZ`` tag holding EVERY BEAT explicitly — beat-number-in-bar, BPM and time,
one entry per beat (a 4-minute track is ~500 entries).

Konduktor's generic model is a marker list, so this module is the projection
between the two:

  * read  — collapse runs of equal BPM into one marker each, so a constant-tempo
    track becomes a list of length one and a variable-tempo track becomes one
    marker per tempo change. This is exactly the shape ``core.model.GridMarker``
    describes.
  * write — expand a marker list back into a full beat list (milestone 2; the
    read direction is what a read-only adapter needs).

BPMs are compared with a tolerance because Rekordbox stores them as hundredths
and floating-point division reintroduces noise; two beats within `BPM_EPSILON`
are the same tempo and must not produce a spurious marker.
"""
from __future__ import annotations

from ...core.model import GridMarker

BPM_EPSILON = 0.005  # half of the 0.01 BPM Rekordbox can actually represent


def markers_from_beats(
    times: list[float], bpms: list[float], beats: list[int] | None = None
) -> list[GridMarker]:
    """Collapse a per-beat grid into the generic marker list.

    `times` are seconds, `bpms` the tempo in force at each beat. A marker is
    emitted for the first beat and wherever the tempo changes.
    """
    if not times or not bpms:
        return []
    out: list[GridMarker] = []
    last_bpm: float | None = None
    for i, (t, bpm) in enumerate(zip(times, bpms)):
        if last_bpm is None or abs(bpm - last_bpm) > BPM_EPSILON:
            out.append(GridMarker(start=float(t), bpm=float(bpm), name=None, companion=None))
            last_bpm = float(bpm)
    return out


def beats_from_markers(
    markers: list[GridMarker], duration_sec: float
) -> tuple[list[int], list[float], list[float]]:
    """Expand a marker list into the per-beat arrays Rekordbox stores.

    Each marker governs until the next one; the last runs to `duration_sec`.
    Returns ``(beat_in_bar, bpms, times)`` with beat numbers cycling 1-4 from
    each marker, which is how Rekordbox numbers bars.

    Not used by the read-only adapter, but it is the inverse of
    `markers_from_beats` and belongs beside it.
    """
    beat_nums: list[int] = []
    bpms: list[float] = []
    times: list[float] = []
    if not markers:
        return beat_nums, bpms, times
    ordered = sorted(markers, key=lambda m: m.start)
    for i, m in enumerate(ordered):
        if m.bpm <= 0:
            continue
        end = ordered[i + 1].start if i + 1 < len(ordered) else duration_sec
        step = 60.0 / m.bpm
        t = m.start
        n = 1
        # Guard against a zero/negative span producing an endless loop.
        while t < end - 1e-9 and step > 0:
            beat_nums.append(n)
            bpms.append(m.bpm)
            times.append(t)
            n = n % 4 + 1
            t += step
    return beat_nums, bpms, times
