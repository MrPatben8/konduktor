"""Where loudness lives (`loudness_store.py`) and the routes that fill and read it.

Runs on a temp app-data dir, a temp copy of the real collection and FLACs
generated here. Pins the decisions that would otherwise fail silently:

  * values are keyed by PATH; a changed file keeps its value but reads STALE
    (dimmed, still sorted on) — never silently dropped, never silently trusted;
  * Konduktor's OWN tag write at Save does not stale a value;
  * a measurement is not an edit: the library stays clean and Discard keeps it;
  * loudness works where nothing can be written (a Files folder, a device),
    while BPM / Grid / Key for those are refused;
  * a path remap carries values to the files' new paths;
  * Analyze skips a measured track unless Replace is ticked, and the preview's
    counts match what the run does.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
_DATA = tempfile.TemporaryDirectory()
os.environ["KONDUKTOR_DATA_DIR"] = _DATA.name

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


from konduktor import loudness_store  # noqa: E402
from konduktor.core.adapter import AudioFacts  # noqa: E402
from konduktor.core.loudness import VERSION, Loudness  # noqa: E402
from konduktor.core.pathmap import PathMapping  # noqa: E402

V = Loudness(lufs=-8.0, rms=-7.5, true_peak=0.3, lra=5.0, plr=8.3)

print("== the store ==")
store = loudness_store.LocalLoudnessStore(Path(tempfile.mkdtemp()) / "l.sqlite3")
f = AudioFacts(key="/music/a.mp3", size=100, mtime_ns=5)
store.put(f, V)
got = store.get(["/music/a.mp3", "/music/none.mp3"])
check("a measured path answers, an unmeasured one does not", list(got) == ["/music/a.mp3"], str(got))
e = got["/music/a.mp3"]
check("values round-trip", e.values == V and e.version == VERSION)
check("fresh while the facts match", not e.stale(f))
check("stale once the size or mtime moves", e.stale(AudioFacts(f.key, 100, 6)) and e.stale(AudioFacts(f.key, 101, 5)))
check("an older measurement version is stale", loudness_store.Entry(V, 100, 5, VERSION - 1).stale(f))
check("keyed as asked, normalised underneath", "/music//a.mp3" in store.get(["/music//a.mp3"]))
n = store.remap(PathMapping.make("/music", "/Volumes/SSD/music"))
check("a remap moves values under the old prefix", n == 1 and "/Volumes/SSD/music/a.mp3" in store.get(
    ["/Volumes/SSD/music/a.mp3"]) and not store.get(["/music/a.mp3"]))
store.put(AudioFacts("/x/b.mp3", 1, 1), V)
store.put(AudioFacts("/y/b.mp3", 2, 2), V)
store.remap(PathMapping.make("/x", "/y"))
check("…a value already at the new path wins", store.get(["/y/b.mp3"])["/y/b.mp3"].size == 2
      and not store.get(["/x/b.mp3"]))
check("…and only under the prefix, segment-aware",
      store.remap(PathMapping.make("/Volumes/SS", "/elsewhere")) == 0)
errors = []


def burst(k):
    try:
        for i in range(50):
            store.put(AudioFacts(f"/t/{k}/{i}.mp3", i, i), V)
    except Exception as ex:  # noqa: BLE001
        errors.append(ex)


threads = [threading.Thread(target=burst, args=(k,)) for k in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("concurrent writers lose nothing", not errors and len(store.get(
    [f"/t/{k}/{i}.mp3" for k in range(8) for i in range(50)])) == 400, str(errors[:1]))


# ---- generated audio ------------------------------------------------------
def tone_flac(path: Path, amp: float, seconds: float = 8.0, **tags) -> Path:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(44100 * seconds)) / 44100
    x = (amp * np.sin(2 * np.pi * 440 * t)).astype("float32")
    sf.write(path, np.stack([x, x], axis=1), 44100, format="FLAC")
    audio = FLAC(path)
    for k, v in tags.items():
        audio[k] = str(v)
    audio.save()
    return path


root = Path(tempfile.mkdtemp())
music = root / "Loose"
loud = tone_flac(music / "loud.flac", 0.5, title="Loud One")
quiet = tone_flac(music / "quiet.flac", 0.05, title="Quiet One")
short = tone_flac(music / "short.flac", 0.5, seconds=2.0, title="Too Short")

work = Path(tempfile.mkdtemp()) / "collection.nml"
shutil.copy2(REAL, work)
os.environ["KONDUKTOR_NML"] = str(work)

from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor.app_state import STATE  # noqa: E402


def wait(c, job):
    while job["state"] == "running":
        time.sleep(0.05)
        job = c.get(f"/api/jobs/{job['id']}").json()
    return job


with TestClient(main.app, raise_server_exceptions=False) as c:
    a = main.require_adapter()
    files = [str(loud), str(quiet), str(short)]
    c.get("/api/folder/tracks", params={"path": str(music)})

    print("== a Files folder: loudness only ==")
    check("nothing is measured yet",
          c.post("/api/loudness", json={"origin": "folder", "track_ids": files}).json()["values"] == {})
    p = c.post("/api/tracks/analyze/preview", json={
        "origin": "folder", "track_ids": files, "bpm": False, "grid": False, "loudness": True}).json()
    check("the preview counts three to measure", (p["total"], p["loudness_measure"], p["tracks_to_analyze"])
          == (3, 3, 3), str(p))
    r = c.post("/api/tracks/analyze", json={"origin": "folder", "track_ids": files, "bpm": True, "grid": False})
    check("BPM for a folder's files is refused", r.status_code == 400, r.text)
    r = c.post("/api/tracks/analyze", json={"origin": "folder", "track_ids": files, "bpm": False,
                                            "grid": False, "loudness": False})
    check("nothing ticked is refused", r.status_code == 400)
    job = wait(c, c.post("/api/tracks/analyze", json={
        "origin": "folder", "track_ids": files, "bpm": False, "grid": False, "loudness": True}).json())
    res = job["result"]
    check("the job finishes", job["state"] == "done", str(job))
    check("two measured, the 2-second file has nothing to measure",
          res["loudness"] == {"measured": 2, "existing": 0, "none": 1}, str(res))
    vals = c.post("/api/loudness", json={"origin": "folder", "track_ids": files}).json()["values"]
    check("the table reads both", set(vals) == {str(loud), str(quiet)}, str(vals))
    check("the louder file reads 20 dB louder",
          abs((vals[str(loud)]["lufs"] - vals[str(quiet)]["lufs"]) - 20.0) < 0.1, str(vals))
    check("a -6 dBFS sine's RMS is -6 dB", abs(vals[str(loud)]["rms"] + 6.02) < 0.05, str(vals[str(loud)]))
    check("fresh", not vals[str(loud)]["stale"])
    check("the library is not dirty: a measurement is not an edit", not c.get("/api/state").json()["dirty"])

    print("== skip measured, unless Replace ==")
    body = {"origin": "folder", "track_ids": files, "bpm": False, "grid": False, "loudness": True}
    p = c.post("/api/tracks/analyze/preview", json=body).json()
    check("measured tracks are skipped", (p["loudness_existing"], p["with_loudness"], p["loudness_measure"])
          == (2, 2, 1), str(p))
    job = wait(c, c.post("/api/tracks/analyze", json=body).json())
    check("…and the run agrees", job["result"]["loudness"] == {"measured": 0, "existing": 2, "none": 1},
          str(job["result"]))
    p = c.post("/api/tracks/analyze/preview", json={**body, "replace_loudness": True}).json()
    check("Replace measures them again", (p["loudness_existing"], p["loudness_measure"]) == (0, 3), str(p))

    print("== a changed file reads stale, and is offered again ==")
    was = vals[str(quiet)]["lufs"]
    st = os.stat(quiet)
    os.utime(quiet, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    vals = c.post("/api/loudness", json={"origin": "folder", "track_ids": files}).json()["values"]
    check("stale, value kept", vals[str(quiet)]["stale"] and vals[str(quiet)]["lufs"] == was,
          str(vals[str(quiet)]))
    p = c.post("/api/tracks/analyze/preview", json=body).json()
    check("the preview offers it again", p["loudness_measure"] == 2, str(p))

    print("== the deck's Analyze where nothing can be written ==")
    r = c.post("/api/tracks/loudness", json={"origin": "folder", "track_id": str(quiet)})
    check("measures and answers the values", r.status_code == 200 and not r.json()["stale"], r.text)
    vals = c.post("/api/loudness", json={"origin": "folder", "track_ids": [str(quiet)]}).json()["values"]
    check("…and the value is fresh again", not vals[str(quiet)]["stale"])
    r = c.post("/api/tracks/loudness", json={"origin": "folder", "track_id": str(short)})
    check("too short to measure is a 400 with a reason", r.status_code == 400 and "3 seconds" in r.text, r.text)
    r = c.post("/api/tracks/loudness", json={"origin": "folder", "track_id": "/etc/passwd"})
    check("a file no scan found is a 404", r.status_code == 404)

    print("== in the collection: the same file, the same value ==")
    job = wait(c, c.post("/api/folder/add", json={"track_ids": [str(loud)], "mode": "reference"}).json())
    tid = job["result"]["track_ids"][0]
    vals = c.post("/api/loudness", json={"origin": "collection", "track_ids": [tid]}).json()["values"]
    check("the collection track shows the folder's measurement", tid in vals and not vals[tid]["stale"], str(vals))
    held = c.post("/api/loudness", json={"origin": "folder", "track_ids": [tid]}).json()["values"]
    check("a folder view's held row (a collection id) resolves too", tid in held, str(held))

    print("== Konduktor's own tag write does not stale it ==")
    r = c.patch("/api/tracks", json={"track_id": tid, "fields": {"title": "Loud Renamed"}})
    check("edit accepted", r.status_code == 200, r.text)
    before = os.stat(loud).st_mtime_ns
    r = c.post("/api/save")
    check("saved", r.status_code == 200, r.text)
    check("the save wrote the file's tags", os.stat(loud).st_mtime_ns != before)
    vals = c.post("/api/loudness", json={"origin": "collection", "track_ids": [tid]}).json()["values"]
    check("…and the value is still fresh", tid in vals and not vals[tid]["stale"], str(vals))

    print("== Discard keeps measurements ==")
    c.patch("/api/tracks", json={"track_id": tid, "fields": {"comment": "dirty"}})
    r = c.post("/api/tracks/loudness", json={"origin": "collection", "track_id": tid})
    check("measured while dirty", r.status_code == 200, r.text)
    check("discarded", c.post("/api/discard").status_code == 200)
    vals = c.post("/api/loudness", json={"origin": "collection", "track_ids": [tid]}).json()["values"]
    check("the value survived", tid in vals, str(vals))

    print("== the collection's Analyze measures alongside the grid ==")
    p = c.post("/api/tracks/analyze/preview", json={
        "track_ids": [tid], "bpm": False, "grid": False, "key": False, "loudness": True,
        "replace_loudness": True}).json()
    check("collection preview counts it", p["loudness_measure"] == 1 and p["with_loudness"] == 1, str(p))

    print("== a path remap carries values to the new paths ==")
    moved = root / "Moved"
    shutil.move(str(music), str(moved))
    r = c.post("/api/library/remap-paths", json={"from": str(music), "to": str(moved)})
    check("remapped", r.status_code == 200 and r.json()["rewritten"] >= 1, r.text)
    new_id = next(t.id for t in a.tracks if a.audio_path(t.id) == moved / "loud.flac")
    vals = c.post("/api/loudness", json={"origin": "collection", "track_ids": [new_id]}).json()["values"]
    check("the moved file keeps its value, fresh (a move keeps size and mtime)",
          new_id in vals and not vals[new_id]["stale"], str(vals))

    print("== a device: loudness only, failures reported per track ==")
    fixture = Path(tempfile.mkdtemp()) / "onelibrary"
    shutil.copytree(FIXTURE, fixture)
    check("the stick opens as a source", c.post("/api/source/open", json={"path": str(fixture)}).status_code == 200)
    ids = [t["id"] for t in c.get("/api/source/tracks", params={"limit": 3}).json()["items"]]
    r = c.post("/api/tracks/analyze", json={"origin": "device", "track_ids": ids, "bpm": False,
                                            "grid": False, "key": True})
    check("a key for a device's tracks is refused", r.status_code == 400, r.text)
    job = wait(c, c.post("/api/tracks/analyze", json={"origin": "device", "track_ids": ids, "bpm": False,
                                                      "grid": False, "loudness": True}).json())
    # The fixture's demo MP3s are 12-byte stubs: each is one reported failure.
    check("the job finishes, each undecodable stub reported, not fatal",
          job["state"] == "done" and len(job["result"]["failed"]) == len(ids), str(job)[:300])

print()
if failed:
    print("FAILED")
    raise SystemExit(1)
print("All loudness store checks passed.")
