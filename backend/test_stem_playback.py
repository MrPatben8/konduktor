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

import html
import os
import re
import shutil
import sys
import tempfile
import time
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
    kinds = {t["id"]: t["media_kind"] for t in listing.json()["tracks"]}
    check("the listing marks the stem file as a stem track and the MP3 as audio",
          kinds.get(str(stem)) == "stem" and kinds.get(str(plain)) == "audio", str(kinds))

    print("== adding a stem file to the collection ==")
    extra_dir = ROOT / "More"
    extra_dir.mkdir()
    extra = extra_dir / "Other.stem.m4a"
    shutil.copy2(stem, extra)
    extra_plain = make_mp3(extra_dir / "Other.mp3", seed=9)
    assert c.get("/api/folder/tracks", params={"path": str(extra_dir)}).status_code == 200
    pl_id = c.post("/api/playlists", json={"name": "Stem Adds"}).json()["id"]
    job = c.post("/api/folder/add", json={"track_ids": [str(extra), str(extra_plain)],
                                          "mode": "reference", "playlist_id": pl_id}).json()
    while job["state"] == "running":
        time.sleep(0.05)
        job = c.get(f"/api/jobs/{job['id']}").json()
    check("(setup) the add finishes", job["state"] == "done", str(job))
    new_stem, new_plain = job["result"]["track_ids"]
    kinds = {t["id"]: t["media_kind"] for t in c.get("/api/tracks", params={"limit": 20000}).json()["items"]}
    check("the added stem file is a stem track, the MP3 beside it audio",
          kinds.get(new_stem) == "stem" and kinds.get(new_plain) == "audio", f"{kinds.get(new_stem)} {kinds.get(new_plain)}")
    saved = work.read_text(encoding="utf-8")

    def entry_of(name: str) -> str:
        at = saved.find(f'FILE="{name}"')
        return saved[at:saved.find("</ENTRY>", at)] if at >= 0 else ""

    m = re.search(r'<STEMS STEMS="([^"]*)"', entry_of(extra.name))
    check("…its saved ENTRY carries <STEMS>, rendered as Traktor renders that file's box",
          m is not None and html.unescape(m.group(1)) == sf.NML_STEMS_JSON, entry_of(extra.name)[:300])
    check("…the MP3's carries none", entry_of(extra_plain.name) != "" and "<STEMS" not in entry_of(extra_plain.name))
    check('…and the playlist keys the stem file TYPE="STEM", the MP3 TYPE="TRACK"',
          re.search(r'<PRIMARYKEY TYPE="STEM" KEY="[^"]*Other\.stem\.m4a"', saved) is not None
          and re.search(r'<PRIMARYKEY TYPE="TRACK" KEY="[^"]*Other\.mp3"', saved) is not None)

    # Rekordbox has no <STEMS>: the FILE decides, so the Type column agrees
    # with the deck, which plays a stem file's stems on every platform.
    print("== adding a stem file to a Rekordbox library ==")
    from konduktor.adapters.rekordbox import discovery as rb_discovery

    found = rb_discovery.detect_libraries()
    if not found:
        print("  (skipped: no Rekordbox library on this machine)")
    else:
        rb_src = Path(found[0]["path"]).parent
        rb_dir = ROOT / "rekordbox"
        rb_dir.mkdir()
        for name in ("master.db", "masterPlaylists6.xml"):
            if (rb_src / name).exists():
                shutil.copy2(rb_src / name, rb_dir / name)
        if (rb_src / "share").is_dir():
            shutil.copytree(rb_src / "share", rb_dir / "share")
        rb_stem_dir = ROOT / "ForRekordbox"
        rb_stem_dir.mkdir()
        rb_stem = rb_stem_dir / "Rb.stem.m4a"
        shutil.copy2(stem, rb_stem)
        rb_plain = make_mp3(rb_stem_dir / "Rb.mp3", seed=11)
        r = c.post("/api/library/open", json={"path": str(rb_dir / "master.db")})
        check("(setup) a Rekordbox copy opens", r.status_code == 200, r.text[:300])
        assert c.get("/api/folder/tracks", params={"path": str(rb_stem_dir)}).status_code == 200
        job = c.post("/api/folder/add", json={"track_ids": [str(rb_stem), str(rb_plain)],
                                              "mode": "reference"}).json()
        while job["state"] == "running":
            time.sleep(0.05)
            job = c.get(f"/api/jobs/{job['id']}").json()
        check("(setup) the add finishes", job["state"] == "done", str(job))
        rb_new_stem, rb_new_plain = (job.get("result") or {}).get("track_ids") or [None, None]

        def rb_kinds():
            items = c.get("/api/tracks", params={"limit": 20000}).json()["items"]
            return {t["id"]: t["media_kind"] for t in items}

        kinds = rb_kinds()
        check("the added stem file is a stem track, the MP3 beside it audio",
              kinds.get(rb_new_stem) == "stem" and kinds.get(rb_new_plain) == "audio",
              f"{kinds.get(rb_new_stem)} {kinds.get(rb_new_plain)}")
        r = c.post("/api/library/open", json={"path": str(rb_dir / "master.db")})
        kinds = rb_kinds()
        check("…and still after a reopen",
              kinds.get(rb_new_stem) == "stem" and kinds.get(rb_new_plain) == "audio",
              f"{kinds.get(rb_new_stem)} {kinds.get(rb_new_plain)}")
        r = c.get("/api/tracks/stems", params={"track_id": rb_new_stem})
        check("and the deck is offered its stems",
              r.status_code == 200 and len(r.json()["stems"]) == 4, r.text[:200])

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
