"""OneLibrary cues: read from the ANLZ analysis files, not the database.

**The `cue` table is not where a drive's cues are.** It exists in the schema —
and has columns for everything a cue needs — but rekordbox exports an empty one
and puts the cues in the track's ANLZ file instead. Measured on a real rekordbox
7 export of a track carrying four hot cues and a looped memory cue: the `cue`
table held zero rows and every cue was present in the analysis files. The table
appears to be for hardware writing edits BACK to the drive, which is a different
direction than this adapter reads.

Two tags carry cues, and they are not equivalent:

  * **`PCOB`** is the original list, and it is SPLIT BY PAD. The `.DAT` holds
    only hot cues 1-3 — the three pads the earliest CDJs had — and the `.EXT`
    holds 4 and up. Reading only the `.DAT` silently loses pads D-H.
  * **`PCO2`** is the extended list in the `.EXT`, and it holds the COMPLETE set
    in one tag plus three things `PCOB` has nowhere to put: an RGB colour, a
    comment, and the loop length expressed in BEATS.

So `PCO2` is preferred and `PCOB` is the fallback for a drive old enough not to
have one. Both are merged across files before that choice is made, because "the
complete list" must not depend on which file happened to be read.

**Slot numbering differs from `master.db` and this is the trap.** In the desktop
database a hot cue's `djmdCue.Kind` is a sparse bank that SKIPS 4 (`1,2,3,5,…`
for pads A-H). In ANLZ, `hot_cue` is a plain dense 1-based index (1=A, 2=B, …),
and `0` means the cue is a memory cue rather than a pad. The two were aligned
against each other on one track: `Kind` 1/2/3/5 at 40.000 s, 18.773 s, 90.667 s
and 120.000 s came out as `hot_cue` 1/2/3/4 at exactly the same times. Carry the
Rekordbox adapter's mapping over here unchanged and every cue from pad D onward
lands one pad too far along.

Times are MILLISECONDS here, where the desktop database counts frames at 150 fps.
"""
from __future__ import annotations

import logging

from ...core.model import CuePoint

log = logging.getLogger(__name__)

# `loop_time` when a cue is not a loop. Stored as an unsigned 32-bit -1 rather
# than 0, so a plain falsiness test would read it as "a loop ending at zero".
NO_LOOP = 0xFFFFFFFF

# `hot_cue` for a cue that sits in no pad bank, i.e. a memory cue.
MEMORY_SLOT = 0

# PCO2/PCOB entry kinds. The value is the same in both tags; PCOB exposes it as
# a construct enum and PCO2 as a plain int, so both are normalised to an int.
ENTRY_CUE = 1
ENTRY_LOOP = 2


def _entry_kind(value) -> int:
    """PCOB gives an enum, PCO2 an int — one number out of either."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return ENTRY_CUE


def _color(entry) -> str | None:
    """A cue's colour as ``#RRGGBB``, when the tag carries one.

    Only `PCO2` stores RGB. A cue with no colour set stores 0,0,0 — which is a
    real black in principle, but rekordbox uses it as "unset" and shows such a
    cue in the default colour, so it is reported as no colour rather than as
    black. Memory cues in the reference export are exactly this case.
    """
    try:
        r, g, b = int(entry.color_red), int(entry.color_green), int(entry.color_blue)
    except (AttributeError, TypeError, ValueError):
        return None
    if (r, g, b) == (0, 0, 0):
        return None
    return f"#{r:02X}{g:02X}{b:02X}"


def _to_cue(entry, *, with_color: bool) -> CuePoint | None:
    """One ANLZ cue entry as a generic `CuePoint`, or None if unreadable."""
    try:
        hot_cue = int(entry.hot_cue)
        start_ms = int(entry.time)
    except (AttributeError, TypeError, ValueError):
        return None

    loop_ms = getattr(entry, "loop_time", NO_LOOP)
    try:
        loop_ms = int(loop_ms)
    except (TypeError, ValueError):
        loop_ms = NO_LOOP

    is_loop = _entry_kind(getattr(entry, "type", ENTRY_CUE)) == ENTRY_LOOP
    length = 0.0
    if is_loop and loop_ms != NO_LOOP and loop_ms > start_ms:
        length = (loop_ms - start_ms) / 1000.0

    # hot_cue 0 is the memory list; 1..N are the pads, dense and 1-based, so the
    # generic 0-based slot is simply one less.
    role = "memory" if hot_cue == MEMORY_SLOT else "hotcue"
    slot = None if role == "memory" else hot_cue - 1

    comment = getattr(entry, "comment", None)
    return CuePoint(
        name=(str(comment) or None) if comment else None,
        type="loop" if length > 0 else "cue",
        role=role,
        start=start_ms / 1000.0,
        length=length,
        slot=slot,
        color=_color(entry) if with_color else None,
        # A OneLibrary drive is read-only in Konduktor, so no cue accepts a
        # command. `capabilities.writable` is what the UI actually gates on;
        # this keeps the per-cue answer consistent with it.
        editable=False,
        readonly_reason="platform_managed",
        grid_marker=None,
    )


def _entries(anlz, tag_type: str) -> list:
    """Every entry of every `tag_type` tag in one parsed ANLZ file.

    A file carries the tag twice — once for hot cues and once for memory cues —
    so both are collected and the entry's own `hot_cue` decides which it is.
    That is more robust than trusting the container's type field, which is
    absent on some tags in real files.
    """
    out: list = []
    for tag in getattr(anlz, "tags", []):
        if tag.type != tag_type:
            continue
        content = getattr(tag.struct, "content", None)
        out.extend(getattr(content, "entries", None) or [])
    return out


def cues_from_anlz(dat, ext) -> list[CuePoint]:
    """The track's complete cue list, from its parsed `.DAT` and `.EXT`.

    Either may be None — a drive can carry a `.DAT` with no `.EXT` — and a track
    with no cues at all yields an empty list rather than an error.
    """
    files = [f for f in (dat, ext) if f is not None]
    if not files:
        return []

    # PCO2 first: it is the complete list and the only one with colour, comment
    # and beat-denominated loops. It lives only in the .EXT.
    pco2: list = []
    for f in files:
        pco2.extend(_entries(f, "PCO2"))
    if pco2:
        cues = [c for e in pco2 if (c := _to_cue(e, with_color=True)) is not None]
    else:
        # Fallback: merge PCOB across BOTH files. The .DAT holds pads 1-3 and the
        # .EXT the rest, so either alone is an incomplete bank.
        pcob: list = []
        for f in files:
            pcob.extend(_entries(f, "PCOB"))
        cues = [c for e in pcob if (c := _to_cue(e, with_color=False)) is not None]

    return _dedupe(cues)


def _dedupe(cues: list[CuePoint]) -> list[CuePoint]:
    """Collapse the same cue appearing in more than one tag or file.

    Identity is (role, slot, start) rather than the whole cue: the same pad can
    be described by both `PCOB` and `PCO2`, and by both files, and those copies
    agree on position while differing in how much detail they carry. The first
    one wins, which — given PCO2 is read first — is the richest.

    Ordered by position, then by slot, so the projection is stable across reads
    regardless of the order the tags happened to store.
    """
    seen: set[tuple[str, int | None, int]] = set()
    out: list[CuePoint] = []
    for c in cues:
        key = (c.role, c.slot, round(c.start * 1000))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    out.sort(key=lambda c: (c.start, c.slot if c.slot is not None else -1))
    return out
