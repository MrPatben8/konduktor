"""Change an audio file's level — losslessly where the format allows, exactly
where it is PCM. Always into a NEW file: the source is never touched.

Decided 2026-10-19 (the gain-normalisation discussion; `level_plan` decides
the gain):

  * **MP3: the frame-gain edit** (mp3gain's): every granule's 8-bit
    `global_gain` in the side info moves by whole steps of 2^(1/4) in
    amplitude (1.505 dB). No re-encode, so no quality loss, no change of
    encoder delay (cues stay put: `adapters/*/timebase.py`), same size. CRC-
    protected frames get their CRC-16 recomputed; the Xing/Info/VBRI header
    frame is not audio and is left alone. The undo is recorded as mp3gain
    records it — an APEv2 `MP3GAIN_UNDO` tag (+ `MP3GAIN_MINMAX`) — which
    Traktor 4.5 was measured to accept (kit G1). Verified exact on real MP3s:
    the decode is the original x 2^(n/4) to within 1e-5 of peak.
  * **WAV / AIFF: the samples are scaled IN their data chunk** (`data`,
    `SSND`), in place in a copy, so the size and every other chunk (ID3,
    cue, bext, …) are byte-identical.
  * **FLAC: decoded, scaled and re-encoded** at the same bit depth and rate,
    every Vorbis comment, picture and application block copied back.
  * 16-bit PCM is TPDF-dithered (+-1 LSB triangular) unless the result is
    exact; 24-bit and float are rounded without dither.

`verify` decodes source and result in step and checks result == source x g.
Kit G1 showed Traktor re-analyses nothing when a file's level changes — the
LIBRARY's stored gain goes stale, which the caller rewrites (`set_auto_gain`).
"""

from __future__ import annotations

import math
import shutil
import struct
from pathlib import Path

import numpy as np

from .level_plan import EXACT, STEP_DB, STEPPED


class LevelEditError(Exception):
    """A file this module cannot (or will not) change; the message is user-facing."""


