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

    PCP2:  4s magic · I len_header · I len_entry · I hot_cue · B kind · x
           H 1000 · I time · I loop_time · B color_id · B 1 · 6x
           H loop_num · H loop_den · I len_comment · {n}s comment(utf-16-be)
           4B colour · {pad}x

**`pyrekordbox` accepting the result is NOT enough.** Its parser ignores
`len_header` and the constants, so an early version with a 12-byte `PCPT`
header round-tripped through Konduktor's own reader perfectly. The cue layout is
therefore pinned to a real rekordbox 7 export byte-for-byte
(`test_export_pioneer.py`).

**Which tags a file needs was measured, not assumed** — by stripping tags from a
rekordbox-written stick one at a time and loading each track in rekordbox 7:
  * `.DAT` needs `PVBR` AND the preview waveform (`PWAV`/`PWV2`) before rekordbox
    shows the grid OR the hot cues. The grid and cue tags alone — rekordbox's
    own bytes — show nothing. (This, not the cue bytes, was why an early
    Konduktor export had no grid.)
  * rekordbox follows the database's analysis-path column; the folder names
    need not match its own path hash.
  * `.EXT` extras (`PWV3`-`PWV5`, `PQT2`, `PSSI`) and the whole `.2EX` affect
    only the drawn waveforms.

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
_LEN_HEADER = {"PPTH": 16, "PQTZ": 24, "PCOB": 24, "PCO2": 20,
               "PVBR": 16, "PWAV": 20, "PWV2": 20}


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


# ---- what rekordbox needs before it will show a grid or cues ------------------
#
# Measured on a rekordbox-written stick by removing tags one at a time: with
# only PPTH + PQTZ + PCOB in the `.DAT` — the grid and cue tags byte-for-byte
# rekordbox's own — rekordbox shows NO grid and NO hot cues. Adding PVBR alone
# is not enough, nor PWAV + PWV2 alone; the full `.DAT` is. So every `.DAT`
# carries all three. (The `.EXT` and `.2EX` waveforms only affect drawing.)

#: PVBR's entry count: 400 seek points plus one trailing value.
_PVBR_ENTRIES = 401


def vbr_tag(total_samples: int = 0) -> bytes:
    """`PVBR` as rekordbox writes it for a constant-rate file.

    400 zero seek offsets, then the track's length in SAMPLES — for an MP3,
    frames x 1152 (Demo Track 1: 6601 x 1152 = 7604352). An `.m4a` gets zero
    there too. A VBR MP3 presumably gets real offsets; none of the reference
    tracks is VBR, so that is unmeasured, and a zero index is what an `.m4a`
    carries and rekordbox accepts.
    """
    body = struct.pack(">I", 0) + bytes(4 * (_PVBR_ENTRIES - 1)) + struct.pack(
        ">I", max(0, int(total_samples)))
    return _wrap("PVBR", body)


def mp3_samples(path: Path) -> int:
    """An MP3's length in samples — frames x 1152 — for `PVBR`; 0 otherwise.

    rekordbox writes this value only for MP3s (every `.m4a` carries 0).
    """
    if path.suffix.lower() != ".mp3":
        return 0
    try:
        import mutagen

        info = mutagen.File(str(path)).info
        return int(round(info.length * info.sample_rate / 1152)) * 1152
    except Exception:  # an unreadable header costs the seek hint, not the export
        return 0


#: PWAV height = K * sqrt(rms) + B, fitted against rekordbox's own previews of
#: eight tracks (mean error 3.4 on the 0-31 scale, correlation 0.8). An absolute
#: scale, not normalised per track — a quiet master draws smaller, as in rekordbox.
_PWAV_K, _PWAV_B = 37.6, -5.2


def preview_tags(rms, brightness) -> list[bytes]:
    """`PWAV` (400 columns) and `PWV2` (100) from per-column RMS and brightness.

    `PWAV`: one byte per column — whiteness (3 bits) << 5 | height (5 bits).
    `PWV2`: a 4-bit height per group of four `PWAV` columns, scaled so the
    track's tallest column is 15; that reproduces rekordbox's own to a mean
    error of 1.7. Whiteness loosely follows brightness in rekordbox's output
    (it is cosmetic: the colour of the blue-to-white preview).
    """
    import numpy as np

    rms = np.asarray(rms, dtype=float)
    heights = np.clip(np.round(_PWAV_K * np.sqrt(rms) + _PWAV_B), 0, 31).astype(int)
    white = np.clip(np.round(2.5 + 10.0 * np.asarray(brightness, dtype=float)), 0, 5).astype(int)
    pwav = bytes(int((w << 5) | h) for w, h in zip(white, heights))

    tallest = max(int(heights.max()), 1)
    grouped = heights.reshape(100, 4).max(axis=1)
    pwv2 = bytes(int(v) for v in np.clip(np.round(grouped * 15 / tallest), 0, 15))
    return [
        _wrap("PWAV", struct.pack(">II", len(pwav), 0x10000) + pwav),
        _wrap("PWV2", struct.pack(">II", len(pwv2), 0x10000) + pwv2),
    ]


