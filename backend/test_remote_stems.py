"""Convert to Stems on a REMOTE library, with a FAKE engine.

A Konduktor server runs in a thread on a temp copy of the real collection whose
playlisted tracks point at generated MP3s on "the server"; the app converts
through its own routes. The server never decodes audio, so this pins the split:

  * the server plans (targets, skips) and this computer reports what it must
    download and upload, and the room it needs here;
  * a Replace batch separates HERE, uploads, and the SERVER publishes, parks
    the original and swaps the entry — its saved collection untouched until
    Save, which deletes the parked original there; Discard puts it back;
  * Cancel deletes what the run uploaded, and nothing else;
  * another computer taking over mid-batch loses no separation: the uploaded,
    verified stem file stays on the server and the next run reuses it without
    the engine.
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
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-remotestems-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor.adapters.remote import config as remote_config  # noqa: E402
from konduktor.adapters.remote.client import RemoteClient  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.app_state import AppState  # noqa: E402
from konduktor.core import stem_file as sf  # noqa: E402
from konduktor.server.app import Server, create_app  # noqa: E402
from konduktor.server.config import ServerConfig  # noqa: E402
from konduktor.stems import convert as convert_mod  # noqa: E402
from konduktor.stems import engine_manager as em  # noqa: E402
from konduktor.stems import remote_convert  # noqa: E402
from konduktor.stems.pending import parked_name, partial_name  # noqa: E402
from stem_test_support import install_fake_engine, make_mp3  # noqa: E402

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


remote_config.use_password_store(MemoryKeychain())
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-remotestems-"))
em._DEFAULT = install_fake_engine(ROOT / "engine")

# ---- a server library: playlisted tracks pointed at generated MP3s -------------------
music = (ROOT / "server" / "Music").resolve()
music.mkdir(parents=True)
work = ROOT / "server" / "collection.nml"
shutil.copy2(REAL, work)
st = TraktorStore(work)
in_pl = {k for n in st._iter_nodes(st._root()) if n.playlist for k in st._entry_keys_of(n.playlist)}
entries = [e for e in st._nml.collection.entry if e.location and st._key_of(e) in in_pl and e.stems is None][:5]
ids = []
for n, e in enumerate(entries):
    path = make_mp3(music / f"Track {n}.mp3", seed=n + 1)
    old = st._key_of(e)
    e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
    new = st._key_of(e)
    for node in st._iter_nodes(st._root()):
        for pe in (node.playlist.entry if node.playlist else None) or []:
            if pe.primarykey and pe.primarykey.key == old:
                pe.primarykey.key = new
    st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
    e.cue_v2 = []
    st.set_hotcue(new, 1, 1.0, 0, name="Drop")
    st.replace_grid(new, [(0.5, 120.0)])
    ids.append(new)
st.save()
del st

cfg = ServerConfig(platform="traktor", library=work, content=music, username="dj", password="pw", name="NAS")
srv = Server(cfg, state=AppState())
srv.open()
uv = uvicorn.Server(uvicorn.Config(create_app(srv), host="127.0.0.1", port=0, log_level="warning"))
threading.Thread(target=uv.run, daemon=True).start()
while not uv.started:
    time.sleep(0.05)
port = uv.servers[0].sockets[0].getsockname()[1]


def wait(c, jid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


with TestClient(main.app) as c:
    remote_id = c.post("/api/remotes", json={"name": "NAS", "host": f"127.0.0.1:{port}",
                                             "username": "dj", "password": "pw"}).json()["remote"]["id"]
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    assert r.status_code == 200, r.text
    server_adapter = srv.state.adapter

    print("== the plan: the server's, plus this computer's ==")
    p = c.post("/api/tracks/stems/preview", json={"track_ids": ids[:1], "mode": "replace"}).json()
    check("one track to convert, its target beside it on the server",
          len(p["convert"]) == 1 and p["convert"][0]["target"] == str(music / "Track 0.stem.m4a"), p)
    check("…and what must cross the network", p["transfer"]["download"] > 0 and p["transfer"]["upload"] > 0,
          p.get("transfer"))
    check("…and the room needed on BOTH machines",
          any(s["volume"] == "this computer" for s in p["space"])
          and any(s["volume"].startswith("the server") for s in p["space"]), p["space"])

    print("== a Replace batch ==")
    saved_bytes = work.read_bytes()
    r = c.post("/api/tracks/stems/convert", json={"track_ids": ids[:1], "mode": "replace"})
    job = wait(c, r.json()["id"])
    res = job.get("result") or {}
    target = music / "Track 0.stem.m4a"
    check("the job converts the track", job["state"] == "done" and len(res.get("converted", [])) == 1,
          job.get("error") or res)
    check("the stem file is on the server, the original parked beside it",
          target.is_file() and sf.is_stem_file(target) and parked_name(music / "Track 0.mp3").is_file())
    new_id = res.get("renamed", {}).get(ids[0])
    check("the server's entry now names the stem file", new_id and server_adapter.track(new_id) is not None)
    check("…and the mirror here follows", main.require_adapter().track(new_id) is not None)
    check("the SAVED collection is untouched until Save", work.read_bytes() == saved_bytes)
    st_ = c.get("/api/state").json()
    check("the state reports the conversion awaiting Save (from the server's ledger)",
          st_["pending_stems"] and st_["pending_stems"]["tracks"] == 1, st_.get("pending_stems"))
    check("…and blocks what would save or drop it", c.post("/api/reload").status_code == 409)
    c.post("/api/save")
    check("Save deletes the parked original on the server",
          not parked_name(music / "Track 0.mp3").exists() and target.is_file())
    check("…and the ledger is clear", not (c.get("/api/state").json()["pending_stems"] or {}).get("tracks"))

    print("== playing a remote stem file ==")
    layout = c.get("/api/tracks/stems", params={"track_id": new_id})
    stems = layout.json().get("stems") if layout.status_code == 200 else None
    check("the deck is offered the stem file's four stems",
          isinstance(stems, list) and len(stems) == 4, layout.text[:200])
    one = c.get("/api/tracks/audio", params={"track_id": new_id, "stem": 1})
    check("…and each streams, extracted HERE from the downloaded file",
          one.status_code == 200 and len(one.content) > 1000, one.status_code)

    print("== Discard puts it back ==")
    r = c.post("/api/tracks/stems/convert", json={"track_ids": [ids[1]], "mode": "replace"})
    job = wait(c, r.json()["id"])
    check("(setup) a second conversion", job["state"] == "done" and (music / "Track 1.stem.m4a").is_file(),
          job.get("error"))
    c.post("/api/discard")
    check("Discard restores the original and removes the stem file, on the server",
          (music / "Track 1.mp3").is_file() and not (music / "Track 1.stem.m4a").exists()
          and not parked_name(music / "Track 1.mp3").exists())

    print("== Cancel deletes what the run uploaded ==")
    adapter = main.require_adapter()
    opts = convert_mod.Options(mode="replace")
    planned = remote_convert.plan(adapter, [ids[2], ids[3]], opts)

    class Handle:
        cancelled = False

        def progress(self, **kw):
            pass

    h = Handle()
    real_stage = adapter.stage_stem

    def stage_then_cancel(*a, **k):
        out = real_stage(*a, **k)
        h.cancelled = True
        return out

    adapter.stage_stem = stage_then_cancel
    out = remote_convert.run(h, adapter, planned, opts, still_current=lambda: True)
    adapter.stage_stem = real_stage
    check("a cancel after one upload stops the batch", out["cancelled"])
    check("…and the uploaded stem file is deleted on the server",
          not partial_name(music / "Track 2.stem.m4a").exists() and not srv.state.pending.leftovers,
          srv.state.pending.leftovers)
    check("…nothing else changed", (music / "Track 2.mp3").is_file() and not server_adapter.dirty)

    print("== a takeover mid-batch loses no separation ==")
    planned = remote_convert.plan(adapter, [ids[4]], opts)
    item = planned.items[0]
    local = adapter.audio_path(ids[4])
    staged = ROOT / "staged.stem.m4a"
    written = sf.build(local, staged, lambda pcm: [pcm * g for g in (0.5, 0.25, -0.2, 0.1)])
    adapter.stage_stem(ids[4], item["target"], staged, bit_rate=written.bit_rate, duration=written.duration)
    other = RemoteClient(remote_config.Remote(id="o", name="NAS", host=f"127.0.0.1:{port}", username="dj"), "pw")
    other.acquire("other", "Studio iMac", takeover=True)
    other.release()
    check("the uploaded file stays on the server as a verified leftover",
          partial_name(music / "Track 4.stem.m4a").is_file() and len(srv.state.pending.leftovers) == 1)
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    p = c.post("/api/tracks/stems/preview", json={"track_ids": [ids[4]], "mode": "replace"}).json()
    check("the next plan reuses it", p["convert"] and p["convert"][0]["reuse"] and p["seconds"] == 0, p)
    em._DEFAULT = em.EngineManager(ROOT / "no-engine")  # an engine call would now fail
    r = c.post("/api/tracks/stems/convert", json={"track_ids": [ids[4]], "mode": "replace"})
    job = wait(c, r.json()["id"]) if r.status_code == 200 else {"state": "refused", "error": r.text}
    check("…and converts without the engine", job["state"] == "done"
          and len((job.get("result") or {}).get("converted", [])) == 1, job.get("error") or job.get("result"))
    c.post("/api/save")
    check("…to a stem file on the server", sf.is_stem_file(music / "Track 4.stem.m4a"))
    uv.should_exit = True

print()
print("RESULT:", "FAILURES" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
