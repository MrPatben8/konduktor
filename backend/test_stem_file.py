"""The stem writer (`core/stem_file.py`) on GENERATED sources with a FAKE engine.

The separation is a callback, so everything Konduktor itself does — decode,
encode five streams, the `stem` box, tags, verification — is tested here with no
torch installed. What would fail SILENTLY is what this pins:

  * the two stem JSON literals are exactly the bytes the user's commercial files
    and Traktor's collection hold (skipped when those files are not on the
    machine), and hold the same float32 values;
  * a mono source is not converted 3 dB quieter, and resampling does not move
    time — both would shift or change every converted track;
  * the finished file starts on the source's first sample (cue positions carry
    over unchanged in the decoded time base), with every tag and the cover;
  * `verify` refuses a shifted, truncated or box-less file.
"""
from __future__ import annotations

import fractions
import html
import json
import os
import re
import struct
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-stemfile-appdata-")

import av  # noqa: E402
import numpy as np  # noqa: E402

from konduktor.core import stem_file as sf  # noqa: E402
from konduktor.core import tag_copy  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def encode(path: Path, codec: str, fmt: str, x: np.ndarray, sr: int, layout="stereo", bit_rate=None):
    with av.open(str(path), "w", format=fmt) as c:
        s = c.add_stream(codec, rate=sr, layout=layout)
        if bit_rate:
            s.bit_rate = bit_rate
        n = x.shape[1]
        for o in range(0, n, 1152):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1152]), format="fltp", layout=layout)
            f.sample_rate, f.pts = sr, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def music(n, sr, seed=1):
    """Noise bursts on a beat: something every encoder and the correlator can grip."""
    rng = np.random.default_rng(seed)
    x = np.zeros((2, n), np.float32)
    for start in range(int(0.5 * sr), n - sr // 10, sr // 2):
        burst = rng.uniform(-0.6, 0.6, (2, sr // 20)).astype(np.float32)
        x[:, start:start + burst.shape[1]] += burst * np.linspace(1, 0, burst.shape[1])
    return x


def fake_separate(pcm):
    """Four 'stems' in DEMUCS_ORDER: scaled copies, so each stream is distinct."""
    return [pcm * g for g in (0.5, 0.25, -0.2, 0.1)]


tmp = Path(tempfile.mkdtemp(prefix="konduktor-stemfile-"))

print("== 1. the stem JSON literals ==")
box, nml = json.loads(sf.STEM_BOX_JSON), json.loads(sf.NML_STEMS_JSON)


def f32(o):
    if isinstance(o, float):
        return struct.unpack("f", struct.pack("f", o))[0]
    if isinstance(o, dict):
        return {k: f32(v) for k, v in o.items()}
    if isinstance(o, list):
        return [f32(v) for v in o]
    return o


check("the box and the NML value hold the same float32 values", f32(box) == f32(nml))
check("names and colours are Drums/Bass/Synths/Vox, as on every stem in the collection",
      [s["name"] for s in box["stems"]] == list(sf.STEM_NAMES))
check("mastering DSP is written disabled", not box["mastering_dsp"]["compressor"]["enabled"]
      and not box["mastering_dsp"]["limiter"]["enabled"])
commercial = sorted(Path.home().glob("Music/Library/*.stem.m4a"))
if commercial:
    check(f"the box is byte-identical to {len(commercial)} commercial stem files",
          all(sf.read_stem_box(p) == sf.STEM_BOX_JSON for p in commercial))
else:
    print("  (skipped: no commercial stem files on this machine)")
real_nml = Path.home() / "Downloads/collection_latest.nml"
if real_nml.is_file():
    values = {html.unescape(v) for v in re.findall(r'<STEMS STEMS="([^"]*)"', real_nml.read_text(encoding="utf-8"))}
    check("the NML value is the one Traktor wrote on every stem entry", values == {sf.NML_STEMS_JSON}, str(len(values)))
else:
    print("  (skipped: the real collection is not on this machine)")

print("== 2. decode: mono level, resampling does not move time ==")
m = np.zeros((1, 44100), np.float32)
m[0, 100] = 0.5
encode(tmp / "mono.wav", "pcm_f32le", "wav", m, 44100, layout="mono")
d = sf.decode(tmp / "mono.wav")
check("a mono source is duplicated at full level (not FFmpeg's -3 dB)",
      d.pcm.shape[0] == 2 and np.allclose(d.pcm[:, 100], 0.5), str(d.pcm[:, 100]))
x48 = np.zeros((2, 48000 * 3), np.float32)
x48[:, 48000] = 1.0
encode(tmp / "imp48.wav", "pcm_f32le", "wav", x48, 48000)
d = sf.decode(tmp / "imp48.wav")
check("a 48 kHz impulse at 1.000 s lands on sample 44100", int(np.argmax(np.abs(d.pcm[0]))) == 44100)
check("…and the length converts exactly", d.samples == 44100 * 3, str(d.samples))

print("== 3. build: MP3 and FLAC sources, both encoders ==")
from mutagen.flac import FLAC, Picture  # noqa: E402
from mutagen.id3 import APIC, ID3, TBPM, TIT2, TKEY, TPE1, TPUB, TRCK  # noqa: E402

SR = 44100
src = music(SR * 12, SR)
MP3 = tmp / "Track One.mp3"
encode(MP3, "libmp3lame", "mp3", src, SR, bit_rate=320_000)
tags = ID3()
for frame in (TIT2(encoding=3, text=["Track One"]), TPE1(encoding=3, text=["Some Artist"]),
              TKEY(encoding=3, text=["8A"]), TBPM(encoding=3, text=["125"]),
              TRCK(encoding=3, text=["3/12"]), TPUB(encoding=3, text=["A Label"])):
    tags.add(frame)
COVER = b"\xff\xd8\xff\xe0" + os.urandom(2000)
tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=COVER))
tags.save(str(MP3))

MP3_128 = tmp / "low.mp3"
encode(MP3_128, "libmp3lame", "mp3", src, SR, bit_rate=128_000)
FLAC_SRC = tmp / "Lossless.flac"
encode(FLAC_SRC, "flac", "flac", src, SR)
fl = FLAC(str(FLAC_SRC))
fl["title"], fl["artist"], fl["initialkey"], fl["tracknumber"], fl["tracktotal"] = "Lossless", "FLAC Artist", "11B", "4", "9"
pic = Picture()
pic.type, pic.mime, pic.data = 3, "image/jpeg", COVER
fl.add_picture(pic)
fl.save()

encoders = ["aac"] + (["aac_at"] if "aac_at" in av.codecs_available else [])
for enc in encoders:
    out = tmp / f"Track One.{enc}.stem.m4a.konduktor-partial"
    source = sf.decode(MP3)
    written = sf.build(MP3, out, fake_separate, encoder=enc)
    with av.open(str(out)) as c:
        streams = c.streams.audio
        info = (len(streams), [int(s.disposition) for s in streams])
    check(f"{enc}: five streams, only the mix enabled", info == (5, [1, 0, 0, 0, 0]), str(info))
    check(f"{enc}: the stem box is exactly the commercial one", sf.read_stem_box(out) == sf.STEM_BOX_JSON)
    check(f"{enc}: it counts as a stem file", sf.is_stem_file(out.with_name("x.stem.m4a")) is False
          and sf.read_stem_box(out) is not None)
    check(f"{enc}: 320 kbps from a 320 kbps source", written.bit_rate == 320_000, str(written.bit_rate))
    check(f"{enc}: duration is the source's", abs(written.duration - source.samples / SR) < 1e-9)
    def stream(k):
        with av.open(str(out)) as c:  # one open per stream: decoding one reads to EOF
            return np.concatenate([f.to_ndarray() for f in c.decode(c.streams.audio[k])], axis=1)
    mix, drums = stream(0), stream(1)
    # By cross-correlation, not a threshold onset: AAC pre-echo smears energy a
    # few dozen samples ahead of a sharp transient, which a threshold reads as
    # "early" although nothing moved.
    lag = sf._aligned(mix, source.pcm)
    check(f"{enc}: the mix is not shifted against the source (cues carry over unchanged)",
          lag == 0, f"lag {lag}")
    check(f"{enc}: stream 1 is the drums stem, not the mix", abs(np.max(np.abs(drums)) / np.max(np.abs(mix)) - 0.5) < 0.1)
    got = tag_copy.read_tags(out)
    check(f"{enc}: title, artist, key, BPM, track number and label copied",
          (got.get("title"), got.get("artist"), got.get("key"), got.get("bpm"), got.get("tracknumber"), got.get("label"))
          == ("Track One", "Some Artist", "8A", "125", "3/12", "A Label"), str(got))
    from mutagen.mp4 import MP4  # noqa: E402
    covr = MP4(str(out)).tags.get("covr")
    check(f"{enc}: the cover art is copied", bool(covr) and bytes(covr[0]) == COVER)

w = sf.build(MP3_128, tmp / "low.stem.m4a", fake_separate, encoder="aac")
check("a 128 kbps source is encoded at the 256 kbps floor", w.bit_rate == 256_000, str(w.bit_rate))
w = sf.build(FLAC_SRC, tmp / "Lossless.stem.m4a", fake_separate, encoder="aac")
check("a lossless source is encoded at 320 kbps", w.bit_rate == 320_000)
got = tag_copy.read_tags(tmp / "Lossless.stem.m4a")
check("FLAC's Vorbis tags cross into MP4 atoms (key, track n/total)",
      (got.get("title"), got.get("key"), got.get("tracknumber")) == ("Lossless", "11B", "4/9"), str(got))
check("a finished stem file is recognised as a stem container", sf.is_stem_file(tmp / "Lossless.stem.m4a"))
check("a plain M4A is not", not sf.is_stem_file(tmp / "plain.m4a"))
check("an MP3 is not, whatever the collection says", not sf.is_stem_file(MP3))
check("Track.mp3 -> Track.stem.m4a", sf.stem_target_name("Track.mp3") == "Track.stem.m4a")

print("== 4. verify refuses what is wrong ==")
source = sf.decode(MP3)


def raw_file(name, master):
    path = tmp / name
    with open(path, "wb") as fp:
        sf._encode(fp, [master, *fake_separate(master)], bit_rate=256_000, encoder="aac", checkpoint=None)
    sf.add_stem_box(path)
    return path


def refused(path, src_obj=source):
    try:
        sf.verify(path, src_obj)
        return None
    except sf.StemFileError as ex:
        return str(ex)


shifted = np.roll(source.pcm, 5, axis=1)
check("a mix shifted by 5 samples is refused", refused(raw_file("shift.m4a", shifted)) is not None)
check("a truncated mix is refused", refused(raw_file("short.m4a", source.pcm[:, : source.samples - 5000])) is not None)
good = raw_file("good.m4a", source.pcm)
check("the same file unshifted passes", refused(good) is None, refused(good) or "")
nobox = tmp / "nobox.m4a"
with open(nobox, "wb") as fp:
    sf._encode(fp, [source.pcm, *fake_separate(source.pcm)], bit_rate=256_000, encoder="aac", checkpoint=None)
check("a file without the stem box is refused", "box" in (refused(nobox) or ""), refused(nobox) or "")

print("== 5. cancel ==")


class Stop(Exception):
    pass


calls = {"n": 0}


def stop_in_encode():
    calls["n"] += 1
    if calls["n"] > 3:
        raise Stop()


try:
    sf.build(MP3, tmp / "cancelled.partial", fake_separate, encoder="aac", checkpoint=stop_in_encode)
    check("a checkpoint that raises stops the build", False, "it finished")
except Stop:
    check("a checkpoint that raises stops the build", True)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
