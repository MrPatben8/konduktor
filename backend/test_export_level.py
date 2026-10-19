"""Export with "Normalise audio" and "Auto gain", through the routes, on a temp
collection pointed at GENERATED audio, exported to Traktor + Rekordbox Library
+ OneLibrary at once.

What would fail silently, and matter at a gig:

  * a normalised COPY is its source at exactly the planned gain, the source is
    never touched, and a copy that needs no change is byte-identical;
  * the DJ app's gain is written from the COPY's level (Traktor -9.2 - LUFS,
    rekordbox -10 - LUFS), and only where the target keeps one;
  * sources with no loudness are measured once, into the store;
  * the set remembers its options; a re-export at the same level copies
    nothing, one at another level re-copies only the copies whose level
    changes, and turning Normalise off puts the source's own bytes back;
  * the waveform cache is keyed by the level too (else a stick is drawn from
    the un-levelled measurement).
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
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-export-level-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import av  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import exporter, loudness_store  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import level_edit, loudness  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-export-level-"))
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def music(seconds=8.0, amp=0.3, sr=44100, seed=1):
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(seed)
    beat = (np.sin(2 * np.pi * 2 * t) > 0.6).astype(float)
    x = [amp * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.3 * np.sin(2 * np.pi * (440 + 30 * c) * t)
                + 0.2 * rng.standard_normal(t.size)) for c in range(2)]
    return np.vstack(x).astype(np.float32)


def make(path: Path, amp: float, spike: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    x = music(amp=amp)
    if spike:
        x[:, 44100] = 0.99  # a quiet track with one peak over the ceiling
    if path.suffix == ".mp3":
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
    else:
        sf.write(str(path), x.T, 44100, subtype="PCM_16")
    return path


FILES = [("hot.mp3", 0.9, False), ("quiet.flac", 0.05, False), ("hot.wav", 0.8, False),
         ("spike.wav", 0.02, True)]


def library():
    d = ROOT / "lib"
    work = d / "collection.nml"
    d.mkdir(parents=True)
    shutil.copy2(REAL, work)
    st = TraktorStore(work)
    entries = [e for e in st._nml.collection.entry if e.location and e.stems is None]
    ids, files = [], []
    for (f, amp, spike), e in zip(FILES, entries):
        path = make(d / "Music" / f, amp, spike)
        e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
        e.cue_v2 = []  # the real track's cues mean nothing on 8 s of generated audio
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


def manifest_gains(dest: Path) -> dict[str, float]:
    audio = json.loads((dest / exporter.MANIFEST_NAME).read_text())["audio"]
    return {Path(rel).name: float(r.get("gain_db") or 0.0) for rel, r in audio.items()}


def traktor_gains(dest: Path) -> dict[str, float | None]:
    st = TraktorStore(dest / "collection.nml")
    return {e.location.file: (e.loudness.perceived_db if e.loudness is not None else None)
            for e in st._nml.collection.entry}


def rekordbox_gains(dest: Path) -> dict[str, float | None]:
    ad = RekordboxAdapter(dest / "master.db")
    try:
        return {Path(t.filepath).name: t.auto_gain_db for t in ad.tracks}
    finally:
        ad.close()


LEVEL = {"measure": "lufs", "target": -9.0, "ceiling": -1.0}

work, ids, sources = library()
before = {p.name: sha(p) for p in sources}
dest = ROOT / "STICK"
dest.mkdir()

with TestClient(main.app) as c:
    r = c.post("/api/library/open", json={"path": str(work)})
    assert r.status_code == 200, r.text
    s = c.post("/api/exports", json={"name": "Gig", "targets": ["traktor", "rekordbox", "onelibrary"],
                                      "destination": str(dest)}).json()
    c.post(f"/api/exports/{s['id']}/add", json={"track_ids": ids})
    check("a new set: Normalise off, Auto gain on", (s["normalise"], s["auto_gain"]) == (False, True), str(s))
    targets = {o["platform"]: o for o in c.get("/api/export-targets").json()}
    check("Auto gain is offered for Traktor and the Rekordbox Library, not for a stick",
          targets["traktor"]["writes_auto_gain"] and targets["rekordbox"]["writes_auto_gain"]
          and not targets["onelibrary"]["writes_auto_gain"]
          and not targets["rekordbox_export"]["writes_auto_gain"])

    print("== the first export: every source measured, the copies levelled ==")
    body = {"normalise": True, "auto_gain": True, "level": LEVEL}
    pv = c.post(f"/api/exports/{s['id']}/preview", json=body).json()
    check("the preview counts the sources the run measures first",
          pv["to_measure"] == len(ids) and pv["level_counts"].get("unmeasured") == len(ids), str(pv))
    j = wait(c, c.post(f"/api/exports/{s['id']}/run", json=body).json()["id"])
    check("the export runs", j["state"] == "done", str(j.get("error")))
    store = loudness_store.local()
    entries = store.get([str(p) for p in sources])
    check("every source was measured into the store", len(entries) == len(sources), str(list(entries)))
    check("the sources are byte-identical", {p.name: sha(p) for p in sources} == before)
    gains = manifest_gains(dest)
    check("the manifest records each copy's level", set(gains) == {p.name for p in sources}, str(gains))
    check("three copies change level, the spike's does not (hot peak left alone)",
          sum(1 for g in gains.values() if g) == 3 and gains["spike.wav"] == 0.0 and j["result"]["normalised"] == 3,
          str(gains))
    for p in sources:
        copy = dest / "Contents" / p.name
        g = gains[p.name]
        if g:
            try:
                level_edit.verify(p, copy, g)
                ok = True
            except level_edit.LevelEditError as ex:
                ok = str(ex)
            check(f"{p.name}: the copy is the source at {g:+.2f} dB", ok is True, str(ok))
        else:
            check(f"{p.name}: an unchanged level is a byte-identical copy", sha(copy) == before[p.name])
    after = {p.name: shifted for p in sources
             if (shifted := entries[str(p)].values.lufs + gains[p.name]) is not None}
    tk, rb = traktor_gains(dest), rekordbox_gains(dest)
    check("Traktor's gain is -9.2 - the COPY's LUFS",
          all(abs(tk[n] - (-9.2 - lufs)) < 1e-3 for n, lufs in after.items()), str(tk))
    check("the Rekordbox Library's gain is -10 - the COPY's LUFS",
          all(abs(rb[n] - (-10.0 - lufs)) < 1e-3 for n, lufs in after.items()), str(rb))
    saved = c.get("/api/exports").json()
    mine = next(x for x in saved if x["id"] == s["id"])
    check("the set remembers Normalise, Auto gain and the level",
          (mine["normalise"], mine["auto_gain"], mine["level"]) == (True, True, LEVEL), str(mine))

    print("== a re-export at the same level copies nothing ==")
    inodes = {p.name: (dest / "Contents" / p.name).stat().st_ino for p in sources}
    pv = c.post(f"/api/exports/{s['id']}/preview", json={}).json()
    check("nothing left to measure", pv["to_measure"] == 0, str(pv))
    j = wait(c, c.post(f"/api/exports/{s['id']}/run", json={}).json()["id"])
    check("every copy kept", j["state"] == "done" and j["result"]["unchanged"] == len(sources), str(j.get("result")))
    check("…the very same files", {p.name: (dest / "Contents" / p.name).stat().st_ino for p in sources} == inodes)

    print("== another target re-copies only the copies whose level changes ==")
    j = wait(c, c.post(f"/api/exports/{s['id']}/run",
                       json={"level": {**LEVEL, "target": -10.0}}).json()["id"])
    new_gains = manifest_gains(dest)
    # At -10 the MP3's nearest step and the ceiling-capped FLAC keep their
    # gain; the WAV's changes; the spike stays at 0.
    changed = {n for n in gains if abs(new_gains[n] - gains[n]) > 0.01}
    kept = set(gains) - changed
    check("a copy whose level changes is written again — and only that one",
          j["state"] == "done" and changed == {"hot.wav"}
          and j["result"]["unchanged"] == len(kept), f"{changed} {j.get('result')}")
    check("…the others are the very same files",
          all((dest / "Contents" / n).stat().st_ino == inodes[n] for n in kept))
    try:
        level_edit.verify(sources[2], dest / "Contents" / "hot.wav", new_gains["hot.wav"])
        ok = True
    except level_edit.LevelEditError as ex:
        ok = str(ex)
    check("…and the re-written one is at the new level", ok is True, str(ok))

    print("== Normalise off: the source's own bytes, the gain from the source's level ==")
    j = wait(c, c.post(f"/api/exports/{s['id']}/run", json={"normalise": False}).json()["id"])
    check("every copy is byte-identical to its source",
          j["state"] == "done" and all(sha(dest / "Contents" / p.name) == before[p.name] for p in sources))
    tk = traktor_gains(dest)
    check("Traktor's gain follows the SOURCE's LUFS",
          all(abs(tk[p.name] - (-9.2 - entries[str(p)].values.lufs)) < 1e-3 for p in sources), str(tk))

    print("== Auto gain off: none written ==")
    j = wait(c, c.post(f"/api/exports/{s['id']}/run", json={"auto_gain": False}).json()["id"])
    check("no LOUDNESS in the exported collection", all(v is None for v in traktor_gains(dest).values()))
    check("no auto gain in the Rekordbox Library", all(v is None for v in rekordbox_gains(dest).values()))

    print("== a file only the file says cannot be levelled (HE-AAC) is copied as it is ==")
    real_apply = level_edit.apply

    def refuse(src, dst, gain_db):
        raise level_edit.LevelEditError(f"{Path(src).name}: only AAC-LC can be changed losslessly")

    level_edit.apply = refuse
    j = wait(c, c.post(f"/api/exports/{s['id']}/run", json={"normalise": True, "auto_gain": True}).json()["id"])
    level_edit.apply = real_apply
    check("the export still completes", j["state"] == "done" and j["result"]["normalised"] == 0, str(j.get("error")))
    check("…every copy byte-identical to its source, recorded at no gain",
          all(sha(dest / "Contents" / p.name) == before[p.name] for p in sources)
          and all(g == 0.0 for g in manifest_gains(dest).values()))
    check("…and the auto gain from the SOURCE's level",
          all(abs(traktor_gains(dest)[p.name] - (-9.2 - entries[str(p)].values.lufs)) < 1e-3 for p in sources))

print("== the waveform cache is keyed by the level ==")
p = exporter.PlannedTrack(track_id="t", title="t", source_path=Path("/x.mp3"), destination=None,
                          size=10, mtime_ns=1)
plain = exporter._fingerprint(p)
p.gain_db = -3.01
check("a levelled copy gets its own waveform-cache key", exporter._fingerprint(p) != plain)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
raise SystemExit(1 if failed else 0)
