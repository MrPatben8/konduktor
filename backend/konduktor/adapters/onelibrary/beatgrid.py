"""A OneLibrary beatgrid -> the generic marker list.

**The beatgrid is not in the database.** `exportLibrary.db` has no grid table at
all — `content.bpmx100` is a single display tempo — and the real grid is the
`PQTZ` tag of the track's ANLZ `.DAT`, holding EVERY BEAT explicitly. That is the
same arrangement, and the same tag, as a desktop Rekordbox library: a drive is
exported from one, and rekordbox regenerates the analysis files rather than
inventing a new format for them.

Verified end to end on a track whose flexible grid Konduktor itself had written
into `master.db`: exporting it to a OneLibrary drive preserved both tempo
segments exactly (128.00 BPM to 60 s, then 90.00 BPM), so multi-tempo grids
survive the whole chain and the marker list is the right generic shape.

The collapse rule is shared with the Rekordbox adapter deliberately rather than
copied. Two implementations of "when is this a new marker?" would be free to
drift apart by a rounding tolerance, and a drifting grid is a silent corruption
rather than a visible bug. If a third AlphaTheta-format adapter ever appears,
this and `rekordbox.beatgrid` should be promoted to one shared ANLZ module; the
import direction here is the cheapest way to stay honest until then.
"""
from __future__ import annotations

from ...core.model import GridMarker
from ..rekordbox.beatgrid import BPM_EPSILON, markers_from_beats  # noqa: F401

__all__ = ["BPM_EPSILON", "markers_from_beats", "beats_from_pqtz", "markers_from_anlz"]


def beats_from_pqtz(anlz) -> tuple[list[float], list[float]] | None:
    """Per-beat ``(times_sec, bpms)`` from a parsed ANLZ file's `PQTZ` tag.

    Returns None when the file has no `PQTZ` — which is a real state, not an
    error: rekordbox analyses one-shot samples too, and gives them an analysis
    file with no beatgrid in it.

    `PQTZ` stores time in milliseconds and tempo in hundredths of a BPM.
    """
    for tag in getattr(anlz, "tags", []):
        if tag.type != "PQTZ":
            continue
        entries = getattr(getattr(tag.struct, "content", None), "entries", None)
        if not entries:
            return None
        times: list[float] = []
        bpms: list[float] = []
        for e in entries:
            try:
                times.append(int(e.time) / 1000.0)
                bpms.append(int(e.tempo) / 100.0)
            except (AttributeError, TypeError, ValueError):
                continue
        return (times, bpms) if times else None
    return None


def markers_from_anlz(anlz) -> list[GridMarker]:
    """The generic marker list for a parsed ANLZ file."""
    grid = beats_from_pqtz(anlz)
    if grid is None:
        return []
    return markers_from_beats(*grid)
