"""`core.level_edit` on GENERATED files — every edit decode-checked.

MP3 is changed by the lossless frame-gain edit, so the proof is that the
decode is the source x 2^(n/4) and the file keeps its size (bar the APE undo
tag); WAV/AIFF are scaled in their data chunk, so every OTHER byte must stay
put; FLAC is re-encoded, so its tags and pictures must come back. The source
is never touched, and a refused edit leaves no file behind.
"""
from __future__ import annotations

import hashlib
import struct
import tempfile
from pathlib import Path

import av
import numpy as np
import soundfile as sf

from konduktor.core import level_edit
from konduktor.core.level_plan import STEP_DB

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def signal(sr: int, channels: int, seconds: float = 6.0, amp: float = 0.25) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(1)
    chans = [amp * (0.6 * np.sin(2 * np.pi * (110 + 37 * c) * t) + 0.3 * np.sin(2 * np.pi * 2200 * t)
                    + 0.1 * rng.standard_normal(t.size)) for c in range(channels)]
    return np.vstack(chans).astype(np.float32)


def encode_mp3(path: Path, *, sr=44100, channels=2, options=None, container_options=None, bit_rate=192_000):
    x = signal(sr, channels)
    layout = "stereo" if channels == 2 else "mono"
    with av.open(str(path), "w", format="mp3", container_options=container_options or {}) as c:
        s = c.add_stream("libmp3lame", rate=sr, layout=layout, options=options or {})
        s.bit_rate = bit_rate
        step = 1152
        for i in range(0, x.shape[1], step):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, i:i + step]), format="fltp", layout=layout)
            f.sample_rate, f.pts = sr, i
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def raises(fn) -> bool:
    try:
        fn()
    except level_edit.LevelEditError:
        return True
    return False


tmp = Path(tempfile.mkdtemp(prefix="konduktor-level-edit-"))

print("== MP3: the frame-gain edit is exact, lossless and keeps the size ==")
check("CRC-16 is Layer III's (poly 0x8005, init 0xFFFF: '123456789' -> 0xAEE7)",
      level_edit._crc16(b"123456789") == 0xAEE7)
variants = {
    "cbr-xing": dict(),
    "no-xing": dict(container_options={"write_xing": "0"}),
    "abr": dict(options={"abr": "1"}),
    "mono": dict(channels=1, bit_rate=96_000),
    "mpeg2-22k": dict(sr=22050, bit_rate=64_000),
}
for name, kw in variants.items():
    src = tmp / f"{name}.mp3"
    encode_mp3(src, **kw)
    if name == "cbr-xing":
        from mutagen.id3 import APIC, ID3, TIT2

        tags = ID3()
        tags.add(TIT2(encoding=3, text="level"))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"\xff\xd8" + bytes(5000)))
        tags.save(str(src))
    before = sha(src)
    for steps in (-2, 1):
        dst = tmp / f"{name}.{steps}.mp3"
        level_edit.apply(src, dst, steps * STEP_DB)
        try:
            level_edit.verify(src, dst, steps * STEP_DB)
            ok, why = True, ""
        except level_edit.LevelEditError as ex:
            ok, why = False, str(ex)
        check(f"{name} {steps:+d} steps: the decode is the source x 2^({steps}/4)", ok, why)
        from mutagen.apev2 import APEv2

        ape = APEv2(str(dst))
        grown = dst.stat().st_size - src.stat().st_size
        check(f"{name} {steps:+d}: same frames, only the APE undo tag added",
              0 < grown < 200 and str(ape["MP3GAIN_UNDO"]) == f"{-steps:+04d},{-steps:+04d},N", f"{grown} {ape}")
    check(f"{name}: the source is untouched", sha(src) == before)

print("== MP3: a second edit accumulates its undo; refusals leave nothing ==")
src, once, twice = tmp / "cbr-xing.mp3", tmp / "once.mp3", tmp / "twice.mp3"
level_edit.apply(src, once, -2 * STEP_DB)
level_edit.apply(once, twice, -1 * STEP_DB)
from mutagen.apev2 import APEv2  # noqa: E402

check("undo after -2 then -1 steps reads +3", str(APEv2(str(twice))["MP3GAIN_UNDO"]).startswith("+003"),
      str(APEv2(str(twice))["MP3GAIN_UNDO"]))
level_edit.verify(src, twice, -3 * STEP_DB)
check("…and the result is the source at -3 steps", True)
check("a gain that is not a whole number of steps is refused",
      raises(lambda: level_edit.apply(src, tmp / "x.mp3", -2.0)) and not (tmp / "x.mp3").exists())
check("a gain past MP3's range is refused, leaving no file",
      raises(lambda: level_edit.apply(src, tmp / "y.mp3", 300 * STEP_DB)) and not (tmp / "y.mp3").exists())
