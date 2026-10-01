"""Reading an ANLZ analysis file at the TAG level, by hand.

The writer half is `anlz_writer`; this is its reader. Like the writer it lives
here because the format is Pioneer's, not one product's.

**Why not `pyrekordbox`'s parser.** It decodes a cue entry with a FIXED layout,
and rekordbox does not write one. An exported entry is 88 bytes, but when
rekordbox 7 EDITS a stick it writes compact ones (measured on Goober,
2026-10-01): a hot cue added in rekordbox was a 48-byte `PCP2` (colour, no
padding), and a memory cue a 44-byte one (no colour bytes at all). pyrekordbox
raises `PaddingError` on the 44-byte form, the whole `.EXT` fails to parse, and
a reader that falls back to the `.DAT` silently loses pads D-H and every colour.
So every entry here is read at its own `len_entry`, and a field the entry is too
short to hold is simply absent.

A file is also split into its RAW tags, kept byte for byte. Editing a stick
replaces one or two tags of a file rekordbox wrote; everything else (the
waveforms, `PVBR`, tags nobody has decoded) must go back exactly as it was, and
re-serialising through a parser is how bytes get lost.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

#: `PMAI` · len_header · len_file · …
FILE_MAGIC = b"PMAI"
NO_LOOP = 0xFFFFFFFF

#: The cue-list kinds in `PCOB`/`PCO2`'s own header.
LIST_MEMORY = 0
LIST_HOT = 1


@dataclass
class Tag:
    """One tag, raw. `data` is the WHOLE tag: magic, both lengths, and body."""

    type: str
    data: bytes
    _entries: list | None = field(default=None, repr=False, compare=False)

    @property
    def len_header(self) -> int:
        return struct.unpack_from(">I", self.data, 4)[0]

    @property
    def list_kind(self) -> int | None:
        """`PCOB`/`PCO2`: whether this is the hot cue or the memory list."""
        if self.type in ("PCOB", "PCO2") and len(self.data) >= 16:
            return struct.unpack_from(">I", self.data, 12)[0]
        return None

    @property
    def entries(self) -> list:
        """The tag's decoded entries (cues, or beats for `PQTZ`); [] otherwise."""
        if self._entries is None:
            decode = _DECODERS.get(self.type)
            self._entries = decode(self.data) if decode else []
        return self._entries


@dataclass
class AnlzFile:
    """An analysis file split into its header and raw tags."""

    header: bytes
    tags: list[Tag]

    def to_bytes(self) -> bytes:
        """Reassemble, fixing only the header's file length."""
        body = b"".join(t.data for t in self.tags)
        head = bytearray(self.header)
        struct.pack_into(">I", head, 8, len(head) + len(body))
        return bytes(head) + body


class AnlzError(ValueError):
    pass


def parse(data: bytes) -> AnlzFile:
    """Split a file into its tags. Raises `AnlzError` on a malformed file.

    Lengths are trusted only as far as the file reaches: a tag claiming more
    bytes than remain ends the walk with an error rather than a short read.
    """
    if len(data) < 12 or data[:4] != FILE_MAGIC:
        raise AnlzError("not an ANLZ file")
    len_header = struct.unpack_from(">I", data, 4)[0]
    if not 12 <= len_header <= len(data):
        raise AnlzError(f"bad file header length {len_header}")
    tags: list[Tag] = []
    off = len_header
    while off < len(data):
        if off + 12 > len(data):
            raise AnlzError(f"truncated tag at {off}")
        kind = data[off:off + 4].decode("latin-1")
        len_tag = struct.unpack_from(">I", data, off + 8)[0]
        if len_tag < 12 or off + len_tag > len(data):
            raise AnlzError(f"bad length {len_tag} for {kind} at {off}")
        tags.append(Tag(kind, data[off:off + len_tag]))
        off += len_tag
    return AnlzFile(header=data[:len_header], tags=tags)


def parse_file(path) -> AnlzFile:
    with open(path, "rb") as f:
        return parse(f.read())


