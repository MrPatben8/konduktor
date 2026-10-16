"""rekordbox's MEMORY-cue colours — a palette of its own, not the hot cues'.

rekordbox 7 offers a memory cue eight colours plus "No Color" (Pink, Red,
Orange, Yellow, Green, Aqua, Blue, Purple — the same eight names as its track
colours), against sixteen swatches for a hot cue. MEASURED 2026-10-16 (kits M1 /
M2 in `.claude/discussions/discuss-memory-cues-2026-10-16.md`), by colouring
memory cues one per colour, in that order, in rekordbox itself:

  * `master.db`: the code is `djmdCue.Color`, **0-based** in menu order (Pink 0
    … Purple 7) and `ColorTableIndex` stays NULL — the opposite of a hot cue,
    which keeps its code in `ColorTableIndex` with `Color = -1`. "No colour" is
    `-1` on a cue never coloured and **255** on one set back to "No Color".
  * the ANLZ `.EXT`: `PCP2` byte 28, **1-based** (Blue = 7), 0 = none. The
    hot-cue colour tail after the comment stays zero on a memory cue.

The RGB values are DISPLAY colours only — nothing stores them. They were
sampled from the dots in rekordbox 7's own colour menu (a screenshot), so they
are what rekordbox shows, not what it writes.
"""
from __future__ import annotations

import colorsys

from .palette import _parse

#: code (0-based, master.db) -> display RGB, in rekordbox's menu order.
COLOURS: list[tuple[str, tuple[int, int, int]]] = [
    ("Pink", (0xE5, 0x77, 0xEF)),
    ("Red", (0xE1, 0x31, 0x21)),
    ("Orange", (0xEA, 0xA3, 0x49)),
    ("Yellow", (0xF2, 0xE2, 0x57)),
    ("Green", (0x65, 0xDB, 0x41)),
    ("Aqua", (0x55, 0xBB, 0xF1)),
    ("Blue", (0x1F, 0x4E, 0xED)),
    ("Purple", (0x8A, 0x1D, 0xED)),
]

#: `master.db`'s `Color` for a memory cue set back to "No Color" (measured);
#: a cue never coloured keeps -1. Both read as uncoloured.
CLEARED = 255

#: Below this saturation a colour is white/grey, which the palette cannot show.
_GREY = 0.2


def _hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


#: The swatches a user can pick — what `capabilities.cues.memory_palette` offers.
SWATCHES: list[str] = [_hex(rgb) for _name, rgb in COLOURS]


def hex_for(code: int | None) -> str | None:
    """`#RRGGBB` for a stored 0-based code; None for -1, 255 or anything unknown."""
    if code is None or not 0 <= code < len(COLOURS):
        return None
    return _hex(COLOURS[code][1])


def code_for(color: str | None) -> int | None:
    """The 0-based code of an EXACT swatch (a user's pick), else None."""
    rgb = _parse(color) if color else None
    return next((i for i, (_n, value) in enumerate(COLOURS) if value == rgb), None)


def nearest_code(color: str | None) -> int | None:
    """The swatch nearest in hue to any `#RRGGBB` — for a colour arriving from
    another platform (an import, an export). None for no colour or a grey."""
    exact = code_for(color)
    if exact is not None:
        return exact
    rgb = _parse(color) if color else None
    if rgb is None:
        return None
    h, _l, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    if s < _GREY or max(rgb) - min(rgb) < 40:
        return None

    def hue_gap(i: int) -> float:
        ph = colorsys.rgb_to_hls(*(v / 255 for v in COLOURS[i][1]))[0]
        d = abs(ph - h)
        return min(d, 1 - d)

    return min(range(len(COLOURS)), key=hue_gap)


def anlz_byte(code: int | None) -> int:
    """`PCP2` byte 28 for a 0-based code: code + 1, and 0 for none."""
    return 0 if code is None else code + 1


def from_anlz_byte(value: int) -> int | None:
    """The 0-based code from `PCP2` byte 28 (0 = none)."""
    return value - 1 if 1 <= value <= len(COLOURS) else None
