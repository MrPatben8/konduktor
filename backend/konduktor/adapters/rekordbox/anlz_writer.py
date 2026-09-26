"""Building ANLZ analysis files from nothing.

Lives here rather than in the OneLibrary adapter because the format is Pioneer's,
not one product's: a `master.db` track and a USB drive track carry the same tags.
OneLibrary borrows it, the same direction it already borrows `beatgrid`.

The read path parses these with `pyrekordbox`; writing needs the inverse, and
two things stand in the way of simply reusing its `construct` structs.

**`AnlzTag.content` is a Switch**, so a tag is built by handing it the parsed
STRUCTURE rather than bytes. Fine once known, surprising until then.

**The cue-entry structs cannot be built at all.** `AnlzCuePoint` and
`AnlzCuePoint2` each declare `"type"` twice — once as a `Const` magic string
(`PCPT`/`PCP2`) and once as the cue's kind. Parsing works because the second
silently overwrites the first in the result; BUILDING is impossible, because
`construct` reads one value out of the dict for both and no single value can
satisfy a string `Const` and an integer field. So cue entries are packed here by
hand, to the layout `pyrekordbox`'s own structs document:

    PCP2:  4s magic · I len_header · I len_entry · I hot_cue · B kind · 3x
           I time · I loop_time · B color_id · 7x · H loop_num · H loop_den
           I len_comment · {n}s comment(utf-16-be) · 4B colour · {pad}x

Everything is verified by parsing the result back through `pyrekordbox`, which is
the only check that matters: the reader is the thing that has to accept it.

Slot numbering here is ANLZ's own — a **dense 1-based** `hot_cue` where 0 means a
memory cue. That is NOT `master.db`'s sparse `Kind` bank, which skips 4.
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path

from construct import Container, ListContainer
from pyrekordbox.anlz import structs

log = logging.getLogger(__name__)

NO_LOOP = 0xFFFFFFFF
#: `len_entry` rekordbox itself writes for a comment-less PCP2 entry.
PCP2_ENTRY_LEN = 88
PCPT_ENTRY_LEN = 56

#: Copied from a rekordbox-written file; zeros here would be a guess.
FILE_HEADER_U1, FILE_HEADER_U2, FILE_HEADER_U3 = 1, 0x10000, 0x10000


#: The three fields every tag starts with: magic, len_header, len_tag.
_TAG_PREFIX = 12


#: Each tag counts a different number of its leading CONTENT fields as part of
#: its header. Read off `pyrekordbox`'s own tag classes rather than guessed.
_LEN_HEADER = {"PPTH": 16, "PQTZ": 24, "PCOB": 24, "PCO2": 20}


def _wrap(kind: str, body: bytes) -> bytes:
    """An AnlzTag around already-built content bytes.

    Returns BYTES, not a tag object. `AnlzFile.build()` cannot be used on the
    write side at all: it re-builds every tag from its parsed struct, which puts
    the cue tags straight back through the unbuildable duplicate-`type` structs.
    So the whole file is assembled here and `pyrekordbox` is used only to read
    the result back — which is the direction that has to work anyway.
    """
    return struct.pack(">4sII", kind.encode("ascii"), _LEN_HEADER[kind],
                       _TAG_PREFIX + len(body)) + body


def path_tag(drive_relative: str) -> bytes:
    """`PPTH` — the track's own path, as the drive stores it."""
    raw_len = (len(drive_relative) + 1) * 2
    body = structs.PPTH.build(Container(len_path=raw_len, path=drive_relative))
    return _wrap("PPTH", body)


