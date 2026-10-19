"""The analysis a platform that ANALYSES what it adds needs for one new track.

Rekordbox draws a new track's waveform and grid from its ANLZ files, so adding a
track means decoding it once: the waveform measured on the platform's own clock
(`lead`, read from the file's header by that platform) plus a detected grid and
key when the track brings none. One definition, used both by the adapter (a library on
this computer) and by the client of a library held by a server — which never
decodes audio, so the client measures its own copy and sends the result
(`PreparedAnalysis`) with the track.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .adapter import PreparedAnalysis
from .model import GridMarker


def prepare(path: Path, *, lead: float, detect: bool = True,
            detect_key: bool = False, measure_loudness: bool = False) -> PreparedAnalysis:
    """Decode `path` once: its waveform at `lead`, and (with `detect`) a grid,
    (with `detect_key`) a key. `measure_loudness` adds the file's loudness —
    a decode of its own, at the file's rate and channels (BS.1770 cannot use
    the analysis decode; see `core.loudness`), ~1 s a track."""
    from . import grid_detect, waveform

    samples = waveform.decode(Path(path))
    measured = waveform.analyse_samples(samples, lead=lead)
    markers: list[GridMarker] = []
    if detect and samples is not None:
        try:
            found = grid_detect.detect_grid(str(path), y=samples, sr=waveform.SR)
            markers = [GridMarker(start=found.anchor, bpm=found.bpm)]
        except ValueError:
            pass  # no pulse to fit (a one-shot, silence): no grid
    key = None
    if detect_key and samples is not None:
        from . import key_detect

        try:
            key = key_detect.detect_key_samples(samples, waveform.SR)
        except Exception:  # noqa: BLE001 - a missing key is not worth failing an add over
            logging.getLogger(__name__).warning("key detection failed", exc_info=True)
    measured_loudness = None
    if measure_loudness:
        from . import loudness

        try:
            measured_loudness = loudness.measure_file(path)
        except Exception:  # noqa: BLE001 - a missing gain is not worth failing an add over
            logging.getLogger(__name__).warning("loudness measurement failed", exc_info=True)
    return PreparedAnalysis(measured=measured, markers=markers, key=key, loudness=measured_loudness)


def with_key(track, key):
    """`track` (a generic `Track`) with the detected `key` if it has none of its
    own. A key the track already carries (its tags, its source library) is
    never second-guessed."""
    if key is None or getattr(track, "key_wheel", None) is not None:
        return track
    return track.model_copy(update={"key": key.name, "key_wheel": key.wheel, "key_mode": key.mode})
