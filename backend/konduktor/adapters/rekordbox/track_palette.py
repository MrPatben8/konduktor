"""rekordbox's TRACK colours — the eight a track can be tagged with.

Stored as a 1-based id into the `djmdColor` table (master.db `djmdContent.ColorID`,
a string: "0" = none) and OneLibrary's `color` table (`content.color_id`, an
integer, 0 = none); the stick's legacy `export.pdb` track row has a `color_id`
byte with the same numbering. Both tables list the same eight rows in the same
order: 1 Pink, 2 Red, 3 Orange, 4 Yellow, 5 Green, 6 Aqua, 7 Blue, 8 Purple —
one per generic `TrackColor`, so the mapping is one to one.

Those are the memory-cue palette's names in its order, so the display RGB is
`memory_palette.COLOURS`' (sampled from rekordbox 7's own colour menu). Nothing
stores the RGB — `djmdColor.ColorCode` is NULL in a real library.
"""
from __future__ import annotations

from ...core.capabilities import ColorSwatch, TrackColor
from . import memory_palette

#: id - 1 -> the generic colour, in rekordbox's menu order.
_NAMES: list[TrackColor] = ["pink", "red", "orange", "yellow", "green", "aqua", "blue", "purple"]

#: What `capabilities.tracks.colors` offers.
SWATCHES: list[ColorSwatch] = [
    ColorSwatch(color=name, hex=memory_palette.SWATCHES[i], label=memory_palette.COLOURS[i][0])
    for i, name in enumerate(_NAMES)
]


def _id(color_id: int | str | None) -> int:
    try:
        return int(color_id) if color_id is not None else 0
    except (TypeError, ValueError):
        return 0


def color_for(color_id: int | str | None) -> TrackColor | None:
    """The generic colour for a stored id; None for 0, NULL or anything unknown."""
    n = _id(color_id)
    return _NAMES[n - 1] if 1 <= n <= len(_NAMES) else None


def hex_for(color_id: int | str | None) -> str | None:
    """rekordbox's display colour for a stored id; None when uncoloured."""
    n = _id(color_id)
    return SWATCHES[n - 1].hex if 1 <= n <= len(SWATCHES) else None


def id_for(color: TrackColor | str | None) -> int:
    """The id to store for a generic colour; 0 for none (or an unknown name)."""
    return _NAMES.index(color) + 1 if color in _NAMES else 0
