"""Normalize on a REMOTE library.

A Konduktor server runs in a thread on a temp copy of the real collection whose
entries point at generated audio on "the server"; the app normalises through
its own routes. The server never decodes audio, so this pins the split:

  * the server plans from its own loudness store, and this computer reports
    what must be downloaded and uploaded;
  * the run downloads, measures where the server has no value (stored back on
    the server), rewrites + verifies HERE and uploads; the SERVER parks the
    original, puts the upload under its name and recomputes the auto gain —
    its saved collection untouched until Save, which deletes the parked
    original there; Discard puts it back;
  * the server refuses an upload at a gain its own plan would not give;
  * Cancel deletes what the run uploaded, and nothing else;
  * a takeover mid-batch loses nothing: the verified upload stays on the
    server and the next run publishes it without rewriting;
  * a platform that draws waveforms gets them measured HERE, from the
    levelled file, and handed to the end step.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-remotenorm-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import av  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor.adapters.remote import config as remote_config  # noqa: E402
from konduktor.adapters.remote.client import RemoteClient  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.app_state import AppState  # noqa: E402
from konduktor.core import level_edit  # noqa: E402
from konduktor.gain import normalize, remote_normalize  # noqa: E402
from konduktor.pending import parked_name, partial_name  # noqa: E402
from konduktor.server.app import Server, create_app  # noqa: E402
from konduktor.server.config import ServerConfig  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


class MemoryKeychain(dict):
    def get(self, account):  # noqa: D401
        return dict.get(self, account)

    def set(self, account, password):
        self[account] = password

    def delete(self, account):
        self.pop(account, None)


def music(seconds=8.0, amp=0.3, sr=44100, seed=1):
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(seed)
    beat = (np.sin(2 * np.pi * 2 * t) > 0.6).astype(float)
    x = [amp * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.3 * np.sin(2 * np.pi * (440 + 30 * c) * t)
                + 0.2 * rng.standard_normal(t.size)) for c in range(2)]
    return np.vstack(x).astype(np.float32)


def make(path: Path, amp: float, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    x = music(amp=amp, seed=seed)
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


remote_config.use_password_store(MemoryKeychain())
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-remotenorm-"))
FILES = ["a.mp3", "b.flac", "c.wav", "d.mp3", "e.wav", "f.mp3"]

# ---- a server library: entries pointed at generated audio --------------------------------
content = (ROOT / "server" / "Music").resolve()
work = ROOT / "server" / "collection.nml"
work.parent.mkdir(parents=True)
shutil.copy2(REAL, work)
st = TraktorStore(work)
entries = [e for e in st._nml.collection.entry if e.location and e.stems is None][:len(FILES)]
ids, files = [], []
for n, (name, e) in enumerate(zip(FILES, entries)):
    path = make(content / name, 0.8, n + 1)
    e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
    e.cue_v2 = []
    st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
    ids.append(st._key_of(e))
    files.append(path)
st.save()
del st
originals = {p.name: p.read_bytes() for p in files}
keep = ROOT / "originals"
keep.mkdir()
for p in files:
    shutil.copy2(p, keep / p.name)

cfg = ServerConfig(platform="traktor", library=work, content=content, username="dj", password="pw", name="NAS")
srv = Server(cfg, state=AppState())
srv.open()
uv = uvicorn.Server(uvicorn.Config(create_app(srv), host="127.0.0.1", port=0, log_level="warning"))
threading.Thread(target=uv.run, daemon=True).start()
while not uv.started:
    time.sleep(0.05)
port = uv.servers[0].sockets[0].getsockname()[1]
OPTS = {"measure": "lufs", "target": -14.0, "ceiling": -1.0}


def wait(c, jid, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def at_gain(name: str, gain: float) -> bool | str:
    try:
        level_edit.verify(keep / name, content / name, gain)
        return True
    except level_edit.LevelEditError as ex:
        return str(ex)


def server_gain_items() -> list[dict]:
    return [i for i in srv.state.pending.items if i.get("kind") == "gain"]


class Handle:
    cancelled = False

    def progress(self, **kw):
        pass


with TestClient(main.app) as c:
    remote_id = c.post("/api/remotes", json={"name": "NAS", "host": f"127.0.0.1:{port}",
                                             "username": "dj", "password": "pw"}).json()["remote"]["id"]
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    assert r.status_code == 200, r.text
    server_adapter = srv.state.adapter
    caps = c.get("/api/capabilities").json()
    check("a remote Traktor library is normalizable", caps["tracks"]["normalizable"], str(caps["tracks"]))

    print("== the plan: the server's, plus this computer's ==")
    p = c.post("/api/tracks/normalize/preview", json={"track_ids": ids[:2], **OPTS}).json()
    check("both tracks are measured first (the server has no value yet)",
          p.get("to_measure") == 2 and all(r["outcome"] == "unmeasured" for r in p["rows"]), str(p)[:300])
    check("…and what must cross the network",
          p["transfer"]["download"] > 0 and p["transfer"]["upload"] > 0, p.get("transfer"))
    check("Traktor draws no waveform from its own files: no lead", all(r["lead"] is None for r in p["rows"]))

    print("== a run: rewritten here, swapped in on the server ==")
    saved = work.read_bytes()
    job = wait(c, c.post("/api/tracks/normalize", json={"track_ids": ids[:2], **OPTS}).json()["id"])
    res = job.get("result") or {}
    check("the job normalises both", job["state"] == "done" and sorted(res.get("normalised", [])) == sorted(ids[:2]),
          job.get("error") or res)
    items = server_gain_items()
    check("the server's ledger holds both, awaiting Save", len(items) == 2
          and all(i["state"] == "swapped" for i in items), str([i.get("state") for i in items]))
    for name in FILES[:2]:
        g = next(i["gain_db"] for i in items if Path(i["source"]).name == name)
        check(f"{name}: the file on the server is the original at {g:+.2f} dB", at_gain(name, g) is True,
              str(at_gain(name, g)))
        check(f"{name}: the original is parked beside it, unchanged",
              parked_name(content / name).read_bytes() == originals[name])
    check("the SAVED collection is untouched until Save", work.read_bytes() == saved)
    held = httpx.post(f"http://127.0.0.1:{port}/v1/loudness/get", json={"paths": [str(files[0])]},
                      auth=("dj", "pw")).json()["entries"]
    after_lufs = held[0]["lufs"] if held else None
    track = main.require_adapter().track(ids[0])
    check("the server's loudness is the NEW level, and the mirror's auto gain follows it",
          after_lufs is not None and abs(track.auto_gain_db - (-9.2 - after_lufs)) < 1e-4,
          f"{after_lufs} {track.auto_gain_db}")
    st_ = c.get("/api/state").json()
    check("the state reports the pending change (from the server's ledger)",
          st_["pending_gain"] and st_["pending_gain"]["tracks"] == 2, st_.get("pending_gain"))
    check("…and blocks what would save or drop it", c.post("/api/reload").status_code == 409)
    c.post("/api/save")
    check("Save deletes the parked originals on the server",
          not any(parked_name(content / n).exists() for n in FILES[:2]) and not server_gain_items())
    check("…and the saved collection holds the new gain",
          f'PERCEIVED_DB="{-9.2 - after_lufs:.6f}"'.encode() in work.read_bytes())

    print("== Discard puts it back ==")
    job = wait(c, c.post("/api/tracks/normalize", json={"track_ids": [ids[2]], **OPTS}).json()["id"])
    check("(setup) a second run", job["state"] == "done" and (job.get("result") or {}).get("normalised"),
          job.get("error"))
    c.post("/api/discard")
    check("Discard restores the original's bytes on the server, nothing parked",
          (content / FILES[2]).read_bytes() == originals[FILES[2]]
          and not parked_name(content / FILES[2]).exists() and not server_gain_items())

    print("== the server checks the gain against its own plan ==")
    adapter = main.require_adapter()
    local = adapter.audio_path(ids[3])
    junk = ROOT / "junk.mp3"
    shutil.copy2(local, junk)
    c.post("/api/tracks/loudness", json={"track_id": ids[3]})
    try:
        adapter.stage_gain(ids[3], junk, gain_db=12.0412, options=OPTS)
        refused = False
    except Exception as ex:  # noqa: BLE001
        refused = "planned" in str(ex) or "409" in str(ex)
    check("an upload at a gain the server would not give is refused", refused)
    check("…and leaves nothing", not partial_name(content / FILES[3]).exists() and not server_gain_items())

    print("== Cancel deletes what the run uploaded ==")
    h = Handle()
    real_stage = adapter.stage_gain

    def stage_then_cancel(*a, **k):
        out = real_stage(*a, **k)
        h.cancelled = True
        return out

    adapter.stage_gain = stage_then_cancel
    out = remote_normalize.run(h, adapter, [ids[3], ids[4]], normalize.Options(**OPTS), still_current=lambda: True)
    adapter.stage_gain = real_stage
    check("a cancel after one upload stops the batch", out["cancelled"])
    check("…and the uploaded file is deleted on the server",
          not partial_name(content / FILES[3]).exists() and not server_gain_items(), str(server_gain_items()))
    check("…nothing else changed", (content / FILES[3]).read_bytes() == originals[FILES[3]]
          and not server_adapter.dirty)

    print("== a takeover mid-batch loses nothing ==")
    other = RemoteClient(remote_config.Remote(id="o", name="NAS", host=f"127.0.0.1:{port}", username="dj"), "pw")

    def stage_then_takeover(*a, **k):
        out = real_stage(*a, **k)
        other.acquire("other", "Studio iMac", takeover=True)
        other.release()
        return out

    adapter.stage_gain = stage_then_takeover
    job = wait(c, c.post("/api/tracks/normalize", json={"track_ids": [ids[4]], **OPTS}).json()["id"])
    adapter.stage_gain = real_stage
    check("(the run cannot publish once taken over)", job["state"] != "done"
          or not (job.get("result") or {}).get("normalised"), str(job)[:300])
    check("the verified upload stays on the server",
          partial_name(content / FILES[4]).is_file() and [i["state"] for i in server_gain_items()] == ["written"])
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    p = c.post("/api/tracks/normalize/preview", json={"track_ids": [ids[4]], **OPTS}).json()
    check("the next plan reuses it", p["rows"][0].get("staged") and p["transfer"]["upload"] == 0, str(p)[:300])
    real_apply = level_edit.apply
    level_edit.apply = lambda *a, **k: (_ for _ in ()).throw(AssertionError("rewrote a staged file"))
    job = wait(c, c.post("/api/tracks/normalize", json={"track_ids": [ids[4]], **OPTS}).json()["id"])
    level_edit.apply = real_apply
    check("…and publishes it without rewriting", job["state"] == "done"
          and (job.get("result") or {}).get("normalised") == [ids[4]], job.get("error") or job.get("result"))
    c.post("/api/save")
    g = next((i for i in srv.state.pending.items), None)
    check("…saved, its original gone", not parked_name(content / FILES[4]).exists() and g is None)

    print("== a platform that draws waveforms gets them measured HERE ==")
    server_adapter = srv.state.adapter
    seen = {}
    server_adapter.analysis_lead = lambda path: 0.0
    real_changed = server_adapter.audio_changed

    def recording(track_id, change):
        seen[track_id] = change
        return real_changed(track_id, change)

    server_adapter.audio_changed = recording
    p = c.post("/api/tracks/normalize/preview", json={"track_ids": [ids[5]], **OPTS}).json()
    check("the plan names the platform's lead", p["rows"][0]["lead"] == 0.0, str(p["rows"][0]))
    job = wait(c, c.post("/api/tracks/normalize", json={"track_ids": [ids[5]], **OPTS}).json()["id"])
    change = seen.get(ids[5])
    check("the end step receives the levelled file's waveform, measured on this computer",
          job["state"] == "done" and change is not None and change.measured is not None
          and len(change.measured.frames.bands) > 100, job.get("error"))
    c.post("/api/discard")
    uv.should_exit = True

print()
print("RESULT:", "FAILURES" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
