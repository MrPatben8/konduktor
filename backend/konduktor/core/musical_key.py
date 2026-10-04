"""Musical keys: the generic form and the notations DJ software writes.

The generic model carries a key as a Camelot wheel position (1-12) plus a mode
(`Track.key_wheel` / `key_mode`); one step round the wheel is a fifth, and the
minor key on a number is the relative of the major one. Each platform STORES a
key in its own notation, and a library also holds whatever another tool put
there — a rekordbox stick's key table carries "12A" beside "Abm" when tracks
arrived with Camelot tags. So parsing accepts every notation, and rendering is
always explicit about which one it writes.

Notations:
  - "camelot":  8A / 8B (Mixed In Key's wheel; A = minor)
  - "open_key": 1m / 1d (Traktor's default; Open Key 1 = Camelot 8)
  - "musical":  Am / C, with rekordbox's spelling: flats for Db Eb Ab Bb, and
    F# — measured from the key rows rekordbox 7 itself created (Abm, Dbm, Ebm,
    Eb; the six-sharp key was not among them).
"""
from __future__ import annotations

import re
from typing import Literal

Mode = Literal["major", "minor"]
Notation = Literal["camelot", "open_key", "musical"]

_CAMELOT = re.compile(r"^(\d{1,2})\s*([ab])$", re.I)
_OPEN_KEY = re.compile(r"^(\d{1,2})\s*([md])$", re.I)
_MUSICAL = re.compile(r"^([A-G])\s*([#b♯♭]?)\s*(m|min|minor|maj|major)?$", re.I)

_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SPELLING = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")


def wheel_of(pitch_class: int, mode: Mode) -> int:
    """Camelot position of a tonic: C major = 8B, A minor = 8A."""
    return (((7 * pitch_class) % 12 + (7 if mode == "major" else 4)) % 12) + 1


def pitch_class_of(wheel: int, mode: Mode) -> int:
    """The tonic (0 = C) at a Camelot position — `wheel_of`'s inverse."""
    return (7 * ((wheel - 1) - (7 if mode == "major" else 4))) % 12


def parse(text: str | None) -> tuple[int | None, Mode | None]:
    """(wheel, mode) for a key in any notation; (None, None) if unreadable.

    A bare number-and-letter is ambiguous only in principle: Camelot uses A/B
    and Open Key m/d, so the letter says which."""
    if not text:
        return None, None
    s = text.strip()
    if m := _CAMELOT.match(s):
        n = int(m.group(1))
        if 1 <= n <= 12:
            return n, "minor" if m.group(2).lower() == "a" else "major"
        return None, None
    if m := _OPEN_KEY.match(s):
        n = int(m.group(1))
        if 1 <= n <= 12:
            return ((n + 6) % 12) + 1, "minor" if m.group(2).lower() == "m" else "major"
        return None, None
    if m := _MUSICAL.match(s):
        letter, accidental, quality = m.group(1).upper(), m.group(2), (m.group(3) or "")
        pc = _PITCH[letter] + {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(accidental, 0)
        # "m" alone is minor; "maj"/"major" major; "min"/"minor" minor.
        q = quality.lower()
        mode: Mode = "minor" if q in ("m", "min", "minor") else "major"
        return wheel_of(pc % 12, mode), mode
    return None, None


def render(wheel: int | None, mode: str | None, notation: Notation) -> str | None:
    """A key in `notation`, or None when the position is unknown — a made-up
    key is worse than a blank one."""
    if not wheel or not (1 <= wheel <= 12) or mode not in ("major", "minor"):
        return None
    if notation == "camelot":
        return f"{wheel}{'A' if mode == 'minor' else 'B'}"
    if notation == "open_key":
        return f"{((wheel + 4) % 12) + 1}{'m' if mode == 'minor' else 'd'}"
    return SPELLING[pitch_class_of(wheel, mode)] + ("m" if mode == "minor" else "")


def notation_of(text: str | None) -> Notation | None:
    """Which notation a stored key string is written in."""
    if not text:
        return None
    s = text.strip()
    if _CAMELOT.match(s):
        return "camelot"
    if _OPEN_KEY.match(s):
        return "open_key"
    if _MUSICAL.match(s):
        return "musical"
    return None
