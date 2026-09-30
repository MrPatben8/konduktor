"""Swapping converted stem files into a Traktor collection (`apply_stem_swaps`).

Against a temp copy of the real collection, with a real playlisted ENTRY pointed
at a generated MP3 (which HAS a Xing/Info header, so Traktor's clock runs 2257
samples behind its decoded audio) and a real stem file built from it. Pins:

  * **the prep does not move in the decoded time base** — every cue and marker
    reads back at the same second — while its stored START shifts by exactly
    the MP3's offset, read from the PARKED original;
  * a repoint changes only that ENTRY and the PRIMARYKEYs naming it (now
    `TYPE="STEM"`), byte for byte, and round-trips through a save;
  * a grid anchor pushed before 0 moves by whole beats with its companion, a
    hotcue there is clamped and reported;
  * "add" appends an entry and leaves the original's bytes alone;
  * a clash is refused with nothing changed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-stemswap-appdata-")

import av  # noqa: E402
import numpy as np  # noqa: E402

from konduktor.adapters.traktor import timebase  # noqa: E402
from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import stem_file as sf  # noqa: E402
from konduktor.core.adapter import InvalidCommand, StemSwap  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def make_mp3(path: Path, seconds=6):
    sr = 44100
    rng = np.random.default_rng(3)
    x = np.zeros((2, sr * seconds), np.float32)
    for start in range(sr // 2, x.shape[1] - sr // 10, sr // 2):
        x[:, start:start + 2000] = rng.uniform(-0.5, 0.5, (2, 2000))
    with av.open(str(path), "w", format="mp3") as c:
        s = c.add_stream("libmp3lame", rate=sr, layout="stereo")
        s.bit_rate = 320_000
        for o in range(0, x.shape[1], 1152):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1152]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = sr, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def tag_lines(b: bytes) -> list[str]:
    return [("<" + t).strip() for t in b.decode("utf-8").split("<") if t.strip()]


def fake_separate(pcm):
    return [pcm * g for g in (0.5, 0.25, -0.2, 0.1)]


def key_of(path: Path) -> str:
    return "".join(os_path_to_location(path))


def setup(tmp: Path, name: str):
    """A temp collection whose first playlisted track now points at an MP3 with
    a header, carrying a hotcue at 3.000 s and a grid anchored at 0.500 s (both
    in DECODED time), saved. Returns (work nml, track id, mp3 path)."""
    work = tmp / "collection.nml"
    shutil.copy2(REAL, work)
    mp3 = tmp / f"{name}.mp3"
    make_mp3(mp3)
    st = TraktorStore(work)
    in_pl = {k for n in st._iter_nodes(st._root()) if n.playlist for k in st._entry_keys_of(n.playlist)}
    entry = next(e for e in st._nml.collection.entry
                 if e.location and st._key_of(e) in in_pl and e.stems is None)
    old = st._key_of(entry)
    entry.location.volume, entry.location.dir, entry.location.file = os_path_to_location(mp3)
    new = st._key_of(entry)
    for n in st._iter_nodes(st._root()):
        for pe in (n.playlist.entry if n.playlist else None) or []:
            if pe.primarykey and pe.primarykey.key == old:
                pe.primarykey.key = new
    st._entry_by_key = {st._key_of(e): e for e in st._nml.collection.entry if e.location}
    entry.cue_v2 = []
    st.set_hotcue(new, 1, 3.0, 0, name="Drop")
    st.replace_grid(new, [(0.5, 120.0)])
    st.save()
    return work, new, mp3


def build_stem(mp3: Path) -> sf.Written:
    target = mp3.with_name(sf.stem_target_name(mp3.name))
    return sf.build(mp3, target, fake_separate, encoder="aac")


def park(mp3: Path) -> Path:
    parked = mp3.with_name(f".{mp3.name}.konduktor-parked")
    os.replace(mp3, parked)
    return parked


OFF = 2257 / 44.1  # ms: the generated MP3's Traktor offset

print("== 1. repoint: prep keeps its place, only the ENTRY + its keys change ==")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work, tid, mp3 = setup(tmp, "Song")
    check("(setup) the MP3 has Traktor's 2257-sample offset", abs(timebase.offset_ms(mp3) - OFF) < 1e-9)
    ad = TraktorAdapter(work)
    before_cues = ad.track_cues(tid)
    before = ad.snapshot()
    written = build_stem(mp3)
    parked = park(mp3)
    stem = written.path
    res = ad.apply_stem_swaps([StemSwap(tid, stem, "repoint", parked, written.bit_rate, written.duration, written.size)])
    new = key_of(stem)
    check("the result renames the track to the stem file's id", res.renamed == {tid: new}, str(res.renamed))
    check("nothing was clamped", res.clamped == {})
    entry = ad.store.model_entry(new)
    check("the entry names the stem file", entry is not None and entry.location.file == stem.name)
    check("it carries Traktor's <STEMS> value", entry.stems is not None and entry.stems.stems == sf.NML_STEMS_JSON)
    check("INFO describes the new file",
          (entry.info.bitrate, entry.info.playtime, entry.info.filesize)
          == (written.bit_rate, round(written.duration), round(written.size / 1024)),
          f"{entry.info.bitrate} {entry.info.playtime} {entry.info.filesize}")
    check("the projection calls it a stem track", ad.track(new).media_kind == "stem")
    after_cues = ad.track_cues(new)
    check("every cue reads back at the same DECODED second",
          [(c.slot, round(c.start, 9)) for c in after_cues.cues] == [(c.slot, round(c.start, 9)) for c in before_cues.cues],
          f"{[(c.slot, c.start) for c in after_cues.cues]} vs {[(c.slot, c.start) for c in before_cues.cues]}")
    check("…and so does the grid",
          [round(m.start, 9) for m in after_cues.grid_markers] == [round(m.start, 9) for m in before_cues.grid_markers])
    drop = next(c for c in entry.cue_v2 if c.hotcue == 1)
    marker = next(c for c in entry.cue_v2 if c.grid is not None)
    check("the stored START moved by the MP3's offset (3051.18 -> 3000.00 ms)", abs(drop.start - 3000.0) < 1e-6, str(drop.start))
    check("…the marker too (551.18 -> 500.00 ms)", abs(marker.start - 500.0) < 1e-6, str(marker.start))

    after = ad.snapshot()
    b_lines, a_lines = tag_lines(before), tag_lines(after)
    # Everything outside this ENTRY must be identical once the one key is renamed.
    def outside(lines, file_name):
        i = next(n for n, l in enumerate(lines) if l.startswith("<LOCATION") and f'FILE="{file_name}"' in l)
        start = max(n for n in range(i) if lines[n].startswith("<ENTRY "))
        end = next(n for n in range(i, len(lines)) if lines[n] == "</ENTRY>")
        return lines[:start] + lines[end + 1:]
    rest_b = outside(b_lines, mp3.name)
    rest_a = [l.replace(f'TYPE="STEM" KEY="{new}"', f'TYPE="TRACK" KEY="{tid}"') for l in outside(a_lines, stem.name)]
    check("outside the ENTRY only its PRIMARYKEYs changed (TYPE STEM, new key)", rest_a == rest_b,
          f"{len(rest_a)} vs {len(rest_b)} lines")
    check("…and there is at least one such PRIMARYKEY", any(f'KEY="{new}"' in l for l in a_lines))
    check("the history message says what happened", "converted 1 track to stems" in ad.store._edit_summary().lower(),
          ad.store._edit_summary())
    ad.save()
    re_ad = TraktorAdapter(work)
    check("after a save the stem entry is a STEM for playlists", new in re_ad.store.stem_keys())
    check("…and its prep still reads back unmoved",
          [round(c.start, 6) for c in re_ad.track_cues(new).cues] == [round(c.start, 6) for c in before_cues.cues])

print("== 2. the offset must come from the PARKED file ==")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work, tid, mp3 = setup(tmp, "Song")
    ad = TraktorAdapter(work)
    written = build_stem(mp3)
    park(mp3)
    ad.apply_stem_swaps([StemSwap(tid, written.path, "repoint", mp3, written.bit_rate, written.duration, written.size)])
    drop = next(c for c in ad.store.model_entry(key_of(written.path)).cue_v2 if c.hotcue == 1)
    check("(the failure this guards against) read from the OLD path, nothing shifts — the cue lands 51 ms late",
          abs(drop.start - (3000.0 + OFF)) < 1e-6, str(drop.start))

print("== 3. positions pushed before 0 ==")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work, tid, mp3 = setup(tmp, "Early")
    st = TraktorStore(work)
    e = st.model_entry(tid)
    marker = next(c for c in e.cue_v2 if c.grid is not None)
    marker.start = 20.979167  # a Traktor AutoGrid inside the header frame
    st.place_grid_companion(tid, 0, prefer_slot=0)
    next(c for c in e.cue_v2 if c.hotcue == 1).start = 10.0  # a hotcue before the audio
    st._sort_cues(e)
    st.save()
    ad = TraktorAdapter(work)
    written = build_stem(mp3)
    parked = park(mp3)
    res = ad.apply_stem_swaps([StemSwap(tid, written.path, "repoint", parked, written.bit_rate, written.duration, written.size)])
    new = key_of(written.path)
    e = ad.store.model_entry(new)
    marker = next(c for c in e.cue_v2 if c.grid is not None)
    comp = next(c for c in e.cue_v2 if c.hotcue == 0)
    expect = 20.979167 - OFF + 500.0  # one beat at 120 BPM later: the same grid
    check("the anchor moved forward by a whole beat, not clamped", abs(marker.start - expect) < 1e-6, str(marker.start))
    check("its companion moved with it, as the same float", comp.start == marker.start)
    check("the hotcue before the audio is clamped to 0 and reported",
          next(c for c in e.cue_v2 if c.hotcue == 1).start == 0.0 and res.clamped == {new: [1]}, str(res.clamped))

print("== 4. add: a new entry, the original untouched ==")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work, tid, mp3 = setup(tmp, "Song")
    ad = TraktorAdapter(work)
    ad.set_track_metadata(tid, {"genre": "Pending edit"})
    original_bytes = [l for l in tag_lines(ad.snapshot())]
    n_before = len(ad.store._nml.collection.entry)
    written = build_stem(mp3)
    playlist = ad.create_playlist("Stems")
    res = ad.apply_stem_swaps([StemSwap(tid, written.path, "add", mp3, written.bit_rate, written.duration, written.size)],
                              add_to_playlist=playlist)
    new = key_of(written.path)
    check("an entry was appended", len(ad.store._nml.collection.entry) == n_before + 1
          and ad.store._nml.collection.entries == n_before + 1)
    check("the original entry is still there, unchanged, not a stem",
          ad.store.model_entry(tid) is not None and ad.store.model_entry(tid).stems is None)
    check("the new one is a stem, with the original's prep",
          ad.track(new).media_kind == "stem"
          and [round(c.start, 9) for c in ad.track_cues(new).cues] == [round(c.start, 9) for c in ad.track_cues(tid).cues])
    check("…and it is in the chosen playlist as a STEM", ad.playlist_entries(playlist) == [new]
          and new in ad.store.stem_keys())
    check("the pending tag edit will also reach the new file on Save", ad.store._journal.fields_for(new) == {"genre"})

print("== 5. refusals change nothing ==")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work, tid, mp3 = setup(tmp, "Song")
    ad = TraktorAdapter(work)
    written = build_stem(mp3)
    before = ad.snapshot()
    other = next(t.id for t in ad.tracks if t.id != tid)
    try:
        ad.apply_stem_swaps([
            StemSwap(tid, written.path, "repoint", mp3, written.bit_rate, written.duration, written.size),
            StemSwap(other, written.path, "repoint", mp3, written.bit_rate, written.duration, written.size),
        ])
        check("two tracks onto one stem file are refused", False, "it ran")
    except InvalidCommand:
        check("two tracks onto one stem file are refused", ad.snapshot() == before and not ad.dirty)
    ad.apply_stem_swaps([StemSwap(tid, written.path, "repoint", mp3, written.bit_rate, written.duration, written.size)])
    try:
        ad.apply_stem_swaps([StemSwap(other, written.path, "add", mp3, written.bit_rate, written.duration, written.size)])
        check("a stem file the collection already holds is refused", False, "it ran")
    except InvalidCommand:
        check("a stem file the collection already holds is refused", True)
    check("Traktor advertises stem conversion", ad.capabilities().tracks.stem_convertible)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
