"""Test helpers for Convert to Stems: a FAKE engine and generated audio.

Not a test itself. The fake engine speaks the real `serve` protocol
(`engine/konduktor_engine/serve.py`) — no torch — returning scaled copies of
the mix as its "stems". Environment switches, read per separation:
  FAKE_DELAY  seconds per progress step (10 steps)   FAKE_ERROR  reply "error"
  FAKE_CRASH  die mid-request (stderr says why)       FAKE_COUNT  append a line
                                                      per separation to this file
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import av
import numpy as np

FAKE_ENGINE = r'''#!{python}
import json, os, queue, sys, threading, time
import numpy as np
if sys.argv[1] == "info":
    print(json.dumps({{"event": "info", "version": "{version}", "devices": ["cpu"]}}))
    sys.exit(0)
print(json.dumps({{"event": "ready", "device": "cpu"}}), flush=True)
cancel, Q = set(), queue.Queue()
def reader():
    for line in sys.stdin:
        m = json.loads(line)
        (cancel.add(m["id"]) if m.get("cmd") == "cancel" else Q.put(m))
    os._exit(0)
threading.Thread(target=reader, daemon=True).start()
while True:
    m = Q.get()
    rid = m["id"]
    x = np.fromfile(m["input"], dtype=np.float32).reshape(2, -1)
    if os.environ.get("FAKE_COUNT"):
        with open(os.environ["FAKE_COUNT"], "a") as f:
            f.write(rid + "\n")
    if os.environ.get("FAKE_CRASH"):
        print("boom: out of cheese", file=sys.stderr, flush=True); os._exit(3)
    if os.environ.get("FAKE_ERROR"):
        print(json.dumps({{"id": rid, "event": "error", "message": "bad input"}}), flush=True); continue
    stopped = False
    for i in range(10):
        time.sleep(float(os.environ.get("FAKE_DELAY", "0.005")))
        if rid in cancel:
            print(json.dumps({{"id": rid, "event": "cancelled"}}), flush=True); stopped = True; break
        print(json.dumps({{"id": rid, "event": "progress", "fraction": (i + 1) / 10}}), flush=True)
    if stopped:
        continue
    os.makedirs(m["output_dir"], exist_ok=True)
    for k, g in zip(("drums", "bass", "other", "vocals"), (0.5, 0.25, -0.2, 0.1)):
        (x * g).astype(np.float32).tofile(os.path.join(m["output_dir"], k + ".f32"))
    print(json.dumps({{"id": rid, "event": "done", "device": "cpu"}}), flush=True)
'''


def write_fake_engine(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_ENGINE.format(python=sys.executable, version=version))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def install_fake_engine(root: Path):
    """An `EngineManager` whose engine is the fake one and whose weights are two
    tiny files — `ready()` without any download."""
    from konduktor.stems import engine_manager as em

    files = [("htdemucs_ft.yaml", b"models: [a]\n"), ("a.safetensors", b"\0" * 64)]
    weights = {"repo": "x/y", "revision": "fake", "url": "http://invalid/{name}",
               "files": [{"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in files]}
    mgr = em.EngineManager(root=root, weights=weights, target="macos-arm64")
    exe = write_fake_engine(root / mgr.version / "macos-arm64" / "konduktor-engine" / "konduktor-engine", mgr.version)
    (root / "installed.json").write_text(json.dumps(
        {"version": mgr.version, "target": "macos-arm64", "size": 1, "executable": str(exe), "info": {}}))
    wd = mgr.weights_dir()
    wd.mkdir(parents=True, exist_ok=True)
    for n, d in files:
        (wd / n).write_bytes(d)
    assert mgr.ready()
    return mgr


def make_mp3(path: Path, seconds: float = 4, seed: int = 3) -> Path:
    """An MP3 WITH a Xing/Info header (so Traktor's 2257-sample offset applies)."""
    sr = 44100
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    x = np.zeros((2, n), np.float32)
    for start in range(sr // 2, n - sr // 10, sr // 2):
        x[:, start:start + 2000] = rng.uniform(-0.5, 0.5, (2, 2000))
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="mp3") as c:
        s = c.add_stream("libmp3lame", rate=sr, layout="stereo")
        s.bit_rate = 320_000
        for o in range(0, n, 1152):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1152]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = sr, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    return path
