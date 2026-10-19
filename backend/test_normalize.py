"""Normalize end to end, through the routes, on temp collections pointed at
GENERATED audio (MP3, FLAC, WAV at known levels).

What would fail silently or destroy something:

  * the preview's counts are the run's (one `plan_gain`);
  * the run leaves the SAVED collection untouched; the new file is the
    original x 10^(g/20) under the ORIGINAL's path, the original parked
    beside it; the library's auto gain is RECOMPUTED from the new level and
    Konduktor's loudness value SHIFTED; operations that would save or drop the
    change answer 409; a track awaiting Save is not normalised (or converted)
    again;
  * Save deletes the parked originals and changes only the touched ENTRYs;
    Discard (and opening another library) put the originals back, bytes and
    loudness both;
  * after a CRASH, the next open restores — or, if the save got through,
    finishes — from the ledger plus what the saved library says.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-normalize-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import av  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import paths  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import level_edit, loudness  # noqa: E402
from konduktor.core.level_plan import STEP_DB  # noqa: E402
from konduktor.pending import parked_name  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-normalize-"))
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def music(seconds=8.0, amp=0.3, sr=44100, seed=1):
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(seed)
    beat = (np.sin(2 * np.pi * 2 * t) > 0.6).astype(float)  # a pulse, so it is not a steady tone
    x = [amp * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.3 * np.sin(2 * np.pi * (440 + 30 * c) * t)
                + 0.2 * rng.standard_normal(t.size)) for c in range(2)]
    return np.vstack(x).astype(np.float32)


def make_mp3(path: Path, amp: float):
    x = music(amp=amp)
    with av.open(str(path), "w", format="mp3") as c:
        s = c.add_stream("libmp3lame", rate=44100, layout="stereo")
        s.bit_rate = 256_000
        for o in range(0, x.shape[1], 1152):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1152]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = 44100, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def make(path: Path, amp: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".mp3":
        make_mp3(path, amp)
    else:
        sf.write(str(path), music(amp=amp).T, 44100, subtype="PCM_16")
    return path


FILES = [("hot.mp3", 0.9), ("quiet.flac", 0.03), ("hot.wav", 0.8), ("close.mp3", 0.12)]


def library(name: str):
    """A temp collection whose first playlisted entries point at FILES."""
    d = ROOT / name
    work = d / "collection.nml"
    d.mkdir(parents=True)
    shutil.copy2(REAL, work)
    st = TraktorStore(work)
    in_pl = {k for n in st._iter_nodes(st._root()) if n.playlist for k in st._entry_keys_of(n.playlist)}
    entries = [e for e in st._nml.collection.entry if e.location and st._key_of(e) in in_pl and e.stems is None]
    ids, paths_ = [], []
    for (f, amp), e in zip(FILES, entries):
        path = make(d / "Music" / f, amp)
        old = st._key_of(e)
        e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
        new = st._key_of(e)
        for n in st._iter_nodes(st._root()):
            for pe in (n.playlist.entry if n.playlist else None) or []:
                if pe.primarykey and pe.primarykey.key == old:
                    pe.primarykey.key = new
        st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
        ids.append(new)
        paths_.append(path)
    st.save()
    return work, ids, paths_


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def wait(c, jid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


OPTS = {"measure": "lufs", "target": -12.0, "ceiling": -1.0}


def normalize(c, ids, **opts):
    r = c.post("/api/tracks/normalize", json={"track_ids": ids, **OPTS, **opts})
    assert r.status_code == 200, r.text
    return wait(c, r.json()["id"])


def ledger() -> Path:
    return paths.app_data_dir() / "stems" / f"pending-{main.STATE.library_id}.json"


with TestClient(main.app) as c:
    print("== preview: one plan_gain, measured first where needed ==")
    work, ids, files = library("run")
    r = c.post("/api/library/open", json={"path": str(work)})
    assert r.status_code == 200, r.text
    pv = c.post("/api/tracks/normalize/preview", json={"track_ids": ids, **OPTS}).json()
    check("unmeasured tracks are counted to be measured first",
          pv["to_measure"] == len(ids) and pv["tracks_to_process"] == len(ids), str({k: pv[k] for k in pv if k != "rows"}))
    for p in files:
        c.post("/api/tracks/loudness", json={"track_id": ids[files.index(p)]})
    before = {p.name: loudness.measure_file(p) for p in files}
    pv = c.post("/api/tracks/normalize/preview", json={"track_ids": ids, **OPTS}).json()
    rows = {Path(r["title"]).name if False else r["track_id"]: r for r in pv["rows"]}
    gains = {files[i].name: rows[t].get("gain_db") for i, t in enumerate(ids)}
    check("measured: every track has a plan",
          all(rows[t].get("outcome") for t in ids), str(pv["rows"]))
    check("an MP3's gain is a whole number of 1.505 dB steps",
          abs(gains["hot.mp3"] / STEP_DB - round(gains["hot.mp3"] / STEP_DB)) < 1e-6 and gains["hot.mp3"] < 0, str(gains))
    check("the quiet FLAC is raised, but only to the ceiling",
          rows[ids[1]]["outcome"] in ("apply", "capped") and gains["quiet.flac"] > 0
          and before["quiet.flac"].true_peak + gains["quiet.flac"] <= -1.0 + 1e-6, str(rows[ids[1]]))
    check("lossless files are counted (Save makes their change permanent)",
          pv["lossless"] == sum(1 for i, t in enumerate(ids) if files[i].suffix != ".mp3" and rows[t]["gain_db"]),
          str(pv["lossless"]))

    print("== run: new files in place, originals parked, library follows ==")
    saved_collection = work.read_bytes()
    originals = {p.name: sha(p) for p in files}
    job = normalize(c, ids)
    res = job["result"]
    changed = [files[ids.index(t)] for t in res["normalised"]]
    check("the run normalises what the preview planned",
          len(res["normalised"]) == pv["to_change"] and not res["failed"], json.dumps(res)[:400])
    check("the SAVED collection is untouched until Save", work.read_bytes() == saved_collection)
    for p in changed:
        g = gains[p.name]
        parked = parked_name(p)
        try:
            level_edit.verify(parked, p, g)
            ok, why = True, ""
        except level_edit.LevelEditError as ex:
            ok, why = False, str(ex)
        check(f"{p.name}: the file at its own path is the original at {g:+.2f} dB", ok, why)
        check(f"{p.name}: the original is parked beside it, unchanged", sha(parked) == originals[p.name])
    state = c.get("/api/state").json()
    check("Save is due: the library is dirty and the pending change is named",
          state["dirty"] and state["pending_gain"]["tracks"] == len(changed)
          and state["pending_gain"]["lossless"] == sum(1 for p in changed if p.suffix != ".mp3"), str(state))
    vals = c.post("/api/loudness", json={"track_ids": ids}).json()["values"]
    tracks = {t["id"]: t for t in c.get("/api/tracks", params={"limit": 20000}).json()["items"]}
    for p in changed:
        t = ids[files.index(p)]
        want = before[p.name].lufs + gains[p.name]
        check(f"{p.name}: Konduktor's loudness value is shifted by the gain, and fresh",
              abs(vals[t]["lufs"] - want) < 1e-6 and not vals[t]["stale"], str(vals.get(t)))
        check(f"{p.name}: the auto gain is recomputed from the NEW level (-9.2 - LUFS)",
              abs(tracks[t]["auto_gain_db"] - (-9.2 - want)) < 1e-4, str(tracks[t]["auto_gain_db"]))
    check("measured afresh, the new file agrees with the shifted value",
          abs(loudness.measure_file(changed[0]).lufs - (before[changed[0].name].lufs + gains[changed[0].name])) < 0.05)

    print("== while it awaits Save ==")
    r = c.post("/api/reload")
    check("reload is refused (it would drop the change)", r.status_code == 409, r.text[:200])
    again = c.post("/api/tracks/normalize/preview", json={"track_ids": ids[:1], **OPTS}).json()
    check("the same track is not normalised again before Save",
          again["rows"][0]["skip"] and "awaiting Save" in again["rows"][0]["skip"], str(again["rows"]))
    stems = c.post("/api/tracks/stems/preview", json={"track_ids": ids[:1], "mode": "replace"}).json()
    check("…nor converted to stems", any("level change" in s["reason"] for s in stems["skipped"]), str(stems))

    print("== Save: parked originals deleted, only the touched entries change ==")
    r = c.post("/api/save")
    check("Save succeeds", r.status_code == 200, r.text[:200])
    check("the parked originals are gone", not any(parked_name(p).exists() for p in changed))
    check("the ledger is empty", not ledger().exists())
    out = work.read_bytes()
    for p in changed:
        t = ids[files.index(p)]
        check(f"{p.name}: the saved LOUDNESS is the new gain",
              f'PERCEIVED_DB="{round(-9.2 - (before[p.name].lufs + gains[p.name]), 6):.6f}"'.encode() in out)
    check("nothing pending any more", c.get("/api/state").json()["pending_gain"] is None)

    def without_entries(data: bytes, names) -> bytes:
        for name in names:
            i = data.find(f'FILE="{name}"'.encode())
            start, end = data.rfind(b"<ENTRY ", 0, i), data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
            data = data[:start] + data[end:]
        return data

    check("outside the normalised tracks' ENTRYs the collection is byte-identical",
          without_entries(out, [p.name for p in changed]) == without_entries(saved_collection, [p.name for p in changed]))

    print("== Discard puts the originals back, bytes and loudness ==")
    work2, ids2, files2 = library("discard")
    c.post("/api/library/open", json={"path": str(work2)})
    for t in ids2:
        c.post("/api/tracks/loudness", json={"track_id": t})
    shas = {p.name: sha(p) for p in files2}
    vals_before = c.post("/api/loudness", json={"track_ids": ids2}).json()["values"]
    res = normalize(c, ids2)["result"]
    r = c.post("/api/discard")
    check("discard succeeds", r.status_code == 200, r.text[:200])
    check("every file is byte-identical to before", {p.name: sha(p) for p in files2} == shas)
    check("no parked file or partial is left", not any(parked_name(p).exists() or Path(str(p) + ".konduktor-partial").exists()
                                                         for p in files2))
    vals_after = c.post("/api/loudness", json={"track_ids": ids2}).json()["values"]
    check("the loudness values are the originals' again, and fresh",
          all(abs(vals_after[t]["lufs"] - vals_before[t]["lufs"]) < 1e-9 and not vals_after[t]["stale"]
              for t in res["normalised"]), str(vals_after))

    print("== a crash: recovery from the ledger + the saved library ==")
    work3, ids3, files3 = library("crash")
    c.post("/api/library/open", json={"path": str(work3)})
    shas3 = {p.name: sha(p) for p in files3}
    res = normalize(c, ids3)["result"]
    done = [files3[ids3.index(t)] for t in res["normalised"]]
    # Crash 1: the app dies with the change unsaved (state "swapped").
    main.STATE.pending.restore = lambda: 0  # the process never got to restore
    main.STATE.pending.items = list(main.STATE.pending.items)
    c.post("/api/library/open", json={"path": str(work3)})
    check("an unsaved change is undone at the next open", {p.name: sha(p) for p in files3} == shas3
          and not any(parked_name(p).exists() for p in done))
    check("…and reported", (main.STATE.recovery or {}).get("restored") == len(done), str(main.STATE.recovery))
    # Crash 2: the save got through, the parked originals were never deleted.
    res = normalize(c, ids3)["result"]
    data = json.loads(ledger().read_text())
    main.STATE.adapter.save()  # the library write landed…
    for it in data["items"]:
        it["state"] = "saving"   # …but the app died before mark_saved
    ledger().write_text(json.dumps(data))
    main.STATE.pending.restore = lambda: 0
    c.post("/api/library/open", json={"path": str(work3)})
    check("a save that landed is FINISHED: parked originals deleted, new audio kept",
          not any(parked_name(p).exists() for p in done)
          and all(sha(p) != shas3[p.name] for p in done), str(main.STATE.recovery))
    # Crash 3: died in "saving" BEFORE the library write: restore.
    shas3 = {p.name: sha(p) for p in files3}
    res = normalize(c, ids3, target=-16.0)["result"]
    done3 = [files3[ids3.index(t)] for t in res["normalised"]]
    data = json.loads(ledger().read_text())
    for it in data["items"]:
        it["state"] = "saving"
    ledger().write_text(json.dumps(data))
    main.STATE.pending.restore = lambda: 0
    c.post("/api/library/open", json={"path": str(work3)})
    check("a save that did NOT land is undone (the library still has the old gain)",
          done3 and {p.name: sha(p) for p in files3} == shas3, str(main.STATE.recovery))

print("== drawn waveforms follow the new audio (rekordbox / OneLibrary ANLZ) ==")
from konduktor.adapters.rekordbox import anlz_file  # noqa: E402
from konduktor.core import waveform  # noqa: E402
from konduktor.core.adapter import AudioChange  # noqa: E402

measured = waveform.analyse_samples(music(seconds=20, amp=0.5)[0], lead=0.0)
WAVE_TAGS = {".DAT": {"PWAV", "PWV2"}, ".EXT": {"PWV3", "PWV5", "PWV4"}, ".2EX": {"PWV7", "PWV6", "PWVC"}}


def tags(path: Path) -> dict:
    out = {}
    for t in anlz_file.parse_file(path).tags:
        out.setdefault(t.type, []).append(t.data)
    return out


def check_regenerated(label: str, dat: Path, before: dict) -> None:
    for ext, wave in WAVE_TAGS.items():
        f = dat.with_suffix(ext)
        if not f.is_file():
            continue
        now = tags(f)
        old = before[ext]
        check(f"{label} {ext}: the waveform tags are rewritten",
              all(now.get(k) != old.get(k) for k in wave if k in old), str(sorted(now)))
        check(f"{label} {ext}: every other tag is byte-identical (cues, grid, phrases)",
              {k: v for k, v in now.items() if k not in wave} == {k: v for k, v in old.items() if k not in wave})


from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
with tempfile.TemporaryDirectory() as d:
    drive = Path(d) / "STICK"
    shutil.copytree(FIXTURE, drive)
    ad = OneLibraryAdapter(drive, read_only=False)
    t = next(x for x in ad.tracks)
    dat = ad._store.layout.resolve(ad._store.content(t.id).analysisDataFilePath)
    before = {ext: tags(dat.with_suffix(ext)) for ext in WAVE_TAGS if dat.with_suffix(ext).is_file()}
    db_before = (drive / "PIONEER/rekordbox/exportLibrary.db").read_bytes()
    ad.audio_changed(t.id, AudioChange(gain_db=-3.0, size=123456, measured=measured))
    check("OneLibrary: nothing reaches the drive before Save",
          {ext: tags(dat.with_suffix(ext)) for ext in before} == before)
    check("OneLibrary: a level change is an edit (Save will run)", ad.dirty)
    ad.save()
    check_regenerated("OneLibrary", dat, before)
    check("OneLibrary: the database's fileSize follows",
          OneLibraryAdapter(drive, read_only=True)._store.content(t.id).fileSize == 123456)

from konduktor.adapters.rekordbox import discovery as rb_discovery  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402

found = rb_discovery.detect_libraries()
if not found:
    print("  [SKIP] no Rekordbox library on this machine")
else:
    with tempfile.TemporaryDirectory() as d:
        src = Path(found[0]["path"])
        work = Path(d) / "master.db"
        shutil.copy2(src, work)
        shutil.copytree(src.parent / "share", Path(d) / "share", dirs_exist_ok=True)
        ad = RekordboxAdapter(work)
        t = next((x for x in ad.tracks if getattr(ad._store.content(x.id), "AnalysisDataPath", None)
                  and (Path(d) / "share" / str(ad._store.content(x.id).AnalysisDataPath).lstrip("/")).is_file()), None)
        if t is None:
            print("  [SKIP] no track with analysis files")
        else:
            dat = Path(d) / "share" / str(ad._store.content(t.id).AnalysisDataPath).lstrip("/")
            before = {ext: tags(dat.with_suffix(ext)) for ext in WAVE_TAGS if dat.with_suffix(ext).is_file()}
            ad.audio_changed(t.id, AudioChange(gain_db=-3.0, size=654321, measured=measured))
            check("rekordbox: nothing reaches the files before Save",
                  {ext: tags(dat.with_suffix(ext)) for ext in before} == before)
            ad.save()
            check_regenerated("rekordbox", dat, before)
            ad.close()
            check("rekordbox: djmdContent.FileSize follows",
                  RekordboxAdapter(work)._store.content(t.id).FileSize == 654321)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
raise SystemExit(1 if failed else 0)
