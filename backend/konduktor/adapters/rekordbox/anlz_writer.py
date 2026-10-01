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

from ...core.waveform import even_slices

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
               "PVBR": 16, "PWAV": 20, "PWV2": 20,
               "PWV3": 24, "PWV5": 24, "PWV4": 24, "PWV7": 24, "PWV6": 20, "PWVC": 14}


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


# ---- the detail and 3-band waveforms (`.EXT` / `.2EX`) -------------------------
#
# These only affect DRAWING — rekordbox shows the grid and cues without them — but
# without them its song list has no preview and its deck draws a flat blue line,
# and it warns the device was "analyzed by an older version". All constants are
# fitted against rekordbox 7's own files for 12 local tracks, measured through
# `core.waveform` exactly as the exporter measures (per-band mean error ~8 on the
# 0-127 scale). rekordbox 7's song list draws `PWV6` — but ONLY for a track
# whose `content.contentLink` is set (see the OneLibrary exporter); with it NULL
# the row is plain blue whatever the files hold. `PWV4` is written by
# distribution matching, not per-column fitting (see `_PWV4_MAPS`).

#: PWV7 byte per band = top * (energy / its 99.5th percentile) ** gamma.
_PWV7 = ((88, 0.6), (92, 0.9), (68, 1.7))            # low, mid, high
#: PWV6: the same, over 1200 overview columns (column = mean of its frames).
_PWV6 = ((40, 0.5), (34, 0.5), (34, 0.5))
#: PWVC: a per-band gain, = C / (the band's 99.5th percentile), floored at 80 —
#: rekordbox's own values follow this to about +/-20%. What rekordbox does with
#: it is not known; it is reproduced rather than invented.
_PWVC_C, _PWVC_FLOOR = (20763.0, 15220.0, 12571.0), 80
#: PWV3/PWV5 height (5 bits) from overall energy: 28 * x ** 1.5.
_HEIGHT = (28, 1.5)
#: PWV5 colour (3 bits each) from each band's SHARE of the frame's energy.
_RGB = ((8.47, 0.54), (7.60, 0.47), (13.78, 1.83))   # red<-low, green<-mid, blue<-high
_OVERVIEW_COLUMNS = 1200

#: PWV4 — the 6-byte colour overview. NOT what rekordbox 7's song list draws
#: (that is PWV6: removing PWV6 from a rekordbox-written track left its row
#: empty, removing PWV4 changed nothing); written because every rekordbox file
#: carries it and its RGB mode and older players may read it. Per column: b0 overall level, b1 a
#: companion of b0, b2 the height, b3/b4/b5 the low/mid/high (red/green/blue)
#: levels. Two measured facts shape it: b4 and b5 are sampled at ONE instant per
#: column (their column-to-column correlation is 0.05 and 0.16, against ~0.6 for
#: the rest), and rekordbox's per-column bytes are too jumpy to reproduce one by
#: one — so each byte is a QUANTILE MAP, (normalised measurement knots, byte
#: knots), fitted on 12 tracks so the distribution matches rekordbox's (held-out
#: medians within a few levels, the same spiky top end). b2 and b1 follow from
#: the others by linear fits on rekordbox's own bytes (mean error 10 and 12).
_PWV4_MAPS = {
    "b0": ((0.0, 0.1027, 0.1843, 0.3094, 0.3971, 0.4832, 0.5759, 0.6569, 0.731, 0.8102, 0.8973, 0.9419, 0.971, 1.1784),
           (0, 17, 28, 45, 56, 62, 65, 69, 74, 115, 127, 127, 127, 127)),
    "b3": ((0.0, 0.0135, 0.0473, 0.1407, 0.26, 0.3841, 0.5027, 0.5911, 0.6686, 0.7617, 0.8646, 0.9209, 0.9645, 1.1825),
           (0, 1, 4, 15, 24.7, 37, 44, 54, 62, 68, 89, 107, 120, 127)),
    "b4": ((0.0, 0.0305, 0.0654, 0.1307, 0.1855, 0.2325, 0.2819, 0.3342, 0.3977, 0.4809, 0.6036, 0.708, 0.8719, 1.2804),
           (0, 2, 7, 12, 17, 21, 25, 30, 35, 43, 62, 109, 127, 127)),
    "b5": ((0.0, 0.0077, 0.0223, 0.0511, 0.09, 0.1298, 0.1809, 0.2427, 0.3167, 0.4143, 0.5749, 0.7209, 0.863, 1.4313),
           (0, 1, 4, 7, 10, 12, 16, 20, 24, 31, 60, 127, 127, 127)),
}


