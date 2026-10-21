"""Pushing an export set into this computer's Traktor / rekordbox library
(`local_push.py`), through the routes.

The source library is a temp copy of the real collection pointed at GENERATED
audio; the "local" Traktor collection pushed into is ANOTHER temp copy of the
real one — the user's own 8,485 entries — and the shared audio folder is a temp
folder. Prefs and app-data are redirected, and whether a DJ app is running is
faked (a real Traktor may be open on the machine running this).

What would fail SILENTLY, and matter at a gig:

  * a merge touches nothing of the user's own: existing entries and playlists
    render byte-identically, only new ENTRYs and the set's folder appear;
  * a re-push of an unchanged set copies and writes nothing;
  * the source wins: an edited cue or title reaches the pushed entry, and a
    re-levelled copy is swapped in with the entry's size following;
  * removal follows the set, but never takes a track another set holds, and a
    rekordbox refusal keeps the track and reports it;
  * the set's folder never takes over a folder of the user's with its name;
  * overwrite leaves only this set, deleting the other sets' files;
  * a running DJ app, the open library, and a failure mid-copy all leave the
    library as it was.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-push-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import av  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import history, local_push, paths, prefs  # noqa: E402
from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import apps_running  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-push-"))
AUDIO = ROOT / "Music" / "Konduktor"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def make_mp3(path: Path, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 44100
    t = np.arange(sr * 8) / sr
    rng = np.random.default_rng(seed)
    beat = (np.sin(2 * np.pi * 2 * t) > 0.6).astype(float)
    x = np.vstack([0.3 * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.2 * rng.standard_normal(t.size))
                   for _ in range(2)]).astype(np.float32)
    with av.open(str(path), "w", format="mp3") as c:
        s = c.add_stream("libmp3lame", rate=sr, layout="stereo")
        s.bit_rate = 192_000
        for o in range(0, x.shape[1], 1152):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1152]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = sr, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    return path


def source_library(n=5):
    d = ROOT / "source"
    d.mkdir(parents=True)
    work = d / "collection.nml"
    shutil.copy2(REAL, work)
    st = TraktorStore(work)
    entries = [e for e in st._nml.collection.entry if e.location and e.stems is None]
    ids, files = [], []
    for i, e in enumerate(entries[:n]):
        path = make_mp3(d / "Music" / f"track{i}.mp3", seed=i)
        e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
        e.cue_v2 = []
        e.title = f"Track {i}"
        st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
        ids.append(st._key_of(e))
        files.append(path)
    st.save()
    return work, ids, files


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def wait(c, jid, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def copy_of(source: Path) -> Path:
    return AUDIO / local_push._rel_for(source)


def pushed(target: Path) -> dict[str, object]:
    """The target's entries that live in the shared audio folder, by file name."""
    st = TraktorStore(target)
    out = {}
    for e in st._nml.collection.entry:
        path = st.audio_path(st._key_of(e)) if e.location else None
        if path and AUDIO in Path(path).parents:
            out[Path(path).name] = e
    return out


def top_level(target: Path) -> dict[str, object]:
    ad = TraktorAdapter(target)
    return {n.name: n for n in ad.playlist_tree()}


def removed_lines(before: str, after: str) -> list[str]:
    return [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if l.startswith("-") and not l.startswith("---")]


src, ids, files = source_library()
target = ROOT / "laptop" / "Traktor 4.5.0" / "collection.nml"
target.parent.mkdir(parents=True)
shutil.copy2(REAL, target)

RUNNING: set[str] = set()
apps_running.running = lambda platforms: {p for p in platforms if p in RUNNING}
prefs.update_prefs({"localLibraries": {"traktor": str(target), "rekordbox": str(ROOT / "none" / "master.db")},
                    "localAudioRoot": str(AUDIO), "localNormalise": False})

