"""Rekordbox's ``djmdCue.Kind`` encoding <-> the generic cue vocabulary.

The only place these integers are given meaning. Verified against a real
library (see the handoff's Findings section):

  * ``Kind == 0``  -> a MEMORY cue: no bank slot, and Rekordbox allows any
    number of them.
  * ``Kind >= 1``  -> a HOT CUE.

**``Kind`` is 1-based AND it skips 4.** Konduktor's generic model numbers bank
slots from zero — slot 0 is the pad the UI labels "A", which is also how Traktor
stores its ``HOTCUE`` attribute. Rekordbox's first pad is ``Kind=1``, and the
value ``4`` is reserved: the bank runs 1, 2, 3, 5, 6, 7, 8, 9.

Measured, not guessed. A loop was written at every ``Kind`` from 1 to 8, ten
seconds apart, and read off a real deck:

    Kind  1  2  3  4  5  6  7  8  9
    pad   A  B  C  -  D  E  F  G  H

``Kind=4`` appears on no pad at all; Rekordbox shows such a cue as a memory cue.
That one gap explains two earlier puzzles: a Rekordbox-authored loop on pad F
stored ``Kind=7``, and a Konduktor cue written as ``Kind=4`` vanished from the
bank. Getting this wrong silently moves cues to the wrong pad — or loses them
from the bank entirely.

**``Kind`` is treated as an opaque slot id.** Setting cues on pads A and C
stored 1 and 3, but the pad in the 6th position stored 7, so the slot -> letter
mapping is NOT a plain index and has not been pinned down. Nothing here depends
on it: the adapter addresses cues by slot number and the UI labels them, which
is what ``capabilities.cues.slot_labels`` is for.

Loop-ness is NOT carried by ``Kind``. A loop is an ordinary cue row that also
has an out-point, so a loop can sit in a hot cue slot or be a memory cue.
"""
from __future__ import annotations

MEMORY_KIND = 0

# Rekordbox has no fade-in/fade-out/load cues; those are Traktor-only.
CUE_TYPES = ["cue", "loop"]

# What Konduktor will write. Loops were disabled for a while because a loop
# written at `Kind=4` disappeared from the bank — which turned out to be the
# reserved-Kind gap above, not anything to do with loops. Both types write.
WRITABLE_CUE_TYPES = ["cue", "loop"]


# The Kind value that is not a bank slot. Everything at or above it is shifted
# by one relative to the pad index.
RESERVED_KIND = 4


def role_and_slot(kind: int | None) -> tuple[str, int | None]:
    """(role, 0-based slot) for a native ``Kind``.

    A cue at `RESERVED_KIND` occupies no pad, so it is projected as a memory cue
    — which is how Rekordbox itself displays one.
    """
    if kind is None or kind == MEMORY_KIND or int(kind) == RESERVED_KIND:
        return "memory", None
    kind = int(kind)
    return "hotcue", kind - 1 if kind < RESERVED_KIND else kind - 2


def kind_for(role: str, slot: int | None) -> int:
    """The native ``Kind`` for a generic (role, 0-based slot).

    Inverse of `role_and_slot`; skips `RESERVED_KIND`.
    """
    if role == "memory":
        return MEMORY_KIND
    if slot is None:
        raise ValueError("a hot cue needs a slot")
    slot = int(slot)
    if slot < 0:
        raise ValueError(f"a hot cue slot cannot be negative, got {slot}")
    return slot + 1 if slot < RESERVED_KIND - 1 else slot + 2


def cue_type(out_msec: int | None) -> str:
    """A cue with a real out-point is a loop; everything else is a plain cue.

    Rekordbox writes ``OutMsec = -1`` for a non-loop.
    """
    return "loop" if out_msec is not None and out_msec > 0 else "cue"


def beat_loop_size(beats: int) -> int:
    """Encode a loop's musical length.

    ``BeatLoopSize = (beats << 16) | 1`` — verified against two real loops at
    different tempos (1 beat @125 BPM = 0x10001, 4 beats @120 BPM = 0x40001),
    each agreeing with the measured millisecond span.
    """
    return (int(beats) << 16) | 1


def beats_in_loop(beat_loop_size: int | None) -> int | None:
    """Decode `beat_loop_size`. None when unset."""
    if not beat_loop_size:
        return None
    return beat_loop_size >> 16
