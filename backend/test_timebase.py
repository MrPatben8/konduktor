"""Traktor's MP3 time base — the offset between its clock and the decoded audio.

Nothing fails when this is wrong: every MP3 cue and beat is simply ~51 ms off,
which is what every release before `adapters/traktor/timebase.py` did. So this
pins the numbers rather than trusting them, in three layers:

  1. `core.mp3_gapless` on GENERATED MP3s: the header frame is found AFTER an
     ID3 tag holding a megabyte of art (the check that missed it once read only
     the start of the file), and a header-less MP3 has none.
  2. The Traktor store and projection on a skeleton collection pointing at those
     files: a write adds the offset, a read subtracts it, a read is NOT clamped
     (a grid anchored inside the header frame survives a Reset byte for byte),
     and a file with nothing to go by — WAV, missing — gets 0.
  3. Traktor's OWN data: the hotcues placed by hand in Traktor 4.5 on kit 4's
     synthetic kick must read back onto that kick in the decoded audio. Skipped
     cleanly when kit 4 or its collection is not on this machine.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-timebase-appdata-")

import av  # noqa: E402
import numpy as np  # noqa: E402

from konduktor.adapters.traktor import timebase  # noqa: E402
from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.adapters.traktor.export import SKELETON  # noqa: E402
from konduktor.core import mp3_gapless  # noqa: E402
from konduktor.core.adapter import NewTrack  # noqa: E402
from konduktor.core.model import Track  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def encode(path: Path, codec: str, fmt: str, sr: int = 44100, container_options=None, bit_rate=320_000):
    """Two seconds of a click at 1.0 s — enough for a header; content is irrelevant."""
    n = 2 * sr
    x = np.zeros((2, n), dtype=np.float32)
    x[:, sr:sr + 64] = 0.8
    with av.open(str(path), "w", format=fmt, options=container_options or {}) as c:
        s = c.add_stream(codec, rate=sr, layout="stereo")
        if bit_rate and codec != "pcm_s16le":
            s.bit_rate = bit_rate
        step = s.codec_context.frame_size or 1024
        for i in range(0, n, step):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, i:i + step]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = sr, i
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def add_big_id3(path: Path):
    from mutagen.id3 import APIC, ID3, TIT2

    tags = ID3()
    tags.add(TIT2(encoding=3, text="timebase"))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=os.urandom(1_000_000)))
    tags.save(str(path))


tmp = Path(tempfile.mkdtemp(prefix="konduktor-timebase-"))
XING = tmp / "xing.mp3"
NOXING = tmp / "noxing.mp3"
MP3_48K = tmp / "48k.mp3"
WAV = tmp / "plain.wav"
encode(XING, "libmp3lame", "mp3")
add_big_id3(XING)
encode(NOXING, "libmp3lame", "mp3", container_options={"write_xing": "0"})
encode(MP3_48K, "libmp3lame", "mp3", sr=48000)
encode(WAV, "pcm_s16le", "wav")

print("== 1. mp3_gapless reads the first frame ==")
x = mp3_gapless.read(XING)
check("the header frame is found after a 1 MB ID3 tag", x is not None and x.header_frame, str(x))
check("…with LAME's 576-sample encoder delay", x is not None and x.encoder_delay == 576, str(x))
check("…in an MPEG-1 frame of 1152 samples", x is not None and x.samples_per_frame == 1152 and x.sample_rate == 44100)
n = mp3_gapless.read(NOXING)
check("a header-less MP3 has no header frame", n is not None and not n.header_frame, str(n))
check("a missing file reads as None (and is not cached as header-less)",
      mp3_gapless.read(tmp / "gone.mp3") is None)

print("== 1b. timebase.offset ==")
check("MP3 with a header: 2257 samples", timebase.offset_samples(XING) == (2257, 44100), str(timebase.offset_samples(XING)))
check("…which is 51.18 ms at 44.1 kHz", abs(timebase.offset_ms(XING) - 51.179138) < 1e-6, str(timebase.offset_ms(XING)))
check("48 kHz: the same 2257 SAMPLES, i.e. 47.02 ms",
      timebase.offset_samples(MP3_48K) == (2257, 48000) and abs(timebase.offset_ms(MP3_48K) - 47.020833) < 1e-6,
      str(timebase.offset_samples(MP3_48K)))
check("header-less MP3: 0", timebase.offset_ms(NOXING) == 0.0)
check("WAV: 0", timebase.offset_ms(WAV) == 0.0)
check("a missing MP3: 0", timebase.offset_ms(tmp / "gone.mp3") == 0.0)

print("== 2. the store adds it on write, the projection subtracts it on read ==")
lib = tmp / "collection.nml"
lib.write_text(SKELETON, encoding="utf-8")
ad = TraktorAdapter(lib)
ids = dict(zip(("xing", "noxing", "wav"), ad.add_tracks([
    NewTrack(track=Track(id="", title=p.stem), audio_path=p, cues=None) for p in (XING, NOXING, WAV)
])))
OFF = 2257 / 44.1  # ms


def native(tid):
    return ad.store.model_entry(tid)


def cue_start_ms(tid, slot):
    return next(c.start for c in native(tid).cue_v2 if c.hotcue == slot)


def marker_starts_ms(tid):
    return sorted(c.start for c in native(tid).cue_v2 if c.grid is not None)


for key, off in (("xing", OFF), ("noxing", 0.0), ("wav", 0.0)):
    tid = ids[key]
    tc = ad.set_cue(tid, slot=0, start_sec=1.0, cue_type="cue")
    check(f"{key}: a cue at 1.000 s is stored at START {1000 + off:.6f}",
          abs(cue_start_ms(tid, 0) - (1000 + off)) < 1e-9, str(cue_start_ms(tid, 0)))
    check(f"{key}: …and reads back at 1.000 s", abs(tc.cues[0].start - 1.0) < 1e-12, str(tc.cues[0].start))

tid = ids["xing"]
tc = ad.replace_grid(tid, [(0.5, 120.0), (1.5, 126.0)])
check("replace_grid shifts every marker", marker_starts_ms(tid) == [500 + OFF, 1500 + OFF], str(marker_starts_ms(tid)))
check("…and the projection undoes it", [round(m.start, 9) for m in tc.grid_markers] == [0.5, 1.5])
tc = ad.move_grid_marker(tid, 0, 0.25)
check("move_grid_marker shifts", abs(marker_starts_ms(tid)[0] - (250 + OFF)) < 1e-9, str(marker_starts_ms(tid)))
tc = ad.add_grid_marker(tid, 1.0)
check("add_grid_marker shifts", abs(marker_starts_ms(tid)[1] - (1000 + OFF)) < 1e-9, str(marker_starts_ms(tid)))
tc = ad.set_analysed_grid(tid, [(0.75, 124.0)])
comp = tc.grid_markers[0].companion
check("the analysed grid's companion sits on its marker",
      comp is not None and abs(cue_start_ms(tid, comp) - marker_starts_ms(tid)[0]) < 1e-9,
      f"slot {comp}, markers {marker_starts_ms(tid)}")

# A grid Traktor anchored INSIDE the header frame (the real collection has an
# AutoGrid at 21 ms): in decoded time it lies before the audio starts. It must
# read as a negative position, and the deck's Reset — which sends the markers
# back exactly as it loaded them — must leave the bytes alone. (No companion:
# Reset's `replace_grid` drops the old grid's companions by design.)
ad.delete_grid(tid)
for c in list(ad.track_cues(tid).cues):
    ad.delete_cue(tid, c.slot)
ad.replace_grid(tid, [(0.0, 172.0)])
ad.set_cue(tid, slot=3, start_sec=0.0, cue_type="cue")
for c in native(tid).cue_v2:  # as Traktor wrote them, both inside the header frame
    c.start = 20.979167 if c.grid is not None else 33.5
ad.store._sort_cues(native(tid))  # as a real file would be ordered
ad.store._sync_tempo(native(tid))
before = ad.snapshot()
tc = ad.track_cues(tid)
check("a Traktor anchor inside the header frame reads as negative, unclamped",
      abs(tc.grid_markers[0].start - (20.979167 - OFF) / 1000) < 1e-12, str(tc.grid_markers[0].start))
check("…as does a cue there", abs(tc.cues[0].start - (33.5 - OFF) / 1000) < 1e-12, str(tc.cues[0].start))
ad.replace_grid(tid, [(m.start, m.bpm) for m in tc.grid_markers])
ad.set_cue(tid, slot=3, start_sec=tc.cues[0].start, cue_type="cue")
check("…and writing the loaded grid and cue straight back is byte-identical", ad.snapshot() == before)

ad.save()
re_ad = TraktorAdapter(lib)
check("positions survive a save and reopen",
      abs(re_ad.track_cues(tid).grid_markers[0].start - (20.979167 - OFF) / 1000) < 1e-9)
(tmp / "xing.mp3").rename(tmp / "xing-moved.mp3")
gone = TraktorAdapter(lib).track_cues(tid)
check("with the file gone the offset is 0 (the raw START)",
      abs(gone.grid_markers[0].start - 0.020979167) < 1e-12, str(gone.grid_markers[0].start))

print("== 3. Traktor's own hand-placed cues land on the kick (kit 4) ==")
KIT = Path.home() / "Music/Konduktor Stem Test/Kit 4"
NML = Path.home() / "Documents/Native Instruments/Traktor 4.5.0/collection.nml"
if not (KIT.is_dir() and NML.is_file()):
    print("  (skipped: kit 4 or the local Traktor collection is not on this machine)")
else:
    xml = NML.read_text(encoding="utf-8")
    seen = 0
    for m in re.finditer(r"<ENTRY [^>]*>.*?</ENTRY>", xml, re.S):
        e = m.group(0)
        if "Kit 4" not in e:
            continue
        name = re.search(r'FILE="([^"]*)"', e).group(1)
        pad8 = re.search(r'<CUE_V2 [^>]*START="([^"]*)"[^>]*HOTCUE="7"', e)
        if not pad8 or not (KIT / name).is_file():
            continue
        with av.open(str(KIT / name), metadata_errors="ignore") as c:
            st = c.streams.audio[0]
            sr = st.rate
            y = np.concatenate([f.to_ndarray()[0] for f in c.decode(st)]).astype(np.float64)
        a = int(8.9 * sr)
        onset = (a + int(np.argmax(np.abs(y[a:a + int(0.2 * sr)]) > 0.05))) / sr
        decoded = timebase.from_traktor_ms(float(pad8.group(1)), timebase.offset_ms(KIT / name))
        err = (decoded - onset) * 1000
        seen += 1
        check(f"{name}: a cue placed in Traktor reads back on the kick (±1 ms)", abs(err) < 1.0, f"{err:+.2f} ms")
    if not seen:
        print("  (skipped: no hand-placed pad-8 cues in kit 4)")

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
