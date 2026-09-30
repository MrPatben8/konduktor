"""The stem engine manager and process, against a LOCAL server and a FAKE engine.

No network, no torch: a tiny HTTP server (Range-aware, and able to ignore Range)
serves a fake engine archive split into parts plus fake weights, and the fake
engine is a script speaking the real protocol. Pins what fails silently:

  * a download resumes from its `.part`, restarts if the server ignores the
    range, and never keeps a file whose checksum is wrong;
  * an install counts only once parts, archive AND a started engine all check
    out — a corrupt part or a wrong-version engine leaves nothing installed;
  * side-loading verifies against the release manifest; weights are verified;
  * the process driver relays progress, cancels without killing the engine,
    reports a crash WITH its reason, and ends the engine on close;
  * the CUDA engine is offered only for a new enough driver and card.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import stat
import sys
import tarfile
import tempfile
import threading
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-enginemgr-appdata-")

import numpy as np  # noqa: E402

from konduktor.stems import download, engine_manager as em  # noqa: E402
from konduktor.stems.engine_process import EngineCancelled, EngineProcess, keep_awake  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


from stem_test_support import FAKE_ENGINE  # noqa: E402 — the one fake engine, shared


class Handler(http.server.BaseHTTPRequestHandler):
    root: Path
    ignore_range = False
    requests: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.root / self.path.lstrip("/")
        type(self).requests.append((self.path, self.headers.get("Range")))
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        rng = self.headers.get("Range")
        if rng and not type(self).ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the cancel test hangs up mid-download, on purpose


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


tmp = Path(tempfile.mkdtemp(prefix="konduktor-enginemgr-"))
site = tmp / "site"
site.mkdir()
Handler.root = site
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"

print("== the weights manifest ==")
repo_copy = json.loads((Path(__file__).resolve().parents[1] / "engine" / "weights.json").read_text())
check("the backend's weights.json is the engine's (no drift)", em.WEIGHTS == repo_copy)

print("== download: resume, restart, verify, cancel ==")
blob = os.urandom(3_000_000)
(site / "blob.bin").write_bytes(blob)
dest = tmp / "dl" / "blob.bin"
dest.parent.mkdir()
(dest.parent / "blob.bin.part").write_bytes(blob[:1_000_000])
seen = []
Handler.requests.clear()
download.fetch(f"{BASE}/blob.bin", dest, sha256=sha(blob), size=len(blob), on_bytes=seen.append)
check("a download resumes from its .part with a Range request",
      Handler.requests[-1][1] == "bytes=1000000-" and dest.read_bytes() == blob, str(Handler.requests[-1]))
check("…and progress counts the resumed bytes once", sum(seen) == len(blob), str(sum(seen)))
dest.unlink()
(dest.parent / "blob.bin.part").write_bytes(blob[:1_000_000])
Handler.ignore_range = True
seen.clear()
download.fetch(f"{BASE}/blob.bin", dest, sha256=sha(blob), size=len(blob), on_bytes=seen.append)
Handler.ignore_range = False
check("a server that ignores Range makes it start over, correctly", dest.read_bytes() == blob and sum(seen) == len(blob))
dest.unlink()
try:
    download.fetch(f"{BASE}/blob.bin", dest, sha256="0" * 64, size=len(blob))
    check("a checksum mismatch is refused", False, "kept it")
except download.DownloadError:
    check("a checksum mismatch is refused, and nothing is kept",
          not dest.exists() and not (dest.parent / "blob.bin.part").exists())
calls = {"n": 0}


def cancel_soon():
    calls["n"] += 1
    return calls["n"] > 1


try:
    download.fetch(f"{BASE}/blob.bin", dest, sha256=sha(blob), size=len(blob), cancelled=cancel_soon)
    check("a cancelled download stops", False, "finished")
except download.DownloadCancelled:
    check("a cancelled download stops, keeping its .part to resume", (dest.parent / "blob.bin.part").exists()
          and not dest.exists())

print("== engine install ==")
VERSION = em.CONFIG["version"]


def make_release(version_reported: str, name: str, parts: int = 2) -> dict:
    """A fake engine archive, split into `parts`, plus its manifest entry."""
    src = tmp / f"build-{name}"
    exe = src / "konduktor-engine" / "konduktor-engine"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(FAKE_ENGINE.format(python=sys.executable, version=version_reported))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    archive = tmp / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src / "konduktor-engine", arcname="konduktor-engine")
    data = archive.read_bytes()
    size = -(-len(data) // parts)
    entry = {"archive": archive.name, "size": len(data), "sha256": sha(data), "unpacked_size": 1234, "parts": []}
    for i in range(parts):
        chunk = data[i * size:(i + 1) * size]
        part = f"{name}.tar.gz.part{i:02d}"
        (site / part).write_bytes(chunk)
        entry["parts"].append({"name": part, "size": len(chunk), "sha256": sha(chunk)})
    return entry, archive


good, good_archive = make_release(VERSION, "good")
wrong, _ = make_release("0.0.1", "wrong")
config = dict(em.CONFIG, manifest_url=f"{BASE}/manifest-{{version}}.json", asset_url=f"{BASE}/{{name}}")
(site / f"manifest-{VERSION}.json").write_text(json.dumps(
    {"version": VERSION, "targets": {"macos-arm64": good, "windows-x64-cpu": wrong}}))

root = tmp / "root"
mgr = em.EngineManager(root=root, config=config, target="macos-arm64")
check("nothing is installed to begin with", mgr.installed() is None and not mgr.ready())
status = mgr.status()
check("status offers this computer's engine, with its download size",
      status["offered_targets"] == ["macos-arm64"] and status["download_sizes"]["macos-arm64"] == good["size"], str(status))
got = []
rec = mgr.install_engine(on_bytes=got.append)
check("the engine installs from its parts", mgr.installed() is not None and Path(rec["executable"]).is_file())
check("…with byte progress for the whole download", sum(got) == good["size"], str(sum(got)))
check("…and the installed engine was PROBED (it started and named its version)", rec["info"]["version"] == VERSION)
check("the downloaded parts are cleaned up", not any((root / "downloads").glob("*")))

mgr2 = em.EngineManager(root=tmp / "root2", config=config, target="windows-x64-cpu")
try:
    mgr2.install_engine()
    check("an engine reporting the wrong version is refused", False, "installed")
except em.EngineError as ex:
    check("an engine reporting the wrong version is refused, and nothing is installed",
          mgr2.installed() is None and not any((tmp / "root2" / VERSION).glob("*")), str(ex))

(site / good["parts"][1]["name"]).write_bytes(b"corrupt")
mgr3 = em.EngineManager(root=tmp / "root3", config=config, target="macos-arm64")
try:
    mgr3.install_engine()
    check("a corrupt part is refused", False, "installed")
except (download.DownloadError, em.EngineError):
    check("a corrupt part is refused, and nothing is installed", mgr3.installed() is None)

print("== side-loading ==")
mgr4 = em.EngineManager(root=tmp / "root4", config=config, target="macos-arm64")
mgr4.side_load_engine(good_archive)
check("an engine archive the user has is installed after its hash checks out", mgr4.installed() is not None)
bogus = tmp / "bogus.tar.gz"
bogus.write_bytes(b"not an engine")
try:
    em.EngineManager(root=tmp / "root5", config=config, target="macos-arm64").side_load_engine(bogus)
    check("a file that is not the engine is refused", False, "installed")
except em.EngineError:
    check("a file that is not the engine is refused", True)

print("== weights ==")
files = [("htdemucs_ft.yaml", b"models: [a]\n"), ("a.safetensors", os.urandom(200_000))]
for name, data in files:
    (site / "w" / "rev1").mkdir(parents=True, exist_ok=True)
    (site / "w" / "rev1" / name).write_bytes(data)
wmanifest = {"repo": "x/y", "revision": "rev1", "url": f"{BASE}/w/{{revision}}/{{name}}",
             "files": [{"name": n, "size": len(d), "sha256": sha(d)} for n, d in files]}
wm = em.EngineManager(root=tmp / "root6", config=config, weights=wmanifest, target="macos-arm64")
check("weights are not installed to begin with", not wm.weights_installed())
wm.install_weights()
check("the weights install, verified", wm.weights_installed())
side = tmp / "side-weights"
side.mkdir()
for name, data in files:
    (side / name).write_bytes(data)
wm2 = em.EngineManager(root=tmp / "root7", config=config, weights=wmanifest, target="macos-arm64")
wm2.side_load_weights(side)
check("weights from a folder the user supplies are accepted when they verify", wm2.weights_installed())
(side / "a.safetensors").write_bytes(b"tampered")
try:
    em.EngineManager(root=tmp / "root8", config=config, weights=wmanifest, target="macos-arm64").side_load_weights(side)
    check("tampered weights are refused", False, "accepted")
except em.EngineError:
    check("tampered weights are refused", True)
mgr.remove()
check("remove takes the engine away", mgr.installed() is None and not (root / VERSION).exists())

print("== CUDA is offered only where it will run ==")
G = em.NvidiaGpu
check("RTX 40-series, current driver: offered", em.cuda_suitable([G("RTX 4070", "581.08", 8.9)]) is not None)
check("an old driver: not offered", em.cuda_suitable([G("RTX 4070", "552.22", 8.9)]) is None)
check("a GTX 1060 (Pascal, 6.1): not offered", em.cuda_suitable([G("GTX 1060", "581.08", 6.1)]) is None)
check("no card: not offered", em.cuda_suitable([]) is None)

print("== the engine process ==")
exe = Path(mgr4.executable())
pcm = np.random.default_rng(0).uniform(-0.5, 0.5, (2, 44100)).astype(np.float32)
work = tmp / "work"
with EngineProcess(exe, tmp, device="cpu") as eng:
    prog = []
    stems = eng.separate(pcm, work, on_progress=prog.append)
    check("separate returns four stems in drums/bass/other/vocals order",
          len(stems) == 4 and np.allclose(stems[0], pcm * 0.5) and np.allclose(stems[3], pcm * 0.1))
    check("…relaying progress", prog and prog[-1] == 1.0, str(prog))
    check("…and leaves no work files behind", not any(work.rglob("*.f32")), str(list(work.rglob("*"))))
    os.environ["FAKE_DELAY"] = "0.05"
    try:
        eng.separate(pcm, work, cancelled=lambda: True)
        check("a cancel stops the track", False, "finished")
    except EngineCancelled:
        check("a cancel stops the track", True)
    os.environ.pop("FAKE_DELAY")
    check("…and the engine is still usable", len(eng.separate(pcm, work)) == 4)
    proc = eng.proc
check("closing ends the engine process", proc.poll() is not None)
os.environ["FAKE_ERROR"] = "1"
with EngineProcess(exe, tmp) as eng:
    try:
        eng.separate(pcm, work)
        check("an engine error is reported", False)
    except em.EngineError as ex:
        check("an engine error is reported with its message", "bad input" in str(ex), str(ex))
os.environ.pop("FAKE_ERROR")
os.environ["FAKE_CRASH"] = "1"
with EngineProcess(exe, tmp) as eng:
    try:
        eng.separate(pcm, work)
        check("an engine crash is reported", False)
    except em.EngineError as ex:
        check("an engine crash is reported WITH its reason", "out of cheese" in str(ex), str(ex))
os.environ.pop("FAKE_CRASH")
if sys.platform == "darwin":
    import subprocess
    with EngineProcess(exe, tmp) as eng, keep_awake(eng.proc.pid):
        running = subprocess.run(["pgrep", "-f", f"caffeinate -i -w {eng.proc.pid}"], capture_output=True).returncode == 0
    check("the machine is kept awake while the engine runs (caffeinate)", running)

print("== the routes ==")
from fastapi.testclient import TestClient  # noqa: E402
import time as _time  # noqa: E402

import konduktor.main as main  # noqa: E402

# A fresh, uncorrupted release for the routes (an earlier check corrupted `good`).
fresh, _ = make_release(VERSION, "fresh")
(site / f"manifest-{VERSION}.json").write_text(json.dumps({"version": VERSION, "targets": {"macos-arm64": fresh}}))
em._DEFAULT = em.EngineManager(root=tmp / "root-routes", config=config, weights=wmanifest, target="macos-arm64")
with TestClient(main.app) as c:
    st = c.get("/api/stems/engine").json()
    check("status: supported, nothing installed, a download size known",
          st["supported"] and st["installed"] is None and st["download_sizes"]["macos-arm64"] == fresh["size"], str(st)[:300])
    os.environ["FAKE_DELAY"] = "0"
    gate = threading.Event()
    blocker = main.JOBS.submit(main.ENGINE_JOB, lambda h: gate.wait(10))
    second = c.post("/api/stems/engine/install", json={})
    gate.set()
    main.JOBS.wait(blocker.id, 10)
    job = c.post("/api/stems/engine/install", json={}).json()
    end = _time.time() + 30
    while _time.time() < end:
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["state"] != "running":
            break
        _time.sleep(0.05)
    check("install runs as a job counting BYTES (engine + weights)",
          j["state"] == "done" and j["unit"] == "bytes"
          and j["total"] == fresh["size"] + sum(f["size"] for f in wmanifest["files"]), str(j)[:300])
    check("…that ends ready to convert", j["result"] == {"target": "macos-arm64", "ready": True}, str(j["result"]))
    check("an install while another runs is refused (409)", second.status_code == 409, str(second.status_code))
    st = c.get("/api/stems/engine").json()
    check("status now shows the engine and the weights", st["installed"] is not None and st["weights"]["installed"])
    r = c.post("/api/stems/engine/sideload", json={"path": str(bogus), "kind": "engine"})
    check("side-loading a file that is not the engine is refused (400)", r.status_code == 400, r.text[:200])
    r = c.delete("/api/stems/engine")
    check("remove answers with nothing installed", r.status_code == 200 and r.json()["installed"] is None)

server.shutdown()
print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
