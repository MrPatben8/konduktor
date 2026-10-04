"""The Konduktor SERVER (`konduktor.server`), through its own routes.

A library held on the user's NAS and opened from the desktop app as a "Remote".
Run against a temp copy of the real collection, with a FAKE clock for the
session lease. Pins the things that would fail quietly or dangerously:

  * every route needs the one account — a wrong password is a 401, not a
    read-only view;
  * ONE computer at a time: a command without the session token is refused
    (423), a second computer is told who holds it (409) and may take over,
    after which the first one's token is dead; a lease that ran out frees the
    session, and the SAME computer coming back renews it silently;
  * the edits a computer leaves behind are offered to the next one, named;
  * Save writes and versions ON THE SERVER; Discard drops the edits;
  * the folder browser and uploads cannot leave the content folder (`..`, a
    symlink), an upload never overwrites, and a session can only delete what
    it uploaded;
  * audio streams with Range; a track added from an upload plays.
  * the configuration refuses a missing password, a wrong platform and a
    malformed path map with one clear line.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-server-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


from fastapi.testclient import TestClient

from konduktor import history
from konduktor import remote_protocol as rp
from konduktor.app_state import AppState
from konduktor.server.app import Server, create_app
from konduktor.server.config import ConfigError, ServerConfig, from_env, parse_path_maps
from konduktor.server.session import SessionLock
from stem_test_support import make_mp3

print("== configuration ==")
with tempfile.TemporaryDirectory() as d:
    lib = Path(d) / "collection.nml"
    lib.write_text("x")
    content = Path(d) / "music"
    content.mkdir()
    base = {"KONDUKTOR_PLATFORM": "traktor", "KONDUKTOR_LIBRARY": str(lib),
            "KONDUKTOR_CONTENT": str(content), "KONDUKTOR_USERNAME": "dj",
            "KONDUKTOR_PASSWORD": "pw"}

    def refused(env, needle):
        try:
            from_env(env)
        except ConfigError as ex:
            return needle in str(ex)
        return False

    check("a complete environment parses", from_env(base).content == content.resolve())
    check("an empty password is refused", refused({**base, "KONDUKTOR_PASSWORD": ""}, "PASSWORD"))
    check("an unknown platform is refused", refused({**base, "KONDUKTOR_PLATFORM": "serato"}, "PLATFORM"))
    check("a missing content folder is refused",
          refused({**base, "KONDUKTOR_CONTENT": str(Path(d) / "nope")}, "CONTENT"))
    check("a malformed path map is refused", refused({**base, "KONDUKTOR_PATH_MAP": "/a -> /b"}, "PATH_MAP"))
    maps = parse_path_maps("/Users/ben/Music => /music ; X: => /ssd")
    check("several path maps parse, in order",
          [(m.from_prefix, m.to_prefix) for m in maps] == [("/Users/ben/Music", "/music"), ("X:", "/ssd")])

print("== the server ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "library" / "collection.nml"
    work.parent.mkdir()
    shutil.copy2(REAL, work)
    original = work.read_bytes()
    content = (Path(d) / "music").resolve()
    content.mkdir()
    outside = Path(d) / "outside"
    outside.mkdir()
    (content / "Sneaky").symlink_to(outside)
    (content / "Crates").mkdir()

    cfg = ServerConfig(platform="traktor", library=work, content=content,
                       username="dj", password="secret", name="NAS")
    t = [1000.0]
    server = Server(cfg, state=AppState(), lock=SessionLock(clock=lambda: t[0]))
    wrong = ServerConfig(**{**cfg.__dict__, "platform": "rekordbox"})
    try:
        Server(wrong, state=AppState()).open()
        check("a platform that does not match the library file is refused", False)
    except ConfigError as ex:
        check("a platform that does not match the library file is refused", "Traktor" in str(ex), str(ex))
    server.open()
    app = create_app(server)
    c = TestClient(app)
    c.auth = ("dj", "secret")
    anon = TestClient(app)

    check("no credentials → 401", anon.get("/v1/hello").status_code == 401)
    check("a wrong password → 401",
          TestClient(app).get("/v1/hello", auth=("dj", "nope")).status_code == 401)
    hello = c.get("/v1/hello").json()
    check("hello names the API version, platform and library",
          hello["api_version"] == list(rp.API_VERSION) and hello["platform"] == "traktor"
          and hello["name"] == "NAS" and hello["library_id"], hello)
    check("…nobody holds it yet, nothing is unsaved",
          hello["holder"] is None and hello["dirty"] is False and hello["pending_edits"] is None)

    def call(name, token=None, **args):
        headers = {rp.SESSION_HEADER: token} if token else {}
        return c.post(f"/v1/rpc/{name}", json={"args": rp.dump_args(name, args)}, headers=headers)

    adapter = server.state.adapter
    r = call("tracks")
    check("the projection reads without a session",
          r.status_code == 200 and len(r.json()["result"]) == len(adapter.tracks))
    caps = call("capabilities").json()["result"]
    check("capabilities say: destination on the library, no reference, no remapping",
          caps["tracks"]["audio_destination"] == "library" and caps["tracks"]["reference"] is False
          and caps["paths"]["remappable"] is False, caps["tracks"])
    check("an unknown argument is refused, not ignored",
          c.post("/v1/rpc/track", json={"args": {"track_id": "x", "trak": 1}}).status_code == 400)
    track = next(t for t in adapter.tracks if t.genre)
    r = call("set_track_metadata", track_id=track.id, fields={"genre": "Remote"})
    check("a command without the session → 423", r.status_code == 423, r.text[:120])

    print("== one computer at a time ==")
    a = c.post("/v1/session/acquire", json={"client_id": "A", "machine": "Laptop"}).json()
    r = c.post("/v1/session/acquire", json={"client_id": "B", "machine": "Studio"})
    check("a second computer is told who holds it",
          r.status_code == 409 and r.json()["detail"]["machine"] == "Laptop", r.text[:160])
    r = call("set_track_metadata", a["token"], track_id=track.id, fields={"genre": "Remote"})
    body = r.json()
    check("the holder's command applies and returns the changed track",
          r.status_code == 200 and body["changed"][0]["genre"] == "Remote" and body["rev"] == 1, r.text[:200])
    pending = c.get("/v1/hello", params={"client_id": "B"}).json()["pending_edits"]
    check("another computer is told whose unsaved edits these are",
          pending and pending["machine"] == "Laptop" and "track" in pending["summary"], pending)
    check("…but the computer that made them is not",
          c.get("/v1/hello", params={"client_id": "A"}).json()["pending_edits"] is None)
    b = c.post("/v1/session/acquire", json={"client_id": "B", "machine": "Studio", "takeover": True}).json()
    check("a takeover hands over the edits too", b["pending_edits"]["machine"] == "Laptop", b)
    r = call("set_track_metadata", a["token"], track_id=track.id, fields={"genre": "Late"})
    check("the displaced computer's token is dead (423, naming the new holder)",
          r.status_code == 423 and r.json()["detail"]["holder"]["machine"] == "Studio", r.text[:160])
    hb = c.post("/v1/session/heartbeat", json={}, headers={rp.SESSION_HEADER: a["token"]})
    check("…and its heartbeat says so", hb.status_code == 423)
    c.post("/v1/session/heartbeat", json={"batch": "grid-analysis"}, headers={rp.SESSION_HEADER: b["token"]})
    r = c.post("/v1/session/acquire", json={"client_id": "C", "machine": "Phone"})
    check("the holder's running batch is named to whoever asks",
          r.status_code == 409 and r.json()["detail"]["batch"] == "grid-analysis", r.text[:160])
    t[0] += rp.LEASE_SECONDS + 1
    r = c.post("/v1/session/acquire", json={"client_id": "C", "machine": "Phone"})
    check("an expired lease frees the session without a takeover", r.status_code == 200, r.text[:160])
    cc = r.json()
    t[0] += rp.LEASE_SECONDS * 3
    hb = c.post("/v1/session/heartbeat", json={}, headers={rp.SESSION_HEADER: cc["token"]})
    check("the same computer coming back renews its lease silently", hb.status_code == 200, hb.text[:160])
    tok = cc["token"]

    print("== save / discard / history ==")
    versions = len(history.list_history(work))
    r = c.post("/v1/save", headers={rp.SESSION_HEADER: tok}).json()
    check("save writes the library on the server", r["saved"] and work.read_bytes() != original, r)
    check("…and versions it there", len(history.list_history(work)) == versions + 1)
    check("…and nobody's edits are pending afterwards",
          c.get("/v1/hello", params={"client_id": "Z"}).json()["pending_edits"] is None)
    saved = work.read_bytes()
    call("set_track_metadata", tok, track_id=track.id, fields={"genre": "Throw away"})
    check("(setup) a second edit is unsaved", c.get("/v1/hello").json()["dirty"])
    c.post("/v1/discard", headers={rp.SESSION_HEADER: tok})
    check("discard drops it", not c.get("/v1/hello").json()["dirty"]
          and call("track", track_id=track.id).json()["result"]["genre"] == "Remote")
    check("…and leaves the file as saved", work.read_bytes() == saved)
    listed = c.get("/v1/history").json()
    check("history lists the server's versions", len(listed) == versions + 1)

    print("== the content folder ==")
    r = c.get("/v1/fs/list")
    names = [e["name"] for e in r.json()["entries"]]
    check("the browser starts at the content folder", r.json()["path"] == str(content) and "Crates" in names)
    check("`..` cannot leave it", c.get("/v1/fs/list", params={"path": str(content / "..")}).status_code == 400)
    check("nor can a symlink", c.get("/v1/fs/list", params={"path": str(content / "Sneaky")}).status_code == 400)
    check("free space is reported", c.get("/v1/fs/space", params={"path": str(content / "New")}).json()["free"] > 0)

    print("== uploads ==")
    mp3 = make_mp3(Path(d) / "src" / "Kick Tune.mp3")
    data = mp3.read_bytes()
    up = lambda name, folder: c.post("/v1/upload", params={"dir": folder, "name": name},
                                     content=data, headers={rp.SESSION_HEADER: tok})
    r1 = up("Kick Tune.mp3", str(content / "Crates" / "New"))
    r2 = up("Kick Tune.mp3", str(content / "Crates" / "New"))
    p1, p2 = Path(r1.json()["path"]), Path(r2.json()["path"])
    check("an upload lands in the chosen folder (created)", p1 == content / "Crates" / "New" / "Kick Tune.mp3"
          and p1.read_bytes() == data, r1.text[:200])
    check("a second upload of the same name never overwrites", p2.name == "Kick Tune-2.mp3")
    check("no partial is left behind", not list((content / "Crates" / "New").glob(".*")))
    check("an upload outside the content folder is refused", up("x.mp3", str(outside)).status_code == 400)
    check("a name that hides or climbs is refused", up("../x.mp3", str(content)).status_code == 400
          or up(".x.mp3", str(content)).status_code == 400)
    check("without the session, refused", c.post("/v1/upload", params={"dir": str(content), "name": "y.mp3"},
                                                   content=data).status_code == 423)
    r = c.delete("/v1/upload", params={"path": str(p2)}, headers={rp.SESSION_HEADER: tok})
    check("a session deletes what it uploaded", r.status_code == 200 and not p2.exists(), r.text[:160])
    keep = content / "Crates" / "mine.mp3"
    keep.write_bytes(b"user's")
    r = c.delete("/v1/upload", params={"path": str(keep)}, headers={rp.SESSION_HEADER: tok})
    check("…and nothing else", r.status_code == 403 and keep.exists())

    print("== adding an uploaded track, and its audio ==")
    from konduktor.core.model import Track

    wire = rp.WireNewTrack(track=Track(id="", title="Kick Tune", artist="Tester"), audio_path=str(p1))
    r = c.post("/v1/rpc/add_tracks", json={"args": {"items": [wire.model_dump(mode="json")]}},
               headers={rp.SESSION_HEADER: tok})
    ids = r.json()["result"] if r.status_code == 200 else []
    check("an uploaded file is added by the ordinary add_tracks",
          len(ids) == 1 and r.json()["changed"][0]["title"] == "Kick Tune", r.text[:300])
    if ids:
        whole = c.get("/v1/audio", params={"track_id": ids[0]})
        check("its audio streams", whole.status_code == 200 and whole.content == data
              and int(whole.headers["X-Konduktor-Size"]) == len(data))
        part = c.get("/v1/audio", params={"track_id": ids[0]}, headers={"Range": "bytes=10-19"})
        check("…with Range", part.status_code == 206 and part.content == data[10:20])
        facts = call("audio_facts", track_ids=ids).json()["result"]
        check("its facts name the server's file", facts[ids[0]]["key"] == str(p1))
        r = c.delete("/v1/upload", params={"path": str(p1)}, headers={rp.SESSION_HEADER: tok})
        check("an upload the library now names is not deleted", r.status_code == 409 and p1.exists())

    print("== export sets ==")
    doc = {"sets": {"s1": {"name": "Gig", "targets": ["traktor"], "destination": "/Volumes/STICK",
                           "playlist_ids": [], "track_ids": []}}}
    check("export sets need the session to change",
          c.put("/v1/export-sets", json=doc).status_code == 423)
    c.put("/v1/export-sets", json=doc, headers={rp.SESSION_HEADER: tok})
    got = c.get("/v1/export-sets").json()
    check("…and are kept by the server, keyed by its library id",
          got["sets"]["s1"]["name"] == "Gig" and got["library_id"] == hello["library_id"], got)

    c.post("/v1/session/release", headers={rp.SESSION_HEADER: tok})
    check("release frees the session", c.get("/v1/hello").json()["holder"] is None)
    check("the original collection beside the tests was never touched", REAL.exists())

print()
print("RESULT:", "FAILURES" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