with TestClient(main.app) as c:
    r = c.post("/api/library/open", json={"path": str(src)})
    assert r.status_code == 200, r.text
    adapter = main.STATE.adapter

    print("== the local libraries ==")
    libs = {l["platform"]: l for l in c.get("/api/local-libraries").json()["libraries"]}
    check("Traktor is the configured collection, usable", libs["traktor"]["path"] == str(target)
          and libs["traktor"]["blocked"] is None and not libs["traktor"]["detected"], str(libs["traktor"]))
    check("a rekordbox library that is not there is not_found", libs["rekordbox"]["blocked"] == "not_found")

    print("== a computer set ==")
    r = c.post("/api/exports", json={"name": "Gig", "kind": "computer", "targets": ["onelibrary"]})
    check("a stick format is not a push target", r.status_code == 422, r.text)
    gig = c.post("/api/exports", json={"name": "Gig", "kind": "computer", "targets": ["traktor"]}).json()
    check("created with no destination, mode merge", gig["kind"] == "computer" and gig["mode"] == "merge"
          and gig["destination"] == "", str(gig))
    pl = c.post("/api/playlists", json={"name": "Peak"}).json()["id"]
    c.put(f"/api/playlists/{pl}/entries", json={"track_ids": ids[:2]})
    c.post(f"/api/exports/{gig['id']}/add", json={"playlist_ids": [pl], "track_ids": [ids[2]]})
    adapter.set_cue(ids[0], slot=0, start_sec=1.0, cue_type="cue", name="Drop")
    adapter.replace_grid(ids[0], [(0.5, 120.0)])
    check("an export route refuses a computer set",
          c.post(f"/api/exports/{gig['id']}/preview", json={}).status_code == 400)

    pv = c.post(f"/api/exports/{gig['id']}/push/preview", json={}).json()
    t = pv["targets"][0]
    check("the preview: three to add, nothing blocked", pv["blocked"] is None and pv["pushable"] == 3
          and (t["add"], t["already"], t["remove"]) == (3, 0, 0), str(pv))

    print("== the first push merges ==")
    before = target.read_text()
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    check("the push runs", j["state"] == "done", str(j.get("error")))
    check("each copy is its source, byte for byte",
          all(copy_of(f).is_file() and sha(copy_of(f)) == sha(f) for f in files[:3]))
    after = target.read_text()
    gone = removed_lines(before, after)
    check("nothing of the user's changed: only the entry count and the root folder's count",
          all("<COLLECTION ENTRIES=" in l or "SUBNODES COUNT=" in l for l in gone), "\n".join(gone[:5]))
    entries = pushed(target)
    check("three entries in the shared folder", set(entries) == {"track0.mp3", "track1.mp3", "track2.mp3"},
          str(list(entries)))
    tree = top_level(target)
    folder = tree.get("Gig")
    names = {n.name for n in folder.children} if folder else set()
    check("the set's folder holds its playlist and Other", names == {"Peak", "Other"}, str(names))
    tk = TraktorAdapter(target)
    tid = next(t.id for t in tk.tracks if t.title == "Track 0")
    cues = tk.track_cues(tid)
    check("the cue and grid crossed, the grid locked",
          any(c_.slot == 0 and c_.name == "Drop" for c_ in cues.cues) and len(cues.grid_markers) == 1
          and cues.grid_locked, str(cues))
    check("the push is in the collection's version history",
          len(history.list_history(target)) >= 2, str(history.list_history(target)))

    print("== an unchanged re-push writes nothing ==")
    stamp = target.stat().st_mtime_ns
    inodes = {f.name: copy_of(f).stat().st_ino for f in files[:3]}
    pv = c.post(f"/api/exports/{gig['id']}/push/preview", json={}).json()
    check("the preview: three already there", (pv["targets"][0]["add"], pv["targets"][0]["already"]) == (0, 3)
          and pv["unchanged"] == 3, str(pv))
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    check("nothing copied", j["state"] == "done" and j["result"]["copied"] == 0, str(j.get("result")))
    check("…the same files, and the collection not saved",
          {f.name: copy_of(f).stat().st_ino for f in files[:3]} == inodes and target.stat().st_mtime_ns == stamp)

    print("== the source wins ==")
    adapter.set_cue(ids[0], slot=1, start_sec=2.0, cue_type="cue", name="Break")
    adapter.set_track_metadata(ids[1], {"title": "Track 1 (edit)"})
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    tk = TraktorAdapter(target)
    cues = tk.track_cues(tid)
    check("the new cue reached the pushed entry, the old one kept, no duplicates",
          j["state"] == "done" and sorted((c_.slot, c_.name) for c_ in cues.cues if c_.slot is not None)
          == [(0, "Drop"), (1, "Break")], str(cues.cues))
    check("the new title too", any(t.title == "Track 1 (edit)" for t in tk.tracks))
    check("…and only those two were updated", j["result"]["targets"][0]["updated"] == 2, str(j["result"]))

    print("== a re-levelled copy is swapped in ==")
    prefs.update_prefs({"localNormalise": True, "localLevel": {"measure": "lufs", "target": -6.0, "ceiling": -0.5}})
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    check("the push runs", j["state"] == "done", str(j.get("error")))
    changed = [f for f in files[:3] if sha(copy_of(f)) != sha(f)]
    check("the copies changed level", j["result"]["normalised"] > 0 and changed, str(j.get("result")))
    entries = pushed(target)
    check("each entry's FILESIZE follows its new copy",
          all(entries[f.name].info.filesize == round(copy_of(f).stat().st_size / 1024) for f in files[:3]))
    check("no temporary file left", not list(AUDIO.rglob("*.konduktor-partial")))
    prefs.update_prefs({"localNormalise": False})
    wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    check("Normalise off: the source's own bytes back", all(sha(copy_of(f)) == sha(f) for f in files[:3]))

    print("== a second set, sharing a track ==")
    warm = c.post("/api/exports", json={"name": "Warmup", "kind": "computer", "targets": ["traktor"]}).json()
    c.post(f"/api/exports/{warm['id']}/add", json={"track_ids": [ids[2], ids[3]]})
    tk = TraktorAdapter(target)
    tk.create_folder("Warmup")   # the user's own folder, with the set's name
    tk.save()
    j = wait(c, c.post(f"/api/exports/{warm['id']}/push", json={}).json()["id"])
    tree = top_level(target)
    check("a user's folder with the set's name is left alone; the set's is suffixed",
          j["state"] == "done" and "Warmup" in tree and "Warmup (Konduktor)" in tree
          and not tree["Warmup"].children, str(list(tree)))
    check("the shared track is one entry", sum(1 for n in pushed(target) if n == "track2.mp3") == 1
          and len(pushed(target)) == 4, str(list(pushed(target))))

    print("== removal follows the set ==")
    c.post(f"/api/exports/{gig['id']}/remove", json={"track_ids": [ids[2]]})
    c.put(f"/api/playlists/{pl}/entries", json={"track_ids": ids[:1]})
    pv = c.post(f"/api/exports/{gig['id']}/push/preview", json={}).json()
    check("the preview counts two to remove", pv["targets"][0]["remove"] == 2, str(pv["targets"]))
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    names = set(pushed(target))
    check("track1 left the collection, track2 stays (Warmup holds it)",
          j["state"] == "done" and "track1.mp3" not in names and "track2.mp3" in names, str(names))
    check("track1's copy deleted, track2's kept",
          not copy_of(files[1]).exists() and copy_of(files[2]).exists())
    check("one removal reported", j["result"]["targets"][0]["removed"] == 1, str(j["result"]))

    print("== refusals ==")
    RUNNING.add("traktor")
    pv = c.post(f"/api/exports/{gig['id']}/push/preview", json={}).json()
    check("Traktor running blocks the push", pv["blocked"] == "app_running" and pv["targets"][0]["running"], str(pv))
    check("…and the run is refused", c.post(f"/api/exports/{gig['id']}/push", json={}).status_code == 409)
    RUNNING.clear()
    prefs.update_prefs({"localLibraries": {"traktor": str(src)}})
    libs = {l["platform"]: l for l in c.get("/api/local-libraries").json()["libraries"]}
    check("the library open in Konduktor is never a target", libs["traktor"]["blocked"] == "open_in_konduktor",
          str(libs["traktor"]))
    wrong = ROOT / "not-a-collection.nml"
    wrong.write_text("hello")
    prefs.update_prefs({"localLibraries": {"traktor": str(wrong)}})
    libs = {l["platform"]: l for l in c.get("/api/local-libraries").json()["libraries"]}
    check("a file that is not a Traktor collection is unreadable", libs["traktor"]["blocked"] == "unreadable",
          str(libs["traktor"]))
    prefs.update_prefs({"localLibraries": {"traktor": str(target), "rekordbox": str(ROOT / "none" / "master.db")}})

    print("== a failure mid-copy leaves the library and the folder as they were ==")
    c.post(f"/api/exports/{gig['id']}/add", json={"track_ids": [ids[4]]})
    real_write = local_push.write_copy

    def broken(adapter_, planned, partial, handle, advance):
        partial.write_bytes(b"half")
        raise OSError("disk unplugged")

    local_push.write_copy = broken
    before = target.read_bytes()
    j = wait(c, c.post(f"/api/exports/{gig['id']}/push", json={}).json()["id"])
    local_push.write_copy = real_write
    check("the push fails", j["state"] == "failed", str(j))
    check("…the collection untouched, no file placed",
          target.read_bytes() == before and not copy_of(files[4]).exists()
          and not list(AUDIO.rglob("*.konduktor-partial")))

    print("== overwrite ==")
    c.patch(f"/api/exports/{warm['id']}", json={"mode": "overwrite"})
    pv = c.post(f"/api/exports/{warm['id']}/push/preview", json={}).json()
    check("the preview names the set it removes", pv["targets"][0]["wipes_sets"] == ["Gig"], str(pv["targets"]))
    j = wait(c, c.post(f"/api/exports/{warm['id']}/push", json={}).json()["id"])
    check("the overwrite runs", j["state"] == "done", str(j.get("error")))
    st = TraktorStore(target)
    check("the collection holds only Warmup's two tracks", len(st._nml.collection.entry) == 2
          and set(pushed(target)) == {"track2.mp3", "track3.mp3"}, str(len(st._nml.collection.entry)))
    tree = top_level(target)
    check("…in its folder", set(tree) == {"Warmup"}, str(list(tree)))
    check("Gig's own file is deleted, Warmup's kept",
          not copy_of(files[0]).exists() and copy_of(files[2]).exists() and copy_of(files[3]).exists())
    check("the replaced collection is in the version history",
          len(history.list_history(target)) >= 3)
    pv = c.post(f"/api/exports/{gig['id']}/push/preview", json={}).json()
    check("Gig is forgotten there: every track to add again",
          pv["targets"][0]["already"] == 0 and pv["targets"][0]["add"] == pv["pushable"], str(pv["targets"]))
    c.patch(f"/api/exports/{warm['id']}", json={"mode": "merge"})
    pv = c.post(f"/api/exports/{warm['id']}/push/preview", json={}).json()
    check("a merge after the overwrite finds Warmup's tracks", pv["targets"][0]["already"] == 2, str(pv["targets"]))

    # ---- rekordbox, when installed --------------------------------------------------
    from konduktor.adapters.rekordbox import discovery as rb_discovery

    found = rb_discovery.detect_libraries()
    if not found:
        print("== rekordbox: not installed here, skipped ==")
    else:
        from konduktor.adapters.rekordbox.adapter import RekordboxAdapter
        from konduktor.core.adapter import Unsupported

        print("== rekordbox: merge ==")
        rb_dir = ROOT / "rekordbox"
        rb_dir.mkdir()
        real_db = Path(found[0]["path"])
        for name in ("master.db", "masterPlaylists6.xml"):
            if (real_db.parent / name).is_file():
                shutil.copy2(real_db.parent / name, rb_dir / name)
        rb = rb_dir / "master.db"
        probe = RekordboxAdapter(rb)
        synced, count = probe.capabilities().readonly_cause == "cloud_synced", len(probe.tracks)
        probe.close()
        if synced:
            print("  (the local rekordbox library is cloud-synced: checking it is refused)")
        prefs.update_prefs({"localLibraries": {"traktor": str(target), "rekordbox": str(rb)}})
        rbset = c.post("/api/exports", json={"name": "RB Gig", "kind": "computer", "targets": ["rekordbox"]}).json()
        c.post(f"/api/exports/{rbset['id']}/add", json={"track_ids": ids[:2]})
        pv = c.post(f"/api/exports/{rbset['id']}/push/preview", json={}).json()
        if synced:
            check("a cloud-synced library is refused", pv["blocked"] == "target_unavailable"
                  and pv["targets"][0]["blocked"] == "cloud_synced", str(pv))
        else:
            j = wait(c, c.post(f"/api/exports/{rbset['id']}/push", json={}).json()["id"])
            check("the push runs", j["state"] == "done", str(j.get("error")))
            ad = RekordboxAdapter(rb)
            mine = [t for t in ad.tracks if t.filepath and str(AUDIO) in t.filepath]
            tree = {n.name: n for n in ad.playlist_tree()}
            tid = next((t.id for t in mine if t.title == "Track 0"), None)
            cues = ad.track_cues(tid) if tid else None
            ad.close()
            check("two tracks added beside the user's", len(mine) == 2, str([t.title for t in mine]))
            check("the set's folder", "RB Gig" in tree, str(list(tree)))
            check("the cues crossed", cues is not None and any(c_.slot == 0 for c_ in cues.cues), str(cues))
            backups = list((paths.app_data_dir() / "local_push" / "backups" / "rekordbox").iterdir())
            check("the library was backed up first", backups and (backups[0] / "master.db").is_file())

            print("== rekordbox: a track it will not remove is kept and reported ==")
            real_remove = RekordboxAdapter.remove_tracks

            def refuse(self, track_ids):
                raise Unsupported("“Track 1” is in a History list in Rekordbox")

            RekordboxAdapter.remove_tracks = refuse
            c.post(f"/api/exports/{rbset['id']}/remove", json={"track_ids": [ids[1]]})
            j = wait(c, c.post(f"/api/exports/{rbset['id']}/push", json={}).json()["id"])
            RekordboxAdapter.remove_tracks = real_remove
            res = j.get("result") or {}
            check("the push still completes, reporting the kept track",
                  j["state"] == "done" and res["targets"][0]["kept"], str(j.get("error") or res))
            check("…whose file stays", copy_of(files[1]).exists())
            j = wait(c, c.post(f"/api/exports/{rbset['id']}/push", json={}).json()["id"])
            ad = RekordboxAdapter(rb)
            left = [t for t in ad.tracks if t.filepath and str(AUDIO) in t.filepath]
            ad.close()
            check("the next push removes it", j["state"] == "done" and len(left) == 1, str([t.title for t in left]))

            print("== rekordbox: overwrite ==")
            (rb_dir / "master.db-wal").write_bytes(b"stale")
            c.patch(f"/api/exports/{rbset['id']}", json={"mode": "overwrite"})
            j = wait(c, c.post(f"/api/exports/{rbset['id']}/push", json={}).json()["id"])
            check("the overwrite runs", j["state"] == "done", str(j.get("error")))
            ad = RekordboxAdapter(rb)
            titles = [t.title for t in ad.tracks]
            tree = [n.name for n in ad.playlist_tree()]
            ad.close()
            check("the library holds only the set", titles == ["Track 0"], f"{count} -> {titles}")
            check("…in its folder", tree == ["RB Gig"], str(tree))
            check("the stale WAL is gone", not (rb_dir / "master.db-wal").exists())

    print("== the ledger and sets survive as plain data ==")
    sets = {s["name"]: s for s in c.get("/api/exports").json()}
    check("kind and mode persist", sets["Warmup"]["kind"] == "computer" and sets["Warmup"]["mode"] == "merge")

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("RESULT:", "SOME FAILED" if failed else "ALL PASSED")
raise SystemExit(1 if failed else 0)