def apply(src: Path | str, dst: Path | str, gain_db: float) -> None:
    """Write `src` at `gain_db` to `dst`, which must not exist."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise LevelEditError(f"{dst.name} already exists")
    suffix = src.suffix.lower()
    try:
        if suffix == ".mp3":
            _mp3(src, dst, gain_db)
        elif suffix in STEPPED:
            _aac(src, dst, gain_db)
        elif suffix in (".wav", ".wave"):
            shutil.copyfile(src, dst)
            _scale_chunk(dst, _wav_layout(dst), gain_db)
        elif suffix in (".aif", ".aiff"):
            shutil.copyfile(src, dst)
            _scale_chunk(dst, _aiff_layout(dst), gain_db)
        elif suffix == ".flac":
            _flac(src, dst, gain_db)
        else:
            raise LevelEditError(f"Cannot change the level of a {suffix or 'suffix-less'} file")
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


# ---- MP3: the frame-gain edit -------------------------------------------------

_BITRATE = {
    True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATE = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _get(buf, bit: int, n: int) -> int:
    v = 0
    for i in range(n):
        v = (v << 1) | ((buf[(bit + i) >> 3] >> (7 - ((bit + i) & 7))) & 1)
    return v


def _put(buf: bytearray, bit: int, n: int, v: int) -> None:
    for i in range(n):
        b = (v >> (n - 1 - i)) & 1
        byte, shift = (bit + i) >> 3, 7 - ((bit + i) & 7)
        buf[byte] = (buf[byte] & ~(1 << shift) & 0xFF) | (b << shift)


def _frame(buf, pos: int) -> dict | None:
    """A Layer III frame header at `pos`, or None."""
    if pos + 4 > len(buf) or buf[pos] != 0xFF or (buf[pos + 1] & 0xE0) != 0xE0:
        return None
    ver, layer = (buf[pos + 1] >> 3) & 3, (buf[pos + 1] >> 1) & 3
    br_i, sr_i = buf[pos + 2] >> 4, (buf[pos + 2] >> 2) & 3
    if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
        return None
    mpeg1 = ver == 3
    nch = 1 if (buf[pos + 3] >> 6) == 3 else 2
    length = (144000 if mpeg1 else 72000) * _BITRATE[mpeg1][br_i] // _RATE[ver][sr_i] + ((buf[pos + 2] >> 1) & 1)
    side_len = (17 if nch == 1 else 32) if mpeg1 else (9 if nch == 1 else 17)
    return {"mpeg1": mpeg1, "crc": (buf[pos + 1] & 1) == 0, "nch": nch, "len": length, "side_len": side_len}


def _gain_bits(h: dict) -> list[int]:
    """Bit offsets, from the side info's start, of every granule's global_gain:
    MPEG-1 has 2 granules of 59 bits per channel, MPEG-2/2.5 one of 63."""
    nch = h["nch"]
    if h["mpeg1"]:
        start, block, granules = 9 + (5 if nch == 1 else 3) + 4 * nch, 59, 2
    else:
        start, block, granules = 8 + (1 if nch == 1 else 2), 63, 1
    return [start + (g * nch + c) * block + 21 for g in range(granules) for c in range(nch)]


def _crc16(data: bytes) -> int:
    """CRC-16 as Layer III protects a frame: poly 0x8005, init 0xFFFF, over
    header bytes 2-3 and the side info."""
    crc = 0xFFFF
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            top = (crc >> 15) & 1
            crc = (crc << 1) & 0xFFFF
            if top ^ bit:
                crc ^= 0x8005
    return crc


def _audio_start(buf) -> int:
    """Past an ID3v2 tag (and its footer), where the frames begin."""
    if buf[:3] != b"ID3" or len(buf) < 10:
        return 0
    size = (buf[6] << 21) | (buf[7] << 14) | (buf[8] << 7) | buf[9]
    return 10 + size + (10 if buf[5] & 0x10 else 0)


def mp3_steps(gain_db: float) -> int:
    steps = round(gain_db / STEP_DB)
    if abs(steps * STEP_DB - gain_db) > 1e-3:
        raise LevelEditError(f"{gain_db:+.2f} dB is not a whole number of MP3 gain steps")
    return steps


def _mp3(src: Path, dst: Path, gain_db: float) -> None:
    steps = mp3_steps(gain_db)
    buf = bytearray(src.read_bytes())
    pos = _audio_start(buf)
    frames, first = 0, True
    lo, hi = 255, 0
    while (h := _frame(buf, pos)) is not None:
        side = pos + 4 + (2 if h["crc"] else 0)
        if side + h["side_len"] > len(buf):
            break
        tag = bytes(buf[side + h["side_len"]: side + h["side_len"] + 4])
        if first and (tag in (b"Xing", b"Info") or bytes(buf[pos + 36: pos + 40]) == b"VBRI"):
            pass  # the header frame is not audio
        else:
            for off in _gain_bits(h):
                g = _get(buf, side * 8 + off, 8) + steps
                if not 0 <= g <= 255:
                    raise LevelEditError(f"{src.name}: the gain would leave MP3's range (frame {frames})")
                _put(buf, side * 8 + off, 8, g)
                lo, hi = min(lo, g), max(hi, g)
            if h["crc"]:
                crc = _crc16(bytes(buf[pos + 2: pos + 4]) + bytes(buf[side: side + h["side_len"]]))
                buf[pos + 4: pos + 6] = struct.pack(">H", crc)
            frames += 1
        first = False
        pos += h["len"]
    rest = bytes(buf[pos: pos + 8])
    if frames == 0 or (pos < len(buf) - 128 and not rest.startswith((b"TAG", b"APETAGEX", b"LYRICS"))):
        raise LevelEditError(f"{src.name}: not an MP3 this edit can read (lost frame sync at byte {pos})")
    dst.write_bytes(buf)
    _mp3gain_tag(dst, steps, lo, hi)


def _mp3gain_tag(path: Path, steps: int, lo: int, hi: int) -> None:
    """mp3gain's own record of the edit, so a later undo (by mp3gain, or by
    Konduktor) is exact: MP3GAIN_UNDO = the steps that REVERSE every edit so
    far, per channel, and whether any granule wrapped (never: refused)."""
    from mutagen.apev2 import APEv2, APENoHeaderError

    try:
        tag = APEv2(str(path))
    except APENoHeaderError:
        tag = APEv2()
    undo = -steps
    if "MP3GAIN_UNDO" in tag:
        try:
            undo += int(str(tag["MP3GAIN_UNDO"]).split(",")[0])
        except ValueError:
            pass
    tag["MP3GAIN_UNDO"] = f"{undo:+04d},{undo:+04d},N"
    tag["MP3GAIN_MINMAX"] = f"{lo:03d},{hi:03d}"
    tag.save(str(path))


# ---- AAC (an .m4a, a stem file): the same edit, in `aac_gain` -----------------

def _aac(src: Path, dst: Path, gain_db: float) -> None:
    """Every audio stream of the MP4 (a stem file's five alike) moved by the
    same steps. No undo tag: one would grow `moov` and shift every sample
    offset; the edit is exactly reversed by the opposite steps."""
    from . import aac_gain

    steps = mp3_steps(gain_db)
    try:
        aac_gain.apply(src, dst, steps)
    except aac_gain.AacGainError as ex:
        raise LevelEditError(f"{src.name}: {ex}") from ex


# ---- WAV / AIFF: the samples scaled in their own chunk ------------------------

class _Layout:
    def __init__(self, offset: int, size: int, channels: int, bits: int, kind: str, endian: str):
        self.offset, self.size, self.channels = offset, size, channels
        self.bits, self.kind, self.endian = bits, kind, endian  # kind: "int" | "float"


def _wav_layout(path: Path) -> _Layout:
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] not in (b"RIFF", b"RIFX") or head[8:12] != b"WAVE":
            raise LevelEditError(f"{path.name}: not a WAV file")
        endian = "<" if head[:4] == b"RIFF" else ">"
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack(endian + "I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                if size % 2:
                    f.seek(1, 1)
                continue
            if cid == b"data":
                if fmt is None:
                    raise LevelEditError(f"{path.name}: data before its format")
                tag, channels = struct.unpack(endian + "HH", fmt[:4])
                bits = struct.unpack(endian + "H", fmt[14:16])[0]
                if tag == 0xFFFE and len(fmt) >= 26:
                    tag = struct.unpack(endian + "H", fmt[24:26])[0]
                kind = {1: "int", 3: "float"}.get(tag)
                if kind is None or (kind == "int" and bits not in (16, 24, 32)) or (kind == "float" and bits not in (32, 64)):
                    raise LevelEditError(f"{path.name}: a WAV encoding this edit does not handle (format {tag}, {bits}-bit)")
                return _Layout(f.tell(), size, channels, bits, kind, endian)
            f.seek(size + (size % 2), 1)
    raise LevelEditError(f"{path.name}: no audio data")


def _aiff_layout(path: Path) -> _Layout:
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"FORM" or head[8:12] not in (b"AIFF", b"AIFC"):
            raise LevelEditError(f"{path.name}: not an AIFF file")
        comm = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack(">I", hdr[4:])[0]
            if cid == b"COMM":
                comm = f.read(size)
                if size % 2:
                    f.seek(1, 1)
                continue
            if cid == b"SSND":
                if comm is None:
                    raise LevelEditError(f"{path.name}: sound data before its format")
                channels, _frames, bits = struct.unpack(">hIh", comm[:8])
                compression = comm[18:22] if head[8:12] == b"AIFC" and len(comm) >= 22 else b"NONE"
                offset, _block = struct.unpack(">II", f.read(8))
                endian, kind = ">", "int"
                if compression == b"sowt":
                    endian = "<"
                elif compression in (b"fl32", b"FL32"):
                    kind, bits = "float", 32
                elif compression != b"NONE":
                    raise LevelEditError(f"{path.name}: compressed AIFF ({compression.decode(errors='replace')})")
                if kind == "int" and bits not in (16, 24, 32):
                    raise LevelEditError(f"{path.name}: {bits}-bit AIFF is not handled")
                return _Layout(f.tell() + offset, size - 8 - offset, channels, bits, kind, endian)
            f.seek(size + (size % 2), 1)
    raise LevelEditError(f"{path.name}: no sound data")


def _decode_ints(raw: bytes, bits: int, endian: str) -> np.ndarray:
    if bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        if endian == "<":
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        else:
            v = b[:, 2] | (b[:, 1] << 8) | (b[:, 0] << 16)
        return np.where(v & 0x800000, v - 0x1000000, v).astype(np.int64)
    return np.frombuffer(raw, np.dtype(f"{endian}i{bits // 8}")).astype(np.int64)


def _encode_ints(v: np.ndarray, bits: int, endian: str) -> bytes:
    if bits == 24:
        u = (v.astype(np.int64) & 0xFFFFFF).astype(np.uint32)
        out = np.empty((u.size, 3), np.uint8)
        order = (0, 1, 2) if endian == "<" else (2, 1, 0)
        for i, o in enumerate(order):
            out[:, i] = (u >> (8 * o)) & 0xFF
        return out.tobytes()
    return v.astype(np.dtype(f"{endian}i{bits // 8}")).tobytes()


def _quantise(x: np.ndarray, bits: int, rng, dither: bool) -> np.ndarray:
    """Float samples in integer units -> integers, TPDF-dithered when asked,
    clipped to the format's range (a raise is capped below full scale by
    `level_plan`, so clipping is only ever the dither's last LSB)."""
    if dither:
        x = x + rng.random(x.shape) - rng.random(x.shape)
    top = 2 ** (bits - 1)
    return np.clip(np.rint(x), -top, top - 1).astype(np.int64)


def _needs_dither(bits: int, factor: float) -> bool:
    # Exact only when scaling up by a whole power of two (an integer factor).
    return bits == 16 and not (factor >= 1 and float(factor).is_integer())


def _scale_chunk(path: Path, lay: _Layout, gain_db: float) -> None:
    factor = 10.0 ** (gain_db / 20.0)
    width = lay.bits // 8
    frame = width * lay.channels
    rng = np.random.default_rng(0)  # reproducible: a re-run writes the same bytes
    dither = lay.kind == "int" and _needs_dither(lay.bits, factor)
    usable = lay.size - lay.size % frame
    chunk = frame * 65536
    with open(path, "r+b") as f:
        done = 0
        while done < usable:
            n = min(chunk, usable - done)
            f.seek(lay.offset + done)
            raw = f.read(n)
            if len(raw) < n:
                raise LevelEditError(f"{path.name}: the audio data is shorter than its header says")
            if lay.kind == "float":
                dtype = np.dtype(f"{lay.endian}f{width}")
                out = (np.frombuffer(raw, dtype).astype(np.float64) * factor).astype(dtype).tobytes()
            else:
                out = _encode_ints(_quantise(_decode_ints(raw, lay.bits, lay.endian) * factor, lay.bits, rng, dither),
                                   lay.bits, lay.endian)
            f.seek(lay.offset + done)
            f.write(out)
            done += n


# ---- FLAC: decoded, scaled, re-encoded --------------------------------------------

def _flac(src: Path, dst: Path, gain_db: float) -> None:
    import soundfile as sf
    from mutagen.flac import FLAC

    info = sf.info(str(src))
    bits = {"PCM_16": 16, "PCM_24": 24, "PCM_S8": 8, "PCM_U8": 8}.get(info.subtype)
    if bits not in (16, 24):
        raise LevelEditError(f"{src.name}: {info.subtype} FLAC is not handled")
    factor = 10.0 ** (gain_db / 20.0)
    rng = np.random.default_rng(0)
    dither = _needs_dither(bits, factor)
    # int32 reads hold the sample in the TOP bits: shift to the format's units.
    shift = 32 - bits
    with sf.SoundFile(str(src)) as fin, sf.SoundFile(str(dst), "w", samplerate=info.samplerate,
                                                      channels=info.channels, format="FLAC",
                                                      subtype=info.subtype) as fout:
        for block in fin.blocks(blocksize=65536, dtype="int32", always_2d=True):
            v = _quantise((block.astype(np.int64) >> shift) * factor, bits, rng, dither)
            fout.write((v << shift).astype(np.int32))
    old, new = FLAC(str(src)), FLAC(str(dst))
    if old.tags:
        if new.tags is None:
            new.add_tags()
        for key, values in old.tags.as_dict().items():
            new.tags[key] = values
    for picture in old.pictures:
        new.add_picture(picture)
    for block in old.metadata_blocks:
        if getattr(block, "code", None) == 2:  # APPLICATION (e.g. a DJ app's private data)
            new.metadata_blocks.append(block)
    new.save()


# ---- verification ---------------------------------------------------------------

def _audio_streams(path: Path) -> int:
    from . import pyav

    with pyav.open_input(path) as c:
        return len(c.streams.audio)


def _frames(path: Path, index: int = 0):
    import av

    from . import pyav

    with pyav.open_input(path) as c:
        stream = c.streams.audio[index]
        rs = av.AudioResampler(format="fltp", layout="stereo" if stream.codec_context.channels > 1 else "mono",
                               rate=int(stream.codec_context.sample_rate))
        for frame in c.decode(stream):
            for out in rs.resample(frame):
                yield out.to_ndarray().astype(np.float64)
        for out in rs.resample(None):
            yield out.to_ndarray().astype(np.float64)


def verify(src: Path | str, dst: Path | str, gain_db: float, *, tolerance: float = 1e-3) -> None:
    """Decode both and check `dst` == `src` x 10^(g/20), to within `tolerance`
    of the source's peak (16-bit dither is ~3e-5) — EVERY audio stream (a
    stem file's five). Raises `LevelEditError`."""
    n_src, n_dst = _audio_streams(Path(src)), _audio_streams(Path(dst))
    if n_src != n_dst:
        raise LevelEditError(f"{Path(dst).name}: the number of audio streams changed")
    for index in range(n_src):
        _verify_stream(Path(src), Path(dst), gain_db, tolerance, index)


def _verify_stream(src: Path, dst: Path, gain_db: float, tolerance: float, index: int) -> None:
    factor = 10.0 ** (gain_db / 20.0)
    a_iter, b_iter = _frames(src, index), _frames(dst, index)
    a_buf = b_buf = None
    worst = peak = 0.0
    length_a = length_b = 0
    while True:
        if a_buf is None or a_buf.shape[1] == 0:
            a_buf = next(a_iter, None)
            if a_buf is not None:
                length_a += a_buf.shape[1]
        if b_buf is None or b_buf.shape[1] == 0:
            b_buf = next(b_iter, None)
            if b_buf is not None:
                length_b += b_buf.shape[1]
        if a_buf is None or b_buf is None:
            break
        n = min(a_buf.shape[1], b_buf.shape[1])
        if a_buf.shape[0] != b_buf.shape[0]:
            raise LevelEditError(f"{Path(dst).name}: the channel count changed")
        a, b = a_buf[:, :n], b_buf[:, :n]
        peak = max(peak, float(np.abs(a).max(initial=0.0)))
        worst = max(worst, float(np.abs(b - a * factor).max(initial=0.0)))
        a_buf, b_buf = a_buf[:, n:], b_buf[:, n:]
    for rest in a_iter:
        length_a += rest.shape[1]
    for rest in b_iter:
        length_b += rest.shape[1]
    if abs(length_a - length_b) > 0:
        raise LevelEditError(f"{Path(dst).name}: the length changed ({length_a} vs {length_b} samples)")
    if peak > 0 and worst / peak > tolerance:
        raise LevelEditError(f"{Path(dst).name}: the result is not the source at {gain_db:+.2f} dB "
                             f"(off by {20 * math.log10(max(worst / peak, 1e-12)):.1f} dB of peak)")
