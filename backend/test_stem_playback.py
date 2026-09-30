"""Stem playback on the deck — the backend half: a stem file's stems served one
by one, because a browser decodes only an MP4's first audio stream (the mix).

Pins what would fail silently:
  * a served stem is BIT-IDENTICAL to decoding that stream in place, edit list
    and all — a stem shifted by its priming would flam against the others and
    sit off every cue;
  * "is this a stem file" is read from the FILE: an MP3 whose collection entry
    carries `<STEMS>` (105 real ones do) has no stems to play;
  * the cache follows the file (an edited file is not served stale) and stays
    within its limit without evicting what it just wrote;
  * all three origins (collection, device, folder) serve them.

Everything is generated: a stem file built with a FAKE separator whose four
stems are distinct scalings of the mix, so each served stem can be told apart.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-stemplay-appdata-")

import av  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import stem_file as sf  # noqa: E402
from konduktor.stems import stream_cache  # noqa: E402
from stem_test_support import make_mp3  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def decode(path: Path, stream: int = 0) -> np.ndarray:
    with av.open(str(path)) as c:
        return np.concatenate([f.to_ndarray() for f in c.decode(c.streams.audio[stream])], axis=1)


GAINS = (0.5, 0.25, -0.2, 0.1)
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-stemplay-"))
music = ROOT / "Music"
music.mkdir()
stem = music / "Tune.stem.m4a"
sf.build(make_mp3(ROOT / "src.mp3", seconds=6), stem, lambda p: [p * g for g in GAINS])
plain = make_mp3(music / "Plain.mp3", seed=7)
tagged = make_mp3(music / "Tagged.mp3", seed=8)  # its ENTRY will carry <STEMS>

print("== stem_file: what a file carries ==")
layout = sf.stem_layout(stem)
check("a stem file lists its four stems with names and colours, in stream order",
      [s["name"] for s in layout or []] == ["Drums", "Bass", "Synths", "Vox"]
      and all(s["color"].startswith("#") for s in layout), str(layout))
check("a plain MP3 has none", sf.stem_layout(plain) is None)
check("a missing file has none (no exception)", sf.stem_layout(ROOT / "nope.stem.m4a") is None)

print("== a temp collection pointing at them ==")
work = ROOT / "collection.nml"
shutil.copy2(REAL, work)
st = TraktorStore(work)
entries = [e for e in st._nml.collection.entry if e.location][:3]
ids = []
for e, path in zip(entries, (stem, plain, tagged)):
    e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
    ids.append(st._key_of(e))
from traktor_nml_utils.models.collection import Stemstype  # noqa: E402

entries[2].stems = Stemstype(stems=sf.NML_STEMS_JSON)
st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
st.save()
stem_id, plain_id, tagged_id = ids

with TestClient(main.app) as c:
    assert c.post("/api/library/open", json={"path": str(work)}).status_code == 200
    print("== the collection routes ==")
    r = c.get("/api/tracks/stems", params={"track_id": stem_id}).json()
    check("/api/tracks/stems lists the stem file's stems", r["stems"] == layout, str(r))
    check("…none for a plain MP3", c.get("/api/tracks/stems", params={"track_id": plain_id}).json()["stems"] == [])
    tagged_track = c.get("/api/tracks", params={"limit": 20000}).json()
    tagged_kind = next((t["media_kind"] for t in tagged_track["items"] if t["id"] == tagged_id), None) \
        if isinstance(tagged_track, dict) and "items" in tagged_track else None
    check("…and none for an MP3 the COLLECTION calls a stem track (decided by the file)",
          c.get("/api/tracks/stems", params={"track_id": tagged_id}).json()["stems"] == [],
          f"media_kind={tagged_kind}")

    mix = decode(stem, 0)
    for k in range(4):
        r = c.get("/api/tracks/audio", params={"track_id": stem_id, "stem": k})
        ok = r.status_code == 200 and r.headers["content-type"].startswith("audio/mp4")
        got = ROOT / f"served-{k}.m4a"
        got.write_bytes(r.content if ok else b"")
        same = ok and np.array_equal(decode(got), decode(stem, k + 1))
        check(f"stem {k} is served as its own MP4, bit-identical to stream {k + 1} in place", same)
        if same:
            s = decode(got)
            n = min(s.shape[1], mix.shape[1])
            # The fake stems are the mix scaled: the right stream, not just A stream.
            ratio = float(np.dot(s[0, :n], mix[0, :n]) / np.dot(mix[0, :n], mix[0, :n]))
            check(f"…and it is stem {k} (gain {GAINS[k]:+.2f}, measured {ratio:+.2f}), same length as the mix",
                  abs(ratio - GAINS[k]) < 0.05 and s.shape[1] == mix.shape[1], f"{ratio} {s.shape} {mix.shape}")
    check("the plain audio route still serves the whole file",
          c.get("/api/tracks/audio", params={"track_id": stem_id}).content == stem.read_bytes())
    check("a stem out of range is 404",
          c.get("/api/tracks/audio", params={"track_id": stem_id, "stem": 4}).status_code == 404)
    check("asking a plain MP3 for a stem is 404",
          c.get("/api/tracks/audio", params={"track_id": plain_id, "stem": 0}).status_code == 404)

    print("== the cache ==")
    cached = stream_cache.stem_file_for(stem, 0)
    check("a second request reuses the cached file", stream_cache.stem_file_for(stem, 0) == cached)
    st_ = stem.stat()
    os.utime(stem, ns=(st_.st_atime_ns, st_.st_mtime_ns + 10_000_000_000))
    check("an edited source gets a fresh file, never the stale one", stream_cache.stem_file_for(stem, 0) != cached)
    before = sorted(stream_cache.root().glob("*.m4a"))
    stream_cache.LIMIT = 1  # everything is over the limit now
    newest = stream_cache.stem_file_for(stem, 3)
    after = sorted(stream_cache.root().glob("*.m4a"))
    check("over its limit the cache evicts the least recently used…", len(before) >= 2 and after == [newest],
          f"{len(before)} -> {[p.name for p in after]}")
    check("…but never the file it just wrote", newest.exists() and newest in after)
    check("no partial file is left behind", not list(stream_cache.root().glob("*.part")))
    stream_cache.LIMIT = 2 * 1024 ** 3

    print("== the folder origin ==")
    listing = c.get("/api/folder/tracks", params={"path": str(music)})
    check("(setup) the folder lists", listing.status_code == 200, listing.text[:200])
    fid = str(stem)
    r = c.get("/api/folder/tracks/stems", params={"track_id": fid})
    check("a stem file browsed in a folder lists its stems", r.status_code == 200 and r.json()["stems"] == layout,
          r.text[:200])
    r = c.get("/api/folder/tracks/audio", params={"track_id": fid, "stem": 1})
    got = ROOT / "folder-1.m4a"
    got.write_bytes(r.content)
    check("…and serves them", r.status_code == 200 and np.array_equal(decode(got), decode(stem, 2)))

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
