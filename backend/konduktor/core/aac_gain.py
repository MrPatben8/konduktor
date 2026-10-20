"""Lossless gain for AAC-LC in an MP4 (an `.m4a`, a stem file): aacgain's edit.

Every channel of every AAC frame carries an 8-bit `global_gain`, the start of
its scalefactors: the decoder scales that channel's spectrum by 2^(g/4), so
adding n to every `global_gain` scales the decoded audio by EXACTLY 2^(n/4)
(1.505 dB per step) with no re-encode — like MP3's frame-gain edit
(`level_edit`), and in the same steps. Spectral scalefactors, the noise
energies of PNS and the intensity positions are all coded RELATIVE to it (the
first two from it, intensity as a ratio between channels), so they follow.

Finding the field is the hard part: a channel pair holds the SECOND channel's
`global_gain` after the whole first channel — its section data, scalefactors,
TNS and Huffman-coded spectrum — so each frame is WALKED, never decoded:
codewords are looked up only for their lengths (and, in the unsigned books,
the values that decide sign bits and escapes). `aac_tables` holds the
standard's codebooks. The walk runs in numba (a dependency already, through
librosa): ~10 M codewords for a stereo track.

The walk proves itself on every frame: a raw_data_block must end in its last
byte (an END element, then byte alignment), so a mis-parse is caught, and the
whole file is refused, rather than written. `level_edit.verify` decodes the
result on top. What is refused (`AacGainError`): anything but AAC-LC
(HE-AAC/SBR, PS, LTP, Main, SSR), 960-sample frames, a coupling channel or a
program config element, a scalefactor pushed out of 0..255.

Measured 2026-10-19 on FFmpeg- and Apple-encoded files and commercial stem
files (`test_aac_gain.py`, and the spike numbers in the gain discussion).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import aac_tables as T

#: A stem file's streams all share one ladder: one step = 2^(1/4) in amplitude.
STEP_DB = 20.0 * np.log10(2.0 ** 0.25)


class AacGainError(ValueError):
    """A file this edit will not touch; the message is user-facing."""


# ---- the MP4 side: where every AAC frame of every audio track is ------------------

@dataclass
class AacTrack:
    """One audio track: its config and each frame's byte range in the file."""

    index: int            # the audio stream's order among the file's audio tracks
    object_type: int      # 2 = AAC-LC
    sf_index: int
    channels: int
    offsets: np.ndarray   # int64, each sample's file offset
    sizes: np.ndarray     # int64


_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        head = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            raise AacGainError("not an MP4 file this edit can read (a broken box)")
        yield kind, pos + head, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, kind: bytes):
    for k, s, e in _boxes(data, start, end):
        if k == kind:
            return s, e
    return None


