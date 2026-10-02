"""The analysis a platform that ANALYSES what it adds needs for one new track.

Rekordbox draws a new track's waveform and grid from its ANLZ files, so adding a
track means decoding it once: the waveform measured on the platform's own clock
(`lead`, read from the file's header by that platform) plus a detected grid when
the track brings none. One definition, used both by the adapter (a library on
this computer) and by the client of a library held by a server — which never
decodes audio, so the client measures its own copy and sends the result
(`PreparedAnalysis`) with the track.
"""
from __future__ import annotations

from pathlib import Path

from .adapter import PreparedAnalysis
from .model import GridMarker


def prepare(path: Path, *, lead: float, detect: bool = True) -> PreparedAnalysis:
    """Decode `path` once: its waveform at `lead`, and (with `detect`) a grid."""
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
    return PreparedAnalysis(measured=measured, markers=markers)
