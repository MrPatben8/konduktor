"""Rekordbox's ``djmdCue.Kind`` encoding <-> the generic cue vocabulary.

The only place these integers are given meaning. Verified against a real
library (see the handoff's Findings section):

  * ``Kind == 0``  -> a MEMORY cue: no bank slot, and Rekordbox allows any
    number of them.
  * ``Kind >= 1``  -> a HOT CUE occupying bank slot ``Kind``.

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


def role_and_slot(kind: int | None) -> tuple[str, int | None]:
    """(role, slot) for a native ``Kind``."""
    if kind is None or kind == MEMORY_KIND:
        return "memory", None
    return "hotcue", int(kind)


def kind_for(role: str, slot: int | None) -> int:
    """The native ``Kind`` for a generic (role, slot). Inverse of `role_and_slot`."""
    if role == "memory":
        return MEMORY_KIND
    if slot is None:
        raise ValueError("a hot cue needs a slot")
    return int(slot)


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
