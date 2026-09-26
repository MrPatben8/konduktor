"""The beatgrid model: an ORDERED LIST of grid markers.

A Traktor beatgrid is not "a BPM and an anchor" — it is a list of `CUE_V2
TYPE="4"` markers, each carrying its own `<GRID BPM>` child. Marker *i*'s tempo
governs from its position until marker *i+1*. Traktor has stored grids this way
since 3.4 (flexible beatgrids); a constant-tempo track is simply a list of
length one, which is why this model is right for every track rather than a
special case for flexible ones.

`<TEMPO BPM>` mirrors the FIRST marker by START.

Verified against the 8485-entry reference collection:
  * markers per entry: {1: 7966, 0: 517, 2: 2}
  * TEMPO.bpm == markers[0].grid.bpm in 7968/7968 entries with both
  * CUE_V2 is ascending by START in 8485/8485 entries
  * 53 entries have a TEMPO but no marker  -> deleting a grid must KEEP TEMPO

Two rules that are easy to get wrong:

1. **Marker NAME is not a discriminator.** In the reference collection TYPE=4
   markers are named "AutoGrid" x7888, "n.n." x64, "Beat Marker" x14 and
   "Unnamed" x4. Identify a marker by having a `<GRID>` child, and order the
   list by START — never by name, never by document order.

2. **Companion cues are conventional, not structural.** Traktor usually writes a
   white TYPE=0 cue alongside each grid marker, at the same position, occupying
   a real hotcue slot. 7910 of 7970 markers have one; 60 do not, and one track
   has a companion on its first marker but not its second. So we follow them
   where they exist, keep them in sync, and NEVER invent one.

This module is pure and imports nothing else from the package, so it can be the
single shared definition used by the store, the API projection and the read
model — which previously each had their own and disagreed about BPM.
"""
from __future__ import annotations

from traktor_nml_utils.models.collection import CueV2Type, Entrytype

# How close a cue must sit to a marker to be considered its companion. Traktor
# writes them at the identical START; 71 markers in the reference collection are
# a fraction of a millisecond off, which this absorbs.
COMPANION_EPS_MS = 1.0

# Companions are always white. This test is LOAD-BEARING: 49 uncoloured cues and
# 18 LOOPS also sit within 1 ms of a marker in the reference collection. Drop the
# colour check and the marker commands would start dragging and deleting the
# user's own loops.
COMPANION_COLOR = "#FFFFFF"


def grid_markers(entry: Entrytype) -> list[CueV2Type]:
    """Every grid marker on `entry`, ordered by START.

    Sorting is not cosmetic: the first marker by START is the one `<TEMPO BPM>`
    mirrors, and marker indices are the identity used by the edit commands.
    """
    markers = [c for c in (entry.cue_v2 or []) if getattr(c, "grid", None) is not None]
    markers.sort(key=lambda c: c.start or 0.0)
    return markers


def first_marker(entry: Entrytype) -> CueV2Type | None:
    """The marker `<TEMPO BPM>` mirrors, or None when the track has no grid."""
    markers = grid_markers(entry)
    return markers[0] if markers else None


def effective_bpm(entry: Entrytype) -> float | None:
    """The tempo the grid actually starts at.

    Prefers the first marker's `<GRID BPM>` and falls back to `<TEMPO BPM>` so
    the 53 gridless-but-tempo'd entries still report a BPM. Routing both the
    library table and the prep deck through this makes them structurally
    incapable of disagreeing.
    """
    marker = first_marker(entry)
    if marker is not None and marker.grid is not None and marker.grid.bpm:
        return marker.grid.bpm
    return entry.tempo.bpm if entry.tempo else None


def companions(entry: Entrytype) -> dict[int, CueV2Type]:
    """Map marker index -> the companion cue paired with it, where one exists.

    Greedy nearest-unclaimed matching, so two markers close together can never
    both claim the same cue.
    """
    markers = grid_markers(entry)
    if not markers:
        return {}
    pool = [
        c
        for c in (entry.cue_v2 or [])
        if getattr(c, "grid", None) is None
        and c.type == 0
        and c.color == COMPANION_COLOR
        and c.hotcue is not None
        and c.hotcue >= 0
    ]
    out: dict[int, CueV2Type] = {}
    claimed: set[int] = set()
    for i, m in enumerate(markers):
        m_start = m.start or 0.0
        best = min(
            (c for c in pool if id(c) not in claimed),
            key=lambda c: abs((c.start or 0.0) - m_start),
            default=None,
        )
        if best is not None and abs((best.start or 0.0) - m_start) <= COMPANION_EPS_MS:
            out[i] = best
            claimed.add(id(best))
    return out


def is_companion(entry: Entrytype, cue: CueV2Type) -> bool:
    """True when `cue` is some grid marker's companion on this entry."""
    return any(c is cue for c in companions(entry).values())