def flat_preview_tags() -> list[bytes]:
    """Empty previews, for a file nothing could decode, so the grid and cues can
    still show. That an all-zero preview satisfies rekordbox is an assumption,
    by analogy with the all-zero PVBR every `.m4a` carries — not measured."""
    return preview_tags([0.0] * 400, [0.0] * 400)


def _pcp2_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None,
                rgb: tuple[int, int, int] | None, beats: int | None) -> bytes:
    """One `PCP2` entry, byte-for-byte as rekordbox 7 writes it.

    Two constants in here are rekordbox's, not the documented struct's: the
    `1000` after the kind (the struct calls it padding) and the `1` after the
    colour id. Both appear in every entry of a real export; zeros there were a
    guess, and pyrekordbox's parser would never notice one.
    """
    comment = b""
    red, green, blue = rgb or (0, 0, 0)
    return struct.pack(
        ">4sIIIBxHIIBB6xHHI",
        b"PCP2", 16, PCP2_ENTRY_LEN, hot_cue, kind, 1000,
        int(time_ms), NO_LOOP if loop_ms is None else int(loop_ms),
        0, 1, beats or 0, 1 if beats else 0, len(comment),
    ) + comment + struct.pack(">4B", 0, red, green, blue) + bytes(
        PCP2_ENTRY_LEN - 48 - len(comment)
    )


def _pcpt_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None) -> bytes:
    """One `PCPT` entry, byte-for-byte as rekordbox 7 writes it.

    `len_header` is **28**, not 12: a reader that trusts it finds the body 16
    bytes early.
    Status is 0 on a hot cue and 4 on a memory cue, and rekordbox leaves the
    linked-list order fields at 0xFFFF rather than linking the entries.
    """
    return struct.pack(
        ">4sIIIIIHHBxHII",
        b"PCPT", 28, PCPT_ENTRY_LEN, hot_cue,
        0 if hot_cue else 4,
        0x10000, 0xFFFF, 0xFFFF, kind, 1000,
        int(time_ms), NO_LOOP if loop_ms is None else int(loop_ms),
    ) + bytes(16)


def _pcob(cue_type: int, chosen: list[dict]) -> bytes:
    blob = b"".join(
        _pcpt_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                    loop_ms=c["loop_ms"])
        for c in chosen
    )
    # cue_type is Int32ub (an Enum over it), NOT Int16 — getting this wrong
    # shifts every following field and the reader sees garbage. The last field
    # is -1 on a hot-cue list and (count - 1) on a memory list, as rekordbox
    # writes it.
    last = -1 if cue_type == 1 else len(chosen) - 1
    return _wrap("PCOB", struct.pack(">IHHi", cue_type, 0, len(chosen), last) + blob)


def _pco2(cue_type: int, chosen: list[dict]) -> bytes:
    blob = b"".join(
        _pcp2_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                    loop_ms=c["loop_ms"], rgb=c.get("rgb"), beats=c.get("beats"))
        for c in chosen
    )
    return _wrap("PCO2", struct.pack(">IHH", cue_type, len(chosen), 0) + blob)


def cue_tags(cues: list[dict], *, extended: bool) -> list[bytes]:
    """The cue tags for one file, in the order and shape rekordbox writes them.

    `.DAT`: `PCOB` hot (pads 1-3), `PCOB` memory (every memory cue).
    `.EXT`: `PCOB` hot (pads 4 and up), `PCOB` memory (EMPTY), then `PCO2` hot
    and `PCO2` memory — each the complete list of its kind.

    **Every list is written even when empty.** A rekordbox file always carries
    all of them, and a file missing one is not a shape rekordbox produces.
    Hot cues are ordered by pad, HIGHEST first, which is also what it does.
    """
    hot = sorted((c for c in cues if c["hot_cue"]), key=lambda c: -c["hot_cue"])
    memory = sorted((c for c in cues if not c["hot_cue"]), key=lambda c: c["time_ms"])
    if not extended:
        return [_pcob(1, [c for c in hot if c["hot_cue"] <= 3]), _pcob(0, memory)]
    return [
        _pcob(1, [c for c in hot if c["hot_cue"] > 3]),
        _pcob(0, []),
        _pco2(1, hot),
        _pco2(0, memory),
    ]


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