# ---- cue entries ------------------------------------------------------------


@dataclass
class CueEntry:
    """One `PCPT` or `PCP2` entry. Attribute names follow pyrekordbox's, which
    is what `onelibrary.cues` was written against.

    `color_code` / RGB are None when the entry is too short to carry them (every
    `PCPT`, and rekordbox's compact 44-byte `PCP2`).
    """

    hot_cue: int
    type: int
    time: int
    loop_time: int
    comment: str = ""
    loop_numerator: int = 0
    loop_denominator: int = 0
    color_code: int | None = None
    color_red: int | None = None
    color_green: int | None = None
    color_blue: int | None = None
    len_entry: int = 0


def _walk(data: bytes, first: int, magic: bytes, count: int):
    """(offset, len_entry) for each entry, trusting each entry's own length."""
    off = first
    for _ in range(count):
        if off + 12 > len(data) or data[off:off + 4] != magic:
            return
        len_entry = struct.unpack_from(">I", data, off + 8)[0]
        if len_entry < 12 or off + len_entry > len(data):
            return
        yield off, len_entry
        off += len_entry


def _pcob(data: bytes) -> list[CueEntry]:
    # tag prefix(12) · I list kind · H 0 · H count · i last
    count = struct.unpack_from(">H", data, 18)[0]
    out = []
    for off, n in _walk(data, 24, b"PCPT", count):
        if n < 44:
            continue
        # 4s · I len_header · I len_entry · I hot_cue · I status · I 0x10000 ·
        # H order_first · H order_last · B kind · x · H 1000 · I time · I loop
        hot_cue, = struct.unpack_from(">I", data, off + 12)
        kind = data[off + 28]
        time, loop = struct.unpack_from(">II", data, off + 32)
        out.append(CueEntry(hot_cue=hot_cue, type=kind, time=time, loop_time=loop, len_entry=n))
    return out


def _pco2(data: bytes) -> list[CueEntry]:
    # tag prefix(12) · I list kind · H count · H 0
    count = struct.unpack_from(">H", data, 16)[0]
    out = []
    for off, n in _walk(data, 20, b"PCP2", count):
        if n < 28:
            continue
        hot_cue, = struct.unpack_from(">I", data, off + 12)
        kind = data[off + 16]
        time, loop = struct.unpack_from(">II", data, off + 20)
        e = CueEntry(hot_cue=hot_cue, type=kind, time=time, loop_time=loop, len_entry=n)
        if n >= 44:
            e.loop_numerator, e.loop_denominator, len_comment = struct.unpack_from(">HHI", data, off + 36)
            colour_at = off + 44 + len_comment
            if len_comment and colour_at <= off + n:
                e.comment = data[off + 44:colour_at].decode("utf-16-be", "replace").rstrip("\x00")
            if colour_at + 4 <= off + n:
                e.color_code, e.color_red, e.color_green, e.color_blue = data[colour_at:colour_at + 4]
        out.append(e)
    return out


# ---- the beatgrid -----------------------------------------------------------


@dataclass
class Beat:
    beat: int     # 1-4, position in the bar
    tempo: int    # BPM x 100
    time: int     # ms


def _pqtz(data: bytes) -> list[Beat]:
    # tag prefix(12) · I 0 · I 0x80000 · I count, then 8-byte entries
    count = struct.unpack_from(">I", data, 20)[0]
    first = struct.unpack_from(">I", data, 4)[0]
    out = []
    for i in range(count):
        off = first + 8 * i
        if off + 8 > len(data):
            break
        beat, tempo, time = struct.unpack_from(">HHI", data, off)
        out.append(Beat(beat=beat, tempo=tempo, time=time))
    return out


_DECODERS = {"PCOB": _pcob, "PCO2": _pco2, "PQTZ": _pqtz}


__all__ = [
    "AnlzError", "AnlzFile", "Beat", "CueEntry", "LIST_HOT", "LIST_MEMORY",
    "NO_LOOP", "Tag", "parse", "parse_file",
]
