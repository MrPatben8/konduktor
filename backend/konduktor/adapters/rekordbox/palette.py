"""rekordbox's hot-cue colour palette: a generic `#RRGGBB` -> (palette code, RGB).

**rekordbox draws a hot cue from its palette CODE, not from its RGB bytes.** An
export that wrote only RGB (code 0) had every cue drawn in rekordbox's defaults
— green cues, orange loops. The code is `PCP2`'s colour byte (offset 44, before
the RGB triple) in the ANLZ `.EXT`, and `djmdCue.ColorTableIndex` in `master.db`.

The table was MEASURED, not recalled: 16 hot cues coloured through rekordbox 7's
own palette, one colour per pad, exported to a stick, and read back. Rekordbox
writes the palette's exact RGB beside each code.

  * One palette colour went unmeasured — the teal-green between 0x0E and 0x16
    (probably code 0x12); a cue that should be it maps to its nearest neighbour.
  * 0x2B (#FF0017) was observed on a real export though it is not one of the 16
    swatches; it is kept so such a colour survives a round trip exactly.
  * **The palette has no white or grey.** A colourless (white/grey) cue — above
    all Traktor's white grid-marker companion — gets code 0 with its RGB. rekordbox
    produces that state itself (the reference fixture's cues are code 0 + RGB);
    what rekordbox DRAWS for it is not yet verified.
"""
from __future__ import annotations

import colorsys

#: code -> RGB, exactly as rekordbox 7 writes them.
PALETTE: dict[int, tuple[int, int, int]] = {
    0x31: (0xFF, 0x00, 0xA1),  # magenta
    0x38: (0xB3, 0x00, 0xFF),  # purple
    0x3C: (0x4D, 0x00, 0xFF),  # violet
    0x3E: (0x1A, 0x00, 0xFF),  # blue-violet
    0x01: (0x00, 0x00, 0xFF),  # blue
    0x05: (0x00, 0x70, 0xFF),  # light blue
    0x09: (0x00, 0xE0, 0xFF),  # cyan
    0x0E: (0x00, 0xFF, 0xA3),  # teal
    0x16: (0x1A, 0xFF, 0x00),  # green
    0x1A: (0x80, 0xFF, 0x00),  # lime
    0x1E: (0xE6, 0xFF, 0x00),  # yellow-lime
    0x20: (0xFF, 0xE8, 0x00),  # yellow
    0x26: (0xFF, 0x5E, 0x00),  # orange
    0x2A: (0xFF, 0x00, 0x00),  # red
    0x2B: (0xFF, 0x00, 0x17),  # red (observed, not a swatch)
    0x2D: (0xFF, 0x00, 0x45),  # pink-red
}

#: Konduktor's TYPE colours (`core.cue_colors`) -> the swatch a DJ would call by
#: the same name. Nearest-hue alone sends the mint loop green to TEAL, and a
#: Traktor user reads a loop as green; so the type colours are pinned by intent.
_BY_INTENT: dict[str, int] = {
    "#4D94FF": 0x05,  # cue     -> light blue
    "#3DDC84": 0x16,  # loop    -> green
    "#FF9A3D": 0x26,  # fade    -> orange
    "#FFD23D": 0x20,  # load    -> yellow
}

#: Below this saturation a colour is white/grey, which the palette cannot show.
_GREY = 0.2


def _parse(color: str) -> tuple[int, int, int] | None:
    c = (color or "").strip().lstrip("#")
    if len(c) != 6:
        return None
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def code_for(color: str | None) -> tuple[int, tuple[int, int, int]]:
    """(palette code, RGB to store) for a generic `#RRGGBB`; (0, black) for none.

    An exact palette RGB keeps its code; a type colour maps by intent; a
    white/grey keeps its RGB with code 0; anything else takes the swatch of the
    nearest hue, and stores THAT swatch's RGB so code and colour agree.
    """
    rgb = _parse(color) if color else None
    if rgb is None:
        return 0, (0, 0, 0)
    for code, value in PALETTE.items():
        if value == rgb:
            return code, value
    intent = _BY_INTENT.get(f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}")
    if intent is not None:
        return intent, PALETTE[intent]
    h, _l, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    if s < _GREY or max(rgb) - min(rgb) < 40:
        return 0, rgb
    def hue_gap(code: int) -> float:
        ph = colorsys.rgb_to_hls(*(v / 255 for v in PALETTE[code]))[0]
        d = abs(ph - h)
        return min(d, 1 - d)
    best = min((c for c in PALETTE if c != 0x2B), key=hue_gap)
    return best, PALETTE[best]


#: The swatches a user can pick, in rekordbox's own order — what
#: `capabilities.cues.palette` offers. 15 of rekordbox's 16: the unmeasured
#: teal-green is left out rather than offered with a guessed RGB, and 0x2B is
#: not a swatch at all (see the module note).
SWATCHES: list[int] = [code for code in PALETTE if code != 0x2B]


def hex_for(code: int | None) -> str | None:
    """`#RRGGBB` for a stored palette code; None for uncoloured or unknown.

    An unknown code (the unmeasured teal-green) projects as no colour rather
    than a guessed one; it is still kept on the row, since editing a cue's
    position, type or name never rewrites its colour.
    """
    rgb = PALETTE.get(code) if code else None
    return None if rgb is None else f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def swatch_code(color: str) -> int | None:
    """The code of an EXACT palette colour, else None — for a user's pick, which
    must be a swatch, unlike `code_for`'s nearest-hue mapping for exports."""
    rgb = _parse(color)
    return next((code for code, value in PALETTE.items() if value == rgb), None)
