"""A remote library, end to end: a real server, the app's own routes.

A Konduktor server (`konduktor.server`) runs in a thread on a temp copy of the
real collection, its audio mapped onto generated MP3s; the app (`main.py`)
opens it through the picker's routes, exactly as the desktop app would. Pins:

  * a remote is saved only after a handshake — a wrong password is refused with
    nothing saved, and an unreachable PRIMARY falls back to the fallback;
  * the remote is offered as a platform, and opens as THE library: projection,
    capabilities (destination on the library, no reference, no remapping);
  * edits go to the server and come back in the mirror; Save writes on the
    server and touches only the edited ENTRY (the fidelity guarantee survives
    the trip); a no-op save writes nothing; history lists the server's
    versions; discard drops edits there;
  * audio is downloaded once into the cache, by what the server says the file
    is; analysis (Analyze grid) runs here on that copy and lands on the server;
  * another computer taking over makes this one read-only — at the next
    command, not the next heartbeat — and reopening says who holds it, offers
    the takeover and names the edits left behind;
  * export sets are kept by the server, and an export from the remote library
    copies the audio, then re-exports copying nothing;
  * importing a file UPLOADS it into a folder on the server and adds it there;
  * opening a local library releases the server's session.
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
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-remote-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


import uvicorn
from fastapi.testclient import TestClient

from konduktor import history
from konduktor import remote_protocol as rp
from konduktor.adapters.remote import config as remote_config
from konduktor.adapters.remote.client import RemoteClient
from konduktor.adapters.traktor.adapter import TraktorAdapter
from konduktor.app_state import AppState
from konduktor.core.pathmap import PathMapping
from konduktor.server.app import Server, create_app
from konduktor.server.config import ServerConfig
from konduktor.server.session import SessionLock
from stem_test_support import make_mp3


class MemoryKeychain:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account)

    def set(self, account, password):
        self.items[account] = password

    def delete(self, account):
        self.items.pop(account, None)


remote_config.use_password_store(MemoryKeychain())


def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, server.servers[0].sockets[0].getsockname()[1]


def entry_span(data: bytes, file_name: str):
    marker = f'FILE="{file_name}"'.encode()
    i = data.find(marker)
    return data.rfind(b"<ENTRY ", 0, i), data.find(b"</ENTRY>", i) + len(b"</ENTRY>")


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    work = root / "library" / "collection.nml"
    work.parent.mkdir()
    shutil.copy2(REAL, work)
    content = (root / "music").resolve()
    content.mkdir()

    # Map one folder of the collection onto generated audio on the "server".
    probe = TraktorAdapter(work)
    by_dir: dict[Path, list[str]] = {}
    for t in probe.tracks:
        p = probe.audio_path(t.id)
        if p is not None and p.suffix.lower() == ".mp3":
            by_dir.setdefault(p.parent, []).append(t.id)
    folder, ids = max(by_dir.items(), key=lambda kv: len(kv[1]))
    ids = ids[:3]
    mapped = content / "Mapped"
    for n, tid in enumerate(ids):
        make_mp3(mapped / probe.audio_path(tid).name, seconds=4, seed=n + 1)
    probe_paths = {tid: probe.audio_path(tid) for tid in ids}
    del probe

    t = [1000.0]
    cfg = ServerConfig(platform="traktor", library=work, content=content, username="dj",
                       password="secret", name="NAS",
                       path_maps=(PathMapping.make(str(folder), str(mapped)),))
    srv = Server(cfg, state=AppState(), lock=SessionLock(clock=lambda: t[0]))
    srv.open()
    uv, port = serve(create_app(srv))
    server_adapter = srv.state.adapter

    import konduktor.main as main

    c = TestClient(main.app)
    print("== saving a remote ==")
    form = {"name": "NAS", "host": "127.0.0.1:1", "fallback_host": f"127.0.0.1:{port}",
            "username": "dj", "password": "wrong"}
    r = c.post("/api/remotes", json=form)
    check("a wrong password is refused, nothing saved",
          r.status_code == 400 and "password" in r.json()["detail"]["message"].lower()
          and c.get("/api/remotes").json() == [], r.text[:200])
    form["password"] = "secret"
    r = c.post("/api/remotes", json=form)
    body = r.json()
    check("the right one saves, reporting each address",
          r.status_code == 200 and [x["kind"] for x in body["results"]] == ["unreachable", "ok"], r.text[:300])
    remote_id = body["remote"]["id"]
    check("the password is in the keychain, not prefs",
          "secret" not in Path(os.environ["KONDUKTOR_DATA_DIR"], "userprefs.json").read_text())
    plats = c.get("/api/platforms").json()
    check("Remote is offered as a platform, last", plats[-1]["platform"] == "remote"
          and plats[-1]["found"] == 1 and plats[-1]["selects"] == "remote")

    print("== opening it ==")
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    status = r.json()
    check("it opens as THE library", r.status_code == 200 and status["loaded"]
          and status["tracks"] == len(server_adapter.tracks), r.text[:300])
    check("…named as saved, reached through the fallback",
          status["library"]["display_name"] == "NAS" and status["library"]["remote"]["via"] == "fallback")
    caps = c.get("/api/capabilities").json()
    check("its capabilities: the destination is the server's, nothing referenced, nothing remapped",
          caps["writable"] and caps["tracks"]["audio_destination"] == "library"
          and not caps["tracks"]["reference"] and not caps["paths"]["remappable"])
    page = c.get("/api/tracks", params={"limit": 20000}).json()
    check("the table reads from the mirror", page["total"] == len(server_adapter.tracks))
    check("the server shows this computer holding the session",
          srv.lock.describe()["machine"] == remote_config.machine_name())

    print("== edits and saves ==")
    target = ids[0]
    file_name = probe_paths[target].name
    original = work.read_bytes()
    r = c.patch("/api/tracks", json={"track_id": target, "fields": {"genre": "Remote Edit"}})
    check("an edit applies on the server", r.status_code == 200
          and server_adapter.track(target).genre == "Remote Edit")
    check("…and in the mirror", main.require_adapter().track(target).genre == "Remote Edit")
    r = c.post("/api/tracks/cue", json={"track_id": target, "slot": 4, "start": 1.0, "type": "cue"})
    check("a hotcue goes to the server", r.status_code == 200
          and any(cu["slot"] == 4 for cu in server_adapter.track_cues(target).model_dump()["cues"]), r.text[:200])
    check("the state says unsaved", c.get("/api/state").json()["dirty"] is True)
    versions = len(history.list_history(work))
    r = c.post("/api/save").json()
    saved = work.read_bytes()
    check("save writes on the server", r["saved"] and saved != original)
    s0, e0 = entry_span(original, file_name)
    s1, e1 = entry_span(saved, file_name)
    check("…changing nothing outside the edited ENTRY",
          original[:s0] == saved[:s1] and original[e0:] == saved[e1:])
    check("…and is versioned there", len(history.list_history(work)) == versions + 1
          and len(c.get("/api/history").json()) == versions + 1)
    r = c.post("/api/save").json()
    check("a no-op save writes nothing", r["saved"] is False and work.read_bytes() == saved)
    c.patch("/api/tracks", json={"track_id": target, "fields": {"genre": "Throw away"}})
    r = c.post("/api/discard")
    check("discard drops the edit on the server", r.status_code == 200 and not server_adapter.dirty
          and server_adapter.track(target).genre == "Remote Edit"
          and main.require_adapter().track(target).genre == "Remote Edit", r.text[:200])

    print("== version history, on the server ==")
    listed = c.get("/api/history").json()
    oldest = listed[-1]["id"]
    r = c.post(f"/api/history/{oldest}/restore")
    check("restoring a version writes it back ON THE SERVER", r.status_code == 200
          and work.read_bytes() == original, r.text[:200])
    check("…as a new version", len(c.get("/api/history").json()) == len(listed) + 1)
    server_adapter = srv.state.adapter  # a restore reopens the library there
    check("…and the mirror follows", main.require_adapter().track(target).genre
          == server_adapter.track(target).genre)
    # Back to the edited state, for what follows.
    c.patch("/api/tracks", json={"track_id": target, "fields": {"genre": "Remote Edit"}})
    c.post("/api/save")

    print("== audio, on this computer ==")
    adapter = main.require_adapter()
    downloads = []
    real_download = adapter.client.download
    adapter.client.download = lambda *a, **k: (downloads.append(a[0]), real_download(*a, **k))[1]
    r = c.get("/api/tracks/audio", params={"track_id": target})
    served = (mapped / file_name).read_bytes()
    check("the deck's audio is the server's file", r.status_code == 200 and r.content == served)
    c.get("/api/tracks/audio", params={"track_id": target})
    check("…downloaded once, then served from the cache", downloads == [target], downloads)
    cached = adapter.cached_audio(target)
    check("the cache keys it by what the server says the file is",
          cached is not None and cached.read_bytes() == served and cached.suffix == ".mp3")
    r = c.post("/api/tracks/grid/auto", json={"track_id": target})
    grid = server_adapter.track_cues(target).grid_markers
    check("Analyze runs here and the grid lands on the server",
          r.status_code == 200 and grid and abs(grid[0].bpm - 120) < 1.5, r.text[:200])
    check("…with no second download", downloads == [target], downloads)
    c.post("/api/save")

    print("== export, from the server's library ==")
    dest = root / "stick"
    r = c.post("/api/exports", json={"name": "Gig", "targets": ["traktor"], "destination": str(dest)})
    set_id = r.json().get("id")
    c.post(f"/api/exports/{set_id}/add", json={"track_ids": ids})
    doc = c.get("/api/exports").json()
    check("an export set is created", set_id and any(s["id"] == set_id for s in doc), r.text[:300])
    import httpx

    kept = httpx.get(f"http://127.0.0.1:{port}/v1/export-sets", auth=("dj", "secret")).json()
    check("…and kept by the server", set_id in kept["sets"], kept)

    def run_export():
        job = c.post(f"/api/exports/{set_id}/run").json()
        for _ in range(600):
            j = c.get(f"/api/jobs/{job['id']}").json()
            if j["state"] != "running":
                return j
            time.sleep(0.05)
        return j

    downloads.clear()
    job = run_export()
    copies = sorted(p for p in (dest / "Contents").rglob("*.mp3"))
    check("the export copies every track's audio onto the stick",
          job["state"] == "done" and len(copies) == len(ids), job.get("error"))
    check("…downloading only what the cache lacked", sorted(downloads) == sorted(ids[1:]), downloads)
    inodes = {p: p.stat().st_ino for p in copies}
    downloads.clear()
    job = run_export()
    check("a re-export copies nothing", job["state"] == "done"
          and all(p.stat().st_ino == i for p, i in inodes.items()) and downloads == [], downloads)

    print("== importing: an upload into the server ==")
    incoming = make_mp3(root / "laptop" / "Brand New.mp3", seconds=3, seed=9)
    # The folder-add path: a folder of loose files on THIS computer.
    c.get("/api/folder/tracks", params={"path": str(incoming.parent)})  # the scan the add reads
    add = {"track_ids": [str(incoming)], "mode": "copy", "destination": str(mapped / "Incoming")}
    r = c.post("/api/folder/add/preview", json=add)
    check("(preview) adding a local file to the remote library, space read from the server",
          r.status_code == 200 and r.json()["free_bytes"], r.text[:300])
    job = c.post("/api/folder/add", json=add).json()
    for _ in range(400):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["state"] != "running":
            break
        time.sleep(0.05)
    landed = mapped / "Incoming" / "Brand New.mp3"
    check("the file is uploaded into the chosen folder ON THE SERVER",
          j["state"] == "done" and landed.read_bytes() == incoming.read_bytes(), j.get("error"))
    added = [tr for tr in server_adapter.tracks if (tr.filepath or "").endswith("Brand New.mp3")]
    check("…and added to the server's library", len(added) == 1)
    from konduktor.adapters.traktor.locations import os_path_to_location

    stored = "".join(os_path_to_location(folder / "Incoming" / "Brand New.mp3"))
    check("…stored as the collection stores every other path (the mapping, inverted)",
          added and added[0].id == stored, added[0].id if added else None)
    check("…and still resolving to the uploaded file", added and server_adapter.audio_path(added[0].id) == landed)
    check("…and to the mirror here", any((tr.filepath or "").endswith("Brand New.mp3")
                                        for tr in main.require_adapter().tracks))

    print("== another computer takes over ==")
    other = RemoteClient(remote_config.Remote(id="o", name="NAS", host=f"127.0.0.1:{port}", username="dj"), "secret")
    other.acquire("other-client", "Studio iMac", takeover=True)
    other.rpc("set_track_metadata", track_id=ids[1], fields={"comment": "from the studio"})
    r = c.patch("/api/tracks", json={"track_id": target, "fields": {"genre": "Too late"}})
    check("this computer's next command is refused, naming who took it",
          r.status_code == 503 and "Studio iMac" in r.json()["detail"], r.text[:200])
    st = c.get("/api/state").json()
    check("…and it is read-only now, at once", st["remote"]["state"] == "taken_over"
          and st["remote"]["machine"] == "Studio iMac")
    caps = c.get("/api/capabilities").json()
    check("…through its capabilities", not caps["writable"] and caps["readonly_cause"] == "taken_over")
    check("the server still has the genre this computer saved", server_adapter.track(target).genre == "Remote Edit")
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id})
    check("reopening says who holds it", r.status_code == 409
          and r.json()["detail"]["machine"] == "Studio iMac", r.text[:200])
    r = c.post("/api/library/open-remote", json={"remote_id": remote_id, "takeover": True})
    pending = r.json().get("pending_edits")
    check("taking it back names the edits the other computer left",
          r.status_code == 200 and pending and pending["machine"] == "Studio iMac", r.text[:300])
    check("…which the mirror shows", main.require_adapter().track(ids[1]).comment == "from the studio")
    other.close()

    print("== the server forgets the session (a restart) ==")
    c.post("/api/save")  # nobody's edits pending, as after a real restart
    srv.lock = SessionLock(clock=lambda: t[0])
    main.STATE.remote.beat()
    d = c.get("/api/state").json()["remote"]
    h = srv.lock.holder()
    check("the session is taken back without asking, and the user told",
          d["state"] == "connected" and d["notices"] == 1 and d["notice"]
          and h is not None and h.machine == remote_config.machine_name(), d)
    r = c.patch("/api/tracks", json={"track_id": target, "fields": {"comment": "after restart"}})
    check("…and edits go through again", r.status_code == 200
          and server_adapter.track(target).comment == "after restart", r.text[:200])

    print("== leaving ==")
    local = root / "local.nml"
    shutil.copy2(REAL, local)
    r = c.post("/api/library/open", json={"path": str(local)})
    check("opening a local library releases the server's session",
          r.status_code == 200 and srv.lock.holder() is None, r.text[:200])
    uv.should_exit = True

print()
print("RESULT:", "FAILURES" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
