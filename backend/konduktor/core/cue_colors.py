"""The colour a cue is SHOWN in — the one backend definition.

A stored colour wins; otherwise the cue's type decides. That is Traktor's own
convention (it stores a colour only on a grid marker's white companion cue and
draws every other cue from its type) and it is exactly what Konduktor's deck and
table draw. **Keep in lockstep with `TYPE_COLORS` in `frontend/src/lib/cues.ts`.**

Why the backend needs it: an export to a platform that stores a colour per cue
(rekordbox) must write what the user SEES. Writing "no colour" hands the choice
to the target, which draws its own defaults — rekordbox showed green cues and
orange loops where Konduktor and Traktor show blue and green.
"""
from __future__ import annotations

from .model import CuePoint

TYPE_COLORS = {
    "cue": "#4D94FF",       # blue
    "fade_in": "#FF9A3D",   # orange
    "fade_out": "#FF9A3D",  # orange
    "load": "#FFD23D",      # yellow
    "loop": "#3DDC84",      # green
}
MEMORY_COLOR = "#9AA1B2"    # grey


def effective_color(cue: CuePoint) -> str:
    """`#RRGGBB` — the stored colour, or the type's."""
    if cue.color:
        return cue.color.upper()
    if cue.role == "memory":
        return MEMORY_COLOR
    return TYPE_COLORS.get(cue.type, MEMORY_COLOR)
