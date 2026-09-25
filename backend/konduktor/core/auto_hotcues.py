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

import numpy as np

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
DUPLICATE = "duplicate"        # another cue (kept, or new in a lower slot) is on this beat


@dataclass(frozen=True)
class SlotRequest:
    slot: int
    event: str
    offset_beats: int = 0
    overwrite: bool = False


@dataclass(frozen=True)
class ExistingCue:
    """A hotcue already in the bank, before this run."""

    editable: bool
    start: float  # seconds


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


def _beat_of(structure: Structure, t: float) -> int | None:
    """The grid beat a cue at ``t`` sits on, or None if it is between beats.

    A quarter-beat tolerance: a hand-placed cue is rarely sample-exact on the
    grid, and one a DJ put on the half-beat on purpose is a different beat.
    """
    b = structure.beats
    i = int(np.clip(np.searchsorted(b, t), 1, len(b) - 1))
    i = i if abs(b[i] - t) < abs(b[i - 1] - t) else i - 1
    step = b[min(i + 1, len(b) - 1)] - b[max(i - 1, 0)]
    step = step / 2 if step > 0 else 0.5
    return i if abs(b[i] - t) <= step / 4 else None


def plan(
    structure: Structure,
    requests: list[SlotRequest],
    existing: dict[int, ExistingCue],
) -> list[SlotOutcome]:
    """Decide every requested slot.

    ``existing`` is the bank before this run. A non-editable cue (a Traktor grid
    marker's companion) is never replaced, even when overwrite is ticked,
    because the adapter would refuse it anyway.

    **One cue per beat.** A slot whose position is on a beat that already has a
    cue — one this run is placing in a LOWER slot, or one that was in the bank
    before and stays there (the grid cue, a hand-placed cue) — is skipped as a
    duplicate of that slot: a second pad on the same beat is a wasted pad. New
    cues are decided in slot order, so "lower wins" holds whatever order the
    request lists them in.

    A cue being REPLACED frees its beat only if its replacement is actually
    placed; a replacement that is itself skipped leaves the old cue — and its
    claim on the beat — where they were. That is circular (whether slot 3's old
    cue survives decides slot 5, and slot 5 may be what slot 3 collides with),
    so the plan is recomputed until the set of surviving cues stops changing;
    each pass can only add survivors, so it ends within one pass per slot.
    """
    requested = {r.slot: r for r in requests}
    # Old cues whose replacement was not placed, found by the previous pass.
    surviving_overwritten: set[int] = set()
    while True:
        claims: dict[int, int] = {}  # beat index -> slot of the cue on it
        for slot, cue in sorted(existing.items()):
            replaced = slot in requested and requested[slot].overwrite and cue.editable
            if replaced and slot not in surviving_overwritten:
                continue
            beat = _beat_of(structure, cue.start)
            if beat is not None:
                claims.setdefault(beat, slot)
        out = _decide(structure, requests, existing, claims)
        now = {
            o.slot for o in out
            if o.slot in existing and requested[o.slot].overwrite
            and existing[o.slot].editable and o.status != PLACED
        }
        if now <= surviving_overwritten:
            return out
        surviving_overwritten |= now


def _decide(structure, requests, existing, claims) -> list[SlotOutcome]:
    out: list[SlotOutcome] = []
    taken = dict(claims)
    for r in sorted(requests, key=lambda r: r.slot):
        cue = existing.get(r.slot)
        if cue is not None and not cue.editable:
            out.append(SlotOutcome(r.slot, r.event, PROTECTED))
            continue
        if cue is not None and not r.overwrite:
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