def _pwv4(bands) -> bytes:
    import numpy as np

    full = np.sqrt((bands ** 2).sum(axis=1))
    x = np.column_stack([bands, full])                       # low, mid, high, full
    cols = [np.arange(len(x))[s] for s in even_slices(len(x), _OVERVIEW_COLUMNS)]
    mean = np.array([x[i].mean(axis=0) if len(i) else np.zeros(4) for i in cols])
    instant = np.array([x[i[len(i) // 2]] if len(i) else np.zeros(4) for i in cols])

    def mapped(values, key):
        ref = float(np.percentile(values, 99.5)) or 1.0
        knots_x, knots_y = _PWV4_MAPS[key]
        return np.clip(np.round(np.interp(values / ref, knots_x, knots_y)), 0, 127).astype(int)

    b0, b3 = mapped(mean[:, 3], "b0"), mapped(mean[:, 0], "b3")
    b4, b5 = mapped(instant[:, 1], "b4"), mapped(instant[:, 2], "b5")
    b2 = np.clip(np.round(0.83 * np.maximum(b3, np.maximum(b4, b5)) + 11.6), 0, 127).astype(int)
    b1 = np.clip(np.round(235.2 - 0.77 * b0), 0, 255).astype(int)
    return np.column_stack([b0, b1, b2, b3, b4, b5]).astype(np.uint8).tobytes()


def _norm(x, top: float, gamma: float, cap: int):
    import numpy as np

    ref = float(np.percentile(x, 99.5)) or 1.0
    return np.clip(np.round(top * np.clip(x / ref, 0, None) ** gamma), 0, cap).astype(int)


def waveform_tags(bands) -> tuple[list[bytes], list[bytes]]:
    """(`.EXT` tags, `.2EX` tags) from 150-per-second low/mid/high band energy.

    `.EXT`: `PWV3` (detail, 1 byte: whiteness << 5 | height), `PWV5` (detail
    colour, 2 bytes: rrr ggg bbb hhhhh 00) and `PWV4` (the RGB-mode colour
    overview, 1200 x 6 bytes — see `_PWV4_MAPS`). `.2EX`: `PWV7` (3-band detail, one
    byte per band), `PWV6` (3-band overview, 1200 columns) and `PWVC`. The frames
    must already be on rekordbox's clock (`waveform.analyse(lead=offset)`).
    """
    import numpy as np

    bands = np.asarray(bands, dtype=float)
    n = len(bands)
    full = np.sqrt((bands ** 2).sum(axis=1))
    share = bands / (bands.sum(axis=1, keepdims=True) + 1e-12)

    height = _norm(full, *_HEIGHT, 31)
    white = np.clip(np.round(2.5 + 10.0 * share[:, 2]), 0, 7).astype(int)
    pwv3 = bytes(int((w << 5) | h) for w, h in zip(white, height))
    rgb = [np.clip(np.round(k * share[:, i] + c), 0, 7).astype(int)
           for i, (k, c) in enumerate(_RGB)]
    pwv5 = b"".join(struct.pack(">H", (int(r) << 13) | (int(g) << 10) | (int(b) << 7) | (int(h) << 2))
                    for r, g, b, h in zip(*rgb, height))

    pwv7 = np.stack([_norm(bands[:, i], top, g, 127) for i, (top, g) in enumerate(_PWV7)], axis=1)
    cols = np.array([bands[s].mean(axis=0) if n else np.zeros(3)
                     for s in even_slices(n, _OVERVIEW_COLUMNS)])
    pwv6 = np.stack([_norm(cols[:, i], top, g, 127) for i, (top, g) in enumerate(_PWV6)], axis=1)
    gains = [max(_PWVC_FLOOR, int(round(c / (float(np.percentile(bands[:, i], 99.5)) or 1.0))))
             for i, c in enumerate(_PWVC_C)]

    ext = [
        _wrap("PWV3", struct.pack(">III", 1, n, 0x00960000) + pwv3),
        _wrap("PWV5", struct.pack(">III", 2, n, 0x00960305) + pwv5),
        _wrap("PWV4", struct.pack(">III", 6, _OVERVIEW_COLUMNS, 0) + _pwv4(bands)),
    ]
    two_ex = [
        _wrap("PWV7", struct.pack(">III", 3, n, 0x00960000) + pwv7.astype(np.uint8).tobytes()),
        _wrap("PWV6", struct.pack(">II", 3, _OVERVIEW_COLUMNS) + pwv6.astype(np.uint8).tobytes()),
        _wrap("PWVC", struct.pack(">H", 0) + struct.pack(">HHH", *[min(g, 0xFFFF) for g in gains])),
    ]
    return ext, two_ex


def flat_preview_tags() -> list[bytes]:
    """Empty previews, for a file nothing could decode, so the grid and cues can
    still show. That an all-zero preview satisfies rekordbox is an assumption,
    by analogy with the all-zero PVBR every `.m4a` carries — not measured."""
    return preview_tags([0.0] * 400, [0.0] * 400)


def pcp2_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None,
               rgb: tuple[int, int, int] | None, beats: int | None, code: int = 0,
               comment: str | None = None) -> bytes:
    """One `PCP2` entry, byte-for-byte as rekordbox 7 writes it.

    Two constants in here are rekordbox's, not the documented struct's: the
    `1000` after the kind (the struct calls it padding) and the `1` after the
    colour id. Both appear in every entry of a real export; zeros there were a
    guess, and pyrekordbox's parser would never notice one.

    A `comment` (the cue's name) is UTF-16BE with a terminating NUL, and
    `len_comment` counts the terminator — crate-digger's layout, which both
    Konduktor's reader and pyrekordbox's agree with. No reference cue carries
    one, so that detail is documented rather than measured. The entry stays the
    exported 88 bytes unless the name needs more.
    """
    comment_bytes = (comment.encode("utf-16-be") + b"\0\0") if comment else b""
    red, green, blue = rgb or (0, 0, 0)
    length = max(PCP2_ENTRY_LEN, 48 + len(comment_bytes))
    return struct.pack(
        ">4sIIIBxHIIBB6xHHI",
        b"PCP2", 16, length, hot_cue, kind, 1000,
        int(time_ms), NO_LOOP if loop_ms is None else int(loop_ms),
        0, 1, beats or 0, 1 if beats else 0, len(comment_bytes),
    # The colour is the palette CODE, then RGB: rekordbox draws from the code
    # (`palette`); a code of 0 draws its default colour whatever the RGB says.
    ) + comment_bytes + struct.pack(">4B", code & 0xFF, red, green, blue) + bytes(
        length - 48 - len(comment_bytes)
    )


def pcpt_entry(*, hot_cue: int, kind: int, time_ms: int, loop_ms: int | None) -> bytes:
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


def pcob_tag(cue_type: int, entries: list[bytes]) -> bytes:
    """A `PCOB` around already-built `PCPT` entries (rekordbox's own, untouched,
    or new ones from `pcpt_entry`)."""
    # cue_type is Int32ub (an Enum over it), NOT Int16 — getting this wrong
    # shifts every following field and the reader sees garbage. The last field
    # is -1 on a hot-cue list and (count - 1) on a memory list, as rekordbox
    # writes it (an export, and rekordbox's own edits, alike).
    last = -1 if cue_type == 1 else len(entries) - 1
    return _wrap("PCOB", struct.pack(">IHHi", cue_type, 0, len(entries), last) + b"".join(entries))


def pco2_tag(cue_type: int, entries: list[bytes]) -> bytes:
    """A `PCO2` around already-built `PCP2` entries."""
    return _wrap("PCO2", struct.pack(">IHH", cue_type, len(entries), 0) + b"".join(entries))


def _pcob(cue_type: int, chosen: list[dict]) -> bytes:
    return pcob_tag(cue_type, [
        pcpt_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                   loop_ms=c["loop_ms"])
        for c in chosen
    ])


def _pco2(cue_type: int, chosen: list[dict]) -> bytes:
    return pco2_tag(cue_type, [
        pcp2_entry(hot_cue=c["hot_cue"], kind=c["kind"], time_ms=c["time_ms"],
                   loop_ms=c["loop_ms"], rgb=c.get("rgb"), beats=c.get("beats"),
                   code=c.get("code", 0), comment=c.get("comment"))
        for c in chosen
    ])


#: `PQT2` as rekordbox 7 leaves it after a grid edit: header kept, every count
#: and the beat list gone (56 bytes, the `len_header`). MEASURED on Goober
#: (2026-10-01): rekordbox does not recompute this "extended" grid when the grid
#: changes, it blanks it, and a CDJ then works from `PQTZ`.
_PQT2_HEADER = 56


def blank_pqt2(tag: bytes) -> bytes:
    """The blanked form of an existing `PQT2` tag, exactly as rekordbox writes it.

    Bytes 12-24 (rekordbox's constants, `00000000 01000002 00000000` on every
    file seen) are KEPT; the counts and first/last-beat fields after them are
    zeroed, and the beat entries dropped.
    """
    return tag[:8] + struct.pack(">I", _PQT2_HEADER) + tag[12:24] + bytes(_PQT2_HEADER - 24)


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


def anlz_bytes(tags: list[bytes]) -> bytes:
    """Assemble an ANLZ file from built tag bytes."""
    body = b"".join(tags)
    header = struct.pack(
        ">4sIIIIII", b"PMAI", _FILE_HEADER_LEN, _FILE_HEADER_LEN + len(body),
        FILE_HEADER_U1, FILE_HEADER_U2, FILE_HEADER_U3, 0,
    )
    return header + body


def write_anlz(path: Path, tags: list[bytes]) -> Path:
    """Assemble and write an ANLZ file from built tag bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(anlz_bytes(tags))
    return path