def _descriptor(data: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, body start, body end) of an MPEG-4 descriptor at `pos`."""
    tag = data[pos]
    pos += 1
    size = 0
    for _ in range(4):
        b = data[pos]
        pos += 1
        size = (size << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return tag, pos, pos + size


def _audio_specific_config(data: bytes, s: int, e: int) -> tuple[int, int, int, bool]:
    """(object type, sampling-frequency index, channel config, has SBR) from
    an `esds` box body (version/flags first)."""
    tag, ds, de = _descriptor(data, s + 4)
    if tag != 3:
        raise AacGainError("an MP4 audio track without an ES descriptor")
    pos = ds + 2
    flags = data[pos]
    pos += 1
    if flags & 0x80:
        pos += 2
    if flags & 0x40:
        pos += 1 + data[pos]
    if flags & 0x20:
        pos += 2
    while pos < de:
        tag, bs, be = _descriptor(data, pos)
        if tag == 4:  # DecoderConfigDescriptor
            if data[bs] != 0x40:
                raise AacGainError("not an AAC track")
            p = bs + 13
            while p < be:
                t2, s2, e2 = _descriptor(data, p)
                if t2 == 5:
                    return _parse_asc(data[s2:e2])
                p = e2
        pos = be
    raise AacGainError("an AAC track with no AudioSpecificConfig")


def _parse_asc(asc: bytes) -> tuple[int, int, int, bool]:
    bits = int.from_bytes(asc, "big")
    n = len(asc) * 8
    pos = 0

    def read(k: int) -> int:
        nonlocal pos
        v = (bits >> (n - pos - k)) & ((1 << k) - 1)
        pos += k
        return v

    aot = read(5)
    if aot == 31:
        aot = 32 + read(6)
    sfi = read(4)
    if sfi == 15:
        freq = read(24)
        rates = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
        sfi = min(range(len(rates)), key=lambda i: abs(rates[i] - freq))
    channels = read(4)
    sbr = aot in (5, 29)
    if aot == 2:
        frame_length_flag = read(1)
        if frame_length_flag:
            raise AacGainError("AAC with 960-sample frames is not handled")
        if read(1):  # dependsOnCoreCoder
            read(14)
        read(1)  # extensionFlag
        # Implicit SBR signalling (a sync extension after the GA config) = HE-AAC.
        if n - pos >= 16 and read(11) == 0x2B7 and read(5) == 5 and n - pos >= 1 and read(1):
            sbr = True
    return aot, sfi, channels, sbr


def tracks(path: Path | str) -> list[AacTrack]:
    """Every audio track's AAC frames, in the file's audio-stream order."""
    data = Path(path).read_bytes()
    moov = _find(data, 0, len(data), b"moov")
    if moov is None:
        raise AacGainError("not an MP4 file")
    out = []
    for kind, ts, te in _boxes(data, *moov):
        if kind != b"trak":
            continue
        mdia = _find(data, ts, te, b"mdia")
        hdlr = _find(data, *mdia, b"hdlr") if mdia else None
        if hdlr is None or data[hdlr[0] + 8:hdlr[0] + 12] != b"soun":
            continue
        stbl = _find(data, *_find(data, *mdia, b"minf"), b"stbl")
        stsd = _find(data, *stbl, b"stsd")
        entry = stsd[0] + 8
        esize, ekind = struct.unpack_from(">I4s", data, entry)
        if ekind != b"mp4a":
            raise AacGainError(f"an audio track in {ekind.decode('latin-1')}, not AAC")
        esds = _find(data, entry + 36, entry + esize, b"esds")
        if esds is None:
            raise AacGainError("an AAC track with no esds")
        aot, sfi, channels, sbr = _audio_specific_config(data, *esds)
        if aot != 2 or sbr:
            raise AacGainError("only AAC-LC can be changed losslessly (this is HE-AAC or another profile)")
        stsz = _find(data, *stbl, b"stsz")
        uniform, count = struct.unpack_from(">II", data, stsz[0] + 4)
        sizes = (np.full(count, uniform, np.int64) if uniform else
                 np.frombuffer(data, ">u4", count, stsz[0] + 12).astype(np.int64))
        co = _find(data, *stbl, b"stco")
        if co is not None:
            n = struct.unpack_from(">I", data, co[0] + 4)[0]
            chunks = np.frombuffer(data, ">u4", n, co[0] + 8).astype(np.int64)
        else:
            co = _find(data, *stbl, b"co64")
            n = struct.unpack_from(">I", data, co[0] + 4)[0]
            chunks = np.frombuffer(data, ">u8", n, co[0] + 8).astype(np.int64)
        stsc = _find(data, *stbl, b"stsc")
        m = struct.unpack_from(">I", data, stsc[0] + 4)[0]
        runs = np.frombuffer(data, ">u4", m * 3, stsc[0] + 8).reshape(m, 3).astype(np.int64)
        offsets = np.empty(count, np.int64)
        i = 0
        for r in range(m):
            first = runs[r, 0] - 1
            last = (runs[r + 1, 0] - 1) if r + 1 < m else len(chunks)
            per = runs[r, 1]
            for c in range(first, last):
                pos = chunks[c]
                for _ in range(per):
                    if i >= count:
                        break
                    offsets[i] = pos
                    pos += sizes[i]
                    i += 1
        if i != count:
            raise AacGainError("an MP4 sample table this edit cannot follow")
        out.append(AacTrack(len(out), aot, sfi, channels, offsets, sizes))
    if not out:
        raise AacGainError("no AAC audio track")
    return out


# ---- the walk ---------------------------------------------------------------------

def _lut(codes, bits, width: int) -> np.ndarray:
    """A lookup on the next `width` bits: (length << 16) | index, 0 = invalid."""
    lut = np.zeros(1 << width, np.int32)
    for idx, (c, b) in enumerate(zip(codes, bits)):
        lo = c << (width - b)
        lut[lo:lo + (1 << (width - b))] = (b << 16) | idx
    return lut


_SPEC = np.stack([_lut(c, b, 16) for c, b in T.SPECTRAL])
_SF = _lut(T.SF_CODES, T.SF_BITS, 19)
_LONG = np.zeros((13, 64), np.int32)
_SHORT = np.zeros((13, 32), np.int32)
_NLONG = np.zeros(13, np.int32)
_NSHORT = np.zeros(13, np.int32)
for _i in range(13):
    _LONG[_i, :len(T.SWB_LONG[_i])] = T.SWB_LONG[_i]
    _SHORT[_i, :len(T.SWB_SHORT[_i])] = T.SWB_SHORT[_i]
    _NLONG[_i] = len(T.SWB_LONG[_i]) - 1
    _NSHORT[_i] = len(T.SWB_SHORT[_i]) - 1

try:
    from numba import njit
except ImportError:  # pragma: no cover — numba ships with librosa
    def njit(*_a, **_k):
        return lambda f: f


@njit(cache=True)
def _read(buf, pos, n):
    v = 0
    for i in range(n):
        p = pos + i
        v = (v << 1) | ((buf[p >> 3] >> (7 - (p & 7))) & 1)
    return v


@njit(cache=True)
def _peek(buf, pos, n, end):
    v = 0
    for i in range(n):
        p = pos + i
        bit = 0
        if p < end:
            bit = (buf[p >> 3] >> (7 - (p & 7))) & 1
        v = (v << 1) | bit
    return v


@njit(cache=True)
def _walk(buf, offsets, sizes, sfi, spec, sflut, long_swb, short_swb, nlong, nshort):
    """Every frame's global_gain bit positions; out[0] = status (0 ok, else the
    failing frame + 1, negated by reason), plus the scalefactor range."""
    n_frames = offsets.shape[0]
    gpos = np.empty(n_frames * 8, np.int64)
    ng = 0
    sf_lo = 1 << 30
    sf_hi = -(1 << 30)
    nz_lo = 1 << 30
    nz_hi = -(1 << 30)
    sfb_cb = np.zeros((8, 64), np.int32)
    group_len = np.zeros(8, np.int32)
    status = 0
    for f in range(n_frames):
        start = offsets[f] * 8
        end = start + sizes[f] * 8
        pos = start
        ok = True
        common_ws = 0
        common_max_sfb = 0
        common_groups = 0
        while ok:
            if pos + 3 > end:
                ok = False
                break
            elem = _read(buf, pos, 3)
            pos += 3
            if elem == 7:  # END
                break
            if elem == 0 or elem == 3 or elem == 1:  # SCE, LFE, CPE
                pos += 4  # element_instance_tag
                n_ch = 1
                common = 0
                if elem == 1:
                    n_ch = 2
                    common = _read(buf, pos, 1)
                    pos += 1
                    if common:
                        _r = _read(buf, pos, 1)  # reserved
                        common_ws = _read(buf, pos + 1, 2)
                        pos += 4
                        if common_ws == 2:
                            common_max_sfb = _read(buf, pos, 4)
                            grouping = _read(buf, pos + 4, 7)
                            pos += 11
                            common_groups = 1
                            group_len[:] = 0
                            group_len[0] = 1
                            for b in range(6, -1, -1):
                                if (grouping >> b) & 1:
                                    group_len[common_groups - 1] += 1
                                else:
                                    group_len[common_groups] = 1
                                    common_groups += 1
                        else:
                            common_max_sfb = _read(buf, pos, 6)
                            pos += 6
                            if _read(buf, pos, 1):  # predictor data (not LC)
                                status = -(f + 1)
                                return gpos[:0], status, 0, 0, 0, 0
                            pos += 1
                            common_groups = 1
                            group_len[0] = 1
                        ms = _read(buf, pos, 2)
                        pos += 2
                        if ms == 1:
                            pos += common_groups * common_max_sfb
                for ch in range(n_ch):
                    gpos[ng] = pos
                    ng += 1
                    gg = _read(buf, pos, 8)
                    pos += 8
                    if common:
                        ws = common_ws
                        max_sfb = common_max_sfb
                        groups = common_groups
                    else:
                        pos += 1
                        ws = _read(buf, pos, 2)
                        pos += 3
                        if ws == 2:
                            max_sfb = _read(buf, pos, 4)
                            grouping = _read(buf, pos + 4, 7)
                            pos += 11
                            groups = 1
                            group_len[:] = 0
                            group_len[0] = 1
                            for b in range(6, -1, -1):
                                if (grouping >> b) & 1:
                                    group_len[groups - 1] += 1
                                else:
                                    group_len[groups] = 1
                                    groups += 1
                        else:
                            max_sfb = _read(buf, pos, 6)
                            pos += 6
                            if _read(buf, pos, 1):
                                status = -(f + 1)
                                return gpos[:0], status, 0, 0, 0, 0
                            pos += 1
                            groups = 1
                            group_len[0] = 1
                    short = ws == 2
                    nswb = nshort[sfi] if short else nlong[sfi]
                    if max_sfb > nswb:
                        ok = False
                        break
                    # section_data
                    sbits = 3 if short else 5
                    esc = (1 << sbits) - 1
                    for g in range(groups):
                        k = 0
                        while k < max_sfb:
                            cb = _read(buf, pos, 4)
                            pos += 4
                            if cb == 12:
                                ok = False
                                break
                            length = 0
                            while True:
                                inc = _read(buf, pos, sbits)
                                pos += sbits
                                length += inc
                                if inc != esc:
                                    break
                                if pos > end:
                                    break
                            if k + length > max_sfb or pos > end:
                                ok = False
                                break
                            for s in range(k, k + length):
                                sfb_cb[g, s] = cb
                            k += length
                        if not ok:
                            break
                    if not ok:
                        break
                    # scale_factor_data
                    sf = gg
                    noise = gg - 90
                    noise_first = True
                    for g in range(groups):
                        for s in range(max_sfb):
                            cb = sfb_cb[g, s]
                            if cb == 0:
                                continue
                            if cb == 13 and noise_first:
                                noise_first = False
                                noise += _read(buf, pos, 9) - 256
                                pos += 9
                                if noise < nz_lo:
                                    nz_lo = noise
                                if noise > nz_hi:
                                    nz_hi = noise
                                continue
                            e = sflut[_peek(buf, pos, 19, end)]
                            if e == 0:
                                ok = False
                                break
                            pos += e >> 16
                            delta = (e & 0xFFFF) - 60
                            if cb == 13:
                                noise += delta
                                if noise < nz_lo:
                                    nz_lo = noise
                                if noise > nz_hi:
                                    nz_hi = noise
                            elif cb == 14 or cb == 15:
                                pass  # intensity: relative between channels
                            else:
                                sf += delta
                                if sf < sf_lo:
                                    sf_lo = sf
                                if sf > sf_hi:
                                    sf_hi = sf
                        if not ok:
                            break
                    if not ok:
                        break
                    # pulse_data
                    if _read(buf, pos, 1):
                        pos += 1
                        if short:
                            ok = False
                            break
                        npulse = _read(buf, pos, 2)
                        pos += 8 + (npulse + 1) * 9
                    else:
                        pos += 1
                    # tns_data
                    if _read(buf, pos, 1):
                        pos += 1
                        nwin = 8 if short else 1
                        for w in range(nwin):
                            nfilt = _read(buf, pos, 1 if short else 2)
                            pos += 1 if short else 2
                            if nfilt:
                                res = _read(buf, pos, 1)
                                pos += 1
                                for _q in range(nfilt):
                                    pos += 4 if short else 6
                                    order = _read(buf, pos, 3 if short else 5)
                                    pos += 3 if short else 5
                                    if order:
                                        pos += 1
                                        compress = _read(buf, pos, 1)
                                        pos += 1
                                        pos += order * (3 + res - compress)
                    else:
                        pos += 1
                    # gain_control_data (SSR only)
                    if _read(buf, pos, 1):
                        ok = False
                        break
                    pos += 1
                    # spectral_data
                    for g in range(groups):
                        glen = group_len[g]
                        for s in range(max_sfb):
                            cb = sfb_cb[g, s]
                            if cb == 0 or cb >= 13:
                                continue
                            if short:
                                width = short_swb[sfi, s + 1] - short_swb[sfi, s]
                            else:
                                width = long_swb[sfi, s + 1] - long_swb[sfi, s]
                            dim = 4 if cb <= 4 else 2
                            n_words = glen * width // dim
                            for _w in range(n_words):
                                e = spec[cb - 1, _peek(buf, pos, 16, end)]
                                if e == 0:
                                    ok = False
                                    break
                                pos += e >> 16
                                idx = e & 0xFFFF
                                if cb == 3 or cb == 4:
                                    v0 = idx // 27
                                    v1 = (idx // 9) % 3
                                    v2 = (idx // 3) % 3
                                    v3 = idx % 3
                                    pos += (v0 != 0) + (v1 != 0) + (v2 != 0) + (v3 != 0)
                                elif cb >= 7:
                                    mod = 8 if cb <= 8 else (13 if cb <= 10 else 17)
                                    y = idx // mod
                                    z = idx % mod
                                    pos += (y != 0) + (z != 0)
                                    if cb == 11:
                                        for v in (y, z):
                                            if v == 16:
                                                nesc = 0
                                                while _read(buf, pos, 1) == 1:
                                                    pos += 1
                                                    nesc += 1
                                                    if nesc > 8:
                                                        break
                                                pos += 1
                                                if nesc > 8:
                                                    ok = False
                                                    break
                                                pos += nesc + 4
                                if pos > end or not ok:
                                    ok = False
                                    break
                            if not ok:
                                break
                        if not ok:
                            break
                    if not ok:
                        break
                if not ok:
                    break
            elif elem == 4:  # DSE
                pos += 4
                align = _read(buf, pos, 1)
                pos += 1
                count = _read(buf, pos, 8)
                pos += 8
                if count == 255:
                    count += _read(buf, pos, 8)
                    pos += 8
                if align:
                    pos = start + ((pos - start + 7) // 8) * 8
                pos += count * 8
            elif elem == 6:  # FIL
                count = _read(buf, pos, 4)
                pos += 4
                if count == 15:
                    count += _read(buf, pos, 8) - 1
                    pos += 8
                if count > 0:
                    ext = _read(buf, pos, 4)
                    if ext == 13 or ext == 14:  # SBR: HE-AAC
                        status = -(f + 1)
                        return gpos[:0], status, 0, 0, 0, 0
                pos += count * 8
            else:  # CCE, PCE: not handled
                ok = False
                break
            if pos > end:
                ok = False
        # The block must end in its last byte: END, then byte alignment.
        if not ok or pos > end or end - pos >= 8:
            status = f + 1
            return gpos[:0], status, 0, 0, 0, 0
    return gpos[:ng], 0, sf_lo, sf_hi, nz_lo, nz_hi


@dataclass
class Scan:
    """Where every global_gain is, per audio track, and how far it may move."""

    tracks: list[AacTrack]
    positions: list[np.ndarray]   # bit offsets in the file, per track
    min_step: int                 # the most negative n every track allows
    max_step: int


def scan(path: Path | str) -> Scan:
    path = Path(path)
    found = tracks(path)
    buf = np.frombuffer(path.read_bytes(), np.uint8)
    positions = []
    lo_all, hi_all = -255, 255
    for t in found:
        if t.sf_index > 12:
            raise AacGainError("an AAC sampling rate this edit does not know")
        gpos, status, sf_lo, sf_hi, nz_lo, nz_hi = _walk(
            buf, t.offsets, t.sizes, t.sf_index, _SPEC, _SF, _LONG, _SHORT, _NLONG, _NSHORT)
        if status < 0:
            raise AacGainError("only AAC-LC can be changed losslessly (this frame uses another tool)")
        if status > 0:
            raise AacGainError(f"an AAC frame this edit cannot follow (frame {status - 1} of stream {t.index})")
        g = _read_all(buf, gpos)
        if gpos.size:
            lo_all = max(lo_all, -int(g.min()), -int(sf_lo) if sf_lo < (1 << 29) else -255)
            hi_all = min(hi_all, 255 - int(g.max()), 255 - int(sf_hi) if sf_hi > -(1 << 29) else 255)
            if nz_lo < (1 << 29):
                lo_all = max(lo_all, -155 - int(nz_lo))
                hi_all = min(hi_all, 100 - int(nz_hi))
        positions.append(gpos)
    return Scan(found, positions, lo_all, hi_all)


def _read_all(buf: np.ndarray, gpos: np.ndarray) -> np.ndarray:
    """The 8-bit values at each position (vectorised: a 16-bit window)."""
    byte = gpos >> 3
    shift = gpos & 7
    word = (buf[byte].astype(np.int64) << 8) | buf[np.minimum(byte + 1, buf.size - 1)].astype(np.int64)
    return (word >> (8 - shift)) & 0xFF


def apply(src: Path | str, dst: Path | str, steps: int, scanned: Scan | None = None) -> None:
    """Write `src` with every audio track's frames `steps` gain steps louder
    (negative: quieter) to `dst`, which must not exist. Same size, every byte
    outside the global_gain fields identical."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise AacGainError(f"{dst.name} already exists")
    found = scanned or scan(src)
    if not found.min_step <= steps <= found.max_step:
        raise AacGainError(f"{steps:+d} steps would take the AAC scalefactors out of range")
    data = np.frombuffer(src.read_bytes(), np.uint8).copy()
    for gpos in found.positions:
        values = _read_all(data, gpos) + steps
        byte = gpos >> 3
        shift = gpos & 7
        hi_mask = 0xFF >> shift                    # the field's bits in the first byte
        lo_mask = (0xFF << (8 - shift)) & 0xFF     # …and in the second
        first = (values >> shift) & 0xFF
        second = (values << (8 - shift)) & 0xFF
        data[byte] = (data[byte] & ~hi_mask & 0xFF) | (first & hi_mask)
        nxt = byte + 1
        has = shift > 0
        data[nxt[has]] = (data[nxt[has]] & ~lo_mask[has] & 0xFF) | (second[has] & lo_mask[has])
    dst.write_bytes(data.tobytes())
