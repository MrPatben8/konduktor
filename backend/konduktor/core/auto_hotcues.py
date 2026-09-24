"""Auto Hotcues: resolve a slot template against a track's structure.

The user binds each hotcue slot to an EVENT ("first drop", "start of the second
breakdown", …) plus an offset in beats ("16 beats before the first drop"). The
events come from `core/structure.py`; this module is the pure part — which
slots get a cue, where, called what, and why a slot got nothing — so it is
testable without audio.

Offsets are counted along the grid's own beats, not in seconds, so a -16 on a
flexible grid lands 16 BEATS earlier even across a tempo change.

Each slot reports an outcome rather than silently dropping, because a template
names specific events and "no second drop in this track" is an answer the user
needs, not a cue that quietly failed to appear.
"""
from __future__ import annotations

from dataclasses import dataclass

from .structure import EVENTS, Structure

LABELS: dict[str, str] = {
    "first_beat": "First Beat",
    "intro_end": "Intro End",
    "build_1": "Build 1",
    "drop_1": "Drop 1",
    "breakdown_1": "Breakdown 1",
    "build_2": "Build 2",
    "drop_2": "Drop 2",
    "breakdown_2": "Breakdown 2",
    "build_3": "Build 3",
    "drop_3": "Drop 3",
    "breakdown_3": "Breakdown 3",
    "outro": "Outro",
    "last_beat": "Last Beat",
}
assert set(LABELS) == set(EVENTS)

# Outcomes a slot can report.
PLACED = "placed"
NOT_FOUND = "not_found"        # the track has no such event (e.g. no third drop)
OUT_OF_RANGE = "out_of_range"  # the offset moves it before the start / past the end
OCCUPIED = "occupied"          # the slot holds a cue and overwrite was not asked for
PROTECTED = "protected"        # the slot holds a cue the adapter will not replace


@dataclass(frozen=True)
class SlotRequest:
    slot: int
    event: str
    offset_beats: int = 0
    overwrite: bool = False


@dataclass(frozen=True)
class SlotOutcome:
    slot: int
    event: str
    status: str
    start: float | None = None
    name: str | None = None


def cue_name(event: str, offset_beats: int) -> str:
    """The label a placed cue carries, e.g. ``Drop 1`` or ``Drop 1 -16``."""
    label = LABELS[event]
    return f"{label} {offset_beats:+d}" if offset_beats else label


def plan(
    structure: Structure,
    requests: list[SlotRequest],
    existing: dict[int, bool],
) -> list[SlotOutcome]:
    """Decide every requested slot.

    ``existing`` maps each occupied slot to whether its cue is EDITABLE — a
    non-editable one (a Traktor grid marker's companion) is never replaced, even
    when overwrite is ticked, because the adapter would refuse it anyway.
    """
    out: list[SlotOutcome] = []
    for r in requests:
        if r.slot in existing and not existing[r.slot]:
            out.append(SlotOutcome(r.slot, r.event, PROTECTED))
            continue
        if r.slot in existing and not r.overwrite:
            out.append(SlotOutcome(r.slot, r.event, OCCUPIED))
            continue
        beat = structure.events.get(r.event)
        if beat is None:
            out.append(SlotOutcome(r.slot, r.event, NOT_FOUND))
            continue
        t = structure.beat_time(beat + r.offset_beats)
        if t < 0 or t >= structure.duration:
            out.append(SlotOutcome(r.slot, r.event, OUT_OF_RANGE))
            continue
        out.append(SlotOutcome(r.slot, r.event, PLACED, round(t, 4), cue_name(r.event, r.offset_beats)))
    return out