def beatgrid_tag(beats: list[tuple[int, float, float]]) -> bytes:
    """`PQTZ` — EVERY beat, as (beat-in-bar 1-4, bpm, seconds).

    The grid is per-beat here, not per-marker: a OneLibrary/Rekordbox grid has
    no concept of a tempo marker, so the generic marker list is expanded into
    individual beats on the way out and collapsed back on the way in.
    """
    entries = ListContainer([
        Container(beat=beat, tempo=int(round(bpm * 100)), time=int(round(sec * 1000)))
        for beat, bpm, sec in beats
    ])
    body = structs.PQTZ.build(Container(entry_count=len(entries), entries=entries))
    return _wrap("PQTZ", body)


def _pcp2_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None,
                rgb: tuple[int, int, int] | None, beats: int | None) -> bytes:
    comment = b""
    red, green, blue = rgb or (0, 0, 0)
    return struct.pack(
        ">4sIIIB3xIIB7xHHI",
        b"PCP2", 16, PCP2_ENTRY_LEN, hot_cue, kind,
        int(time_ms), NO_LOOP if loop_ms is None else int(loop_ms),
        0, beats or 0, 1 if beats else 0, len(comment),
    ) + comment + struct.pack(">4B", 0, red, green, blue) + bytes(
        PCP2_ENTRY_LEN - 48 - len(comment)
    )


def _pcpt_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None,
                first: int, last: int) -> bytes:
    return struct.pack(
        ">4sIIIIIHHBxHII",
        b"PCPT", 12, PCPT_ENTRY_LEN, hot_cue,
        4 if loop_ms is not None else 4,   # status: 4 = active, as rekordbox writes
        0x10000, first, last, kind, 1000,
        int(time_ms), NO_LOOP if loop_ms is None else int(loop_ms),
    ) + bytes(16)


def cue_tags(cues: list[dict], *, extended: bool) -> list[bytes]:
    """`PCOB` (+ `PCO2` in the .EXT) for one track's cues.

    `PCOB` is **split by pad**: the `.DAT` carries hot cues 1-3 and the `.EXT`
    carries 4 and up, which is why the reader merges across both files. Memory
    cues go in their own `PCOB` with `cue_type` 0. `PCO2` is the complete list
    and only exists in the `.EXT`.
    """
    hot = [c for c in cues if c["hot_cue"]]
    memory = [c for c in cues if not c["hot_cue"]]
    tags: list[bytes] = []

    # PCOB, one per kind, filtered to the pads this file is responsible for.
    for cue_type, group in ((0, memory), (1, hot)):
        chosen = [
            c for c in group
            if not c["hot_cue"] or ((c["hot_cue"] > 3) == extended)
        ] if cue_type == 1 else (group if not extended else [])
        if not chosen:
            continue
        blob = b"".join(
            _pcpt_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                        loop_ms=c["loop_ms"],
                        first=0xFFFF if i == 0 else i,
                        last=i + 1 if i + 1 < len(chosen) else 0xFFFF)
            for i, c in enumerate(chosen)
        )
        # cue_type is Int32ub (an Enum over it), NOT Int16 — getting this wrong
        # shifts every following field and the reader sees garbage.
        header = struct.pack(">IHHi", cue_type, 0, len(chosen), len(chosen))
        tags.append(_wrap("PCOB", header + blob))

    if extended and cues:
        blob = b"".join(
            _pcp2_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                        loop_ms=c["loop_ms"], rgb=c.get("rgb"), beats=c.get("beats"))
            for c in cues
        )
        header = struct.pack(">IHH", 1 if hot else 0, len(cues), 0)
        tags.append(_wrap("PCO2", header + blob))
    return tags


#: `PMAI` + len_header + len_file + four unknowns.
_FILE_HEADER_LEN = 28


def write_anlz(path: Path, tags: list[bytes]) -> Path:
    """Assemble and write an ANLZ file from built tag bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(tags)
    header = struct.pack(
        ">4sIIIIII", b"PMAI", _FILE_HEADER_LEN, _FILE_HEADER_LEN + len(body),
        FILE_HEADER_U1, FILE_HEADER_U2, FILE_HEADER_U3, 0,
    )
    path.write_bytes(header + body)
    return path
