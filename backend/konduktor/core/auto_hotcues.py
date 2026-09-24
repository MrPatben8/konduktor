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
DUPLICATE = "duplicate"        # a lower slot is already getting a cue on this beat


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
    duplicate_of: int | None = None  # the slot that got this beat, for DUPLICATE


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
    non-editable one (a cue the platform manages itself) is never replaced, even
    when overwrite is ticked, because the adapter would refuse it anyway.

    Two slots that resolve to the SAME BEAT get one cue, in the lower slot: a
    second pad on the same beat is a wasted pad, and it happens by construction
    (Build 2 is Breakdown 1 when two drops are close). Slots are decided in slot
    order so "lower wins" holds whatever order the request lists them in, and
    the comparison is on the beat INDEX — exact, where comparing seconds would
    need a tolerance.
    """
    out: list[SlotOutcome] = []
    taken: dict[int, int] = {}  # beat index -> the slot placing a cue there
    for r in sorted(requests, key=lambda r: r.slot):
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
        index = beat + r.offset_beats
        t = structure.beat_time(index)
        if t < 0 or t >= structure.duration:
            out.append(SlotOutcome(r.slot, r.event, OUT_OF_RANGE))
            continue
        if index in taken:
            out.append(SlotOutcome(r.slot, r.event, DUPLICATE, duplicate_of=taken[index]))
            continue
        taken[index] = r.slot
        out.append(SlotOutcome(r.slot, r.event, PLACED, round(t, 4), cue_name(r.event, r.offset_beats)))
    return out
