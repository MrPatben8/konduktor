"""Traktor's TRACK colours — `INFO@COLOR`, an integer.

MEASURED in Traktor 4.5 (kit T1, 2026-10-17,
`.claude/discussions/discuss-track-colour-2026-10-17.md`) by colouring one track
per entry of the browser's Color menu, in its order: 1 Red · 2 Orange ·
3 Yellow · 4 Green · 5 Blue · 6 Violet · 7 Magenta. Traktor keeps it in the
collection only — it does not tag the audio file.

Mapped to the generic colours BY NAME (decided 2026-10-17): Violet is `purple`,
Magenta `pink`, and Blue is `blue` even though it LOOKS like rekordbox's Aqua —
the name is the DJ's category. Traktor has no seventh-and-a-half colour, so
`aqua` arriving from rekordbox is stored as Blue.

The RGB values are DISPLAY colours only — nothing stores them. They were
sampled from the colour dots in Traktor's own browser (a screenshot).
"""
from __future__ import annotations

from ...core.capabilities import ColorSwatch, TrackColor

#: What `capabilities.tracks.colors` offers; code - 1 -> swatch, menu order.
SWATCHES: list[ColorSwatch] = [
    ColorSwatch(color="red", hex="#FB413E", label="Red"),
    ColorSwatch(color="orange", hex="#F79525", label="Orange"),
    ColorSwatch(color="yellow", hex="#F9F918", label="Yellow"),
    ColorSwatch(color="green", hex="#52FA52", label="Green"),
    ColorSwatch(color="blue", hex="#1ACBFA", label="Blue"),
    ColorSwatch(color="purple", hex="#B26CFA", label="Violet"),
    ColorSwatch(color="pink", hex="#FA51EB", label="Magenta"),
]

#: Generic colours Traktor has no swatch of its own for.
_FOLDED: dict[str, int] = {"aqua": 5}


def _swatch(code: int | None) -> ColorSwatch | None:
    return SWATCHES[code - 1] if isinstance(code, int) and 1 <= code <= len(SWATCHES) else None


def color_for(code: int | None) -> TrackColor | None:
    """The generic colour for a stored code; None for absent, 0 or unknown."""
    s = _swatch(code)
    return s.color if s else None


def hex_for(code: int | None) -> str | None:
    """Traktor's display colour for a stored code; None when uncoloured."""
    s = _swatch(code)
    return s.hex if s else None


def code_for(color: TrackColor | str | None) -> int | None:
    """The `INFO@COLOR` to store for a generic colour; None for none."""
    if color is None:
        return None
    for i, s in enumerate(SWATCHES, start=1):
        if s.color == color:
            return i
    return _FOLDED.get(color)