check("an existing target is never overwritten", raises(lambda: level_edit.apply(src, once, -STEP_DB)))
check("an unsupported format is refused", raises(lambda: level_edit.apply(src, tmp / "z.ogg", -1.0))
      or raises(lambda: level_edit.apply(tmp / "nothing.ogg", tmp / "z2.ogg", -1.0)))


def add_chunk_after(path: Path, cid: bytes, body: bytes, *, big_endian: bool) -> None:
    """Append an extra chunk (a tag a DJ app might keep) and fix the RIFF/FORM size."""
    data = bytearray(path.read_bytes())
    fmt = ">I" if big_endian else "<I"
    data += cid + struct.pack(fmt, len(body)) + body + (b"\0" if len(body) % 2 else b"")
    data[4:8] = struct.pack(fmt, len(data) - 8)
    path.write_bytes(data)


print("== WAV / AIFF: scaled inside the data chunk, every other byte untouched ==")
for suffix, subtype, big in ((".wav", "PCM_16", False), (".wav", "PCM_24", False), (".wav", "FLOAT", False),
                             (".aiff", "PCM_16", True), (".aiff", "PCM_24", True)):
    src = tmp / f"pcm-{subtype}{suffix}"
    sf.write(str(src), signal(44100, 2).T, 44100, subtype=subtype)
    add_chunk_after(src, b"id3 " if not big else b"ID3 ", b"ID3\x03\x00\x00\x00\x00\x00\x05TAGS!", big_endian=big)
    lay = (level_edit._wav_layout if suffix == ".wav" else level_edit._aiff_layout)(src)
    for gain in (-3.7, 2.5):
        dst = tmp / f"pcm-{subtype}-{gain}{suffix}"
        level_edit.apply(src, dst, gain)
        a, b = src.read_bytes(), dst.read_bytes()
        outside = a[:lay.offset] + a[lay.offset + lay.size:] == b[:lay.offset] + b[lay.offset + lay.size:]
        try:
            level_edit.verify(src, dst, gain)
            ok, why = True, ""
        except level_edit.LevelEditError as ex:
            ok, why = False, str(ex)
        check(f"{suffix} {subtype} {gain:+.1f} dB: same size, every other chunk byte-identical",
              len(a) == len(b) and outside)
        check(f"{suffix} {subtype} {gain:+.1f} dB: the decode is the source at the gain", ok, why)
    if subtype == "PCM_24":
        dst = tmp / f"exact-{subtype}{suffix}"
        level_edit.apply(src, dst, -6.0)
        x = sf.read(str(src), dtype="int32")[0].astype(np.int64) >> 8
        y = sf.read(str(dst), dtype="int32")[0].astype(np.int64) >> 8
        check(f"{suffix} 24-bit is rounded, not dithered",
              np.array_equal(y, np.rint(x * 10 ** (-6.0 / 20))))
src = tmp / "pcm-PCM_16.wav"
d1, d2 = tmp / "dither-a.wav", tmp / "dither-b.wav"
level_edit.apply(src, d1, -3.0)
level_edit.apply(src, d2, -3.0)
check("16-bit is dithered — and reproducibly (the same edit writes the same bytes)",
      d1.read_bytes() == d2.read_bytes()
      and not np.array_equal(sf.read(str(d1), dtype="int16")[0],
                             np.rint(sf.read(str(src), dtype="int16")[0] * 10 ** (-3 / 20)).astype(np.int16)))

print("== FLAC: re-encoded at its depth, tags and pictures carried ==")
from mutagen.flac import FLAC, Picture  # noqa: E402

for subtype in ("PCM_16", "PCM_24"):
    src = tmp / f"tagged-{subtype}.flac"
    sf.write(str(src), signal(48000, 2).T, 48000, subtype=subtype)
    f = FLAC(str(src))
    f["title"], f["artist"], f["initialkey"] = "Level", "Konduktor", "8A"
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", b"\xff\xd8" + bytes(3000)
    f.add_picture(pic)
    f.save()
    dst = tmp / f"tagged-{subtype}-out.flac"
    level_edit.apply(src, dst, -4.2)
    try:
        level_edit.verify(src, dst, -4.2)
        ok, why = True, ""
    except level_edit.LevelEditError as ex:
        ok, why = False, str(ex)
    check(f"FLAC {subtype}: the decode is the source at -4.2 dB", ok, why)
    out = FLAC(str(dst))
    check(f"FLAC {subtype}: same depth and rate", sf.info(str(dst)).subtype == subtype
          and sf.info(str(dst)).samplerate == 48000)
    check(f"FLAC {subtype}: every tag and the picture come back",
          dict(out.tags.as_dict()) == dict(FLAC(str(src)).tags.as_dict())
          and [p.data for p in out.pictures] == [pic.data], str(out.tags.as_dict()))

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
raise SystemExit(1 if failed else 0)
