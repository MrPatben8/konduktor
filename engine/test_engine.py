"""The stem engine's process protocol, and (with --frozen) the golden test.

    .venv/bin/python test_engine.py --weights DIR [--frozen dist/konduktor-engine/konduktor-engine]

Needs the htdemucs_ft weights folder (`htdemucs_ft.yaml` + four
`.safetensors`); defaults to the Hugging Face cache snapshot when present.
Runs on CPU so results are comparable across machines (CI runners have no GPU).

Pins what would fail silently or hang the app:
  * `serve` loads once, separates repeatedly, and the SAME seed gives the SAME
    stems — a re-conversion is reproducible;
  * a cancel stops the running separation and the process stays usable;
  * closing stdin (the backend died) ends the process even mid-separation —
    otherwise an orphaned engine keeps a CPU pinned for an hour;
  * the frozen build separates bit-for-bit like the unfrozen code (golden).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def default_weights() -> str | None:
    snaps = sorted(Path.home().glob(".cache/huggingface/hub/models--adefossez--HTDemucs-ft/snapshots/*/"))
    return str(snaps[-1]) if snaps else None


def clip(seconds: float, seed: int = 0) -> np.ndarray:
    """Something with drums, bass and a tone: enough for every model in the bag."""
    sr = 44100
    n = int(seconds * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(seed)
    x = 0.3 * np.sin(2 * np.pi * 55 * t) + 0.1 * np.sin(2 * np.pi * 440 * t)
    for start in range(0, n, sr // 2):
        k = min(2000, n - start)
        x[start:start + k] += rng.uniform(-0.6, 0.6, k) * np.linspace(1, 0, k)
    return np.stack([x, x * 0.9]).astype(np.float32)


class Engine:
    def __init__(self, cmd: list[str], weights: str):
        self.proc = subprocess.Popen(cmd + ["serve", "--weights", weights, "--device", "cpu"],
                                     cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def send(self, **msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("engine closed its output")
        return json.loads(line)

    def until(self, *events, rid=None):
        seen = []
        while True:
            m = self.recv()
            seen.append(m)
            if m.get("event") in events and (rid is None or m.get("id") == rid):
                return m, seen

    def separate(self, rid, pcm, out: Path, seed=0):
        src = out / f"{rid}.f32"
        pcm.tofile(src)
        self.send(cmd="separate", id=rid, input=str(src), samples=pcm.shape[1], output_dir=str(out / rid), seed=seed)


def read_stems(folder: Path):
    return {n: np.fromfile(folder / f"{n}.f32", dtype=np.float32) for n in ("drums", "bass", "other", "vocals")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=default_weights())
    ap.add_argument("--frozen")
    args = ap.parse_args()
    if not args.weights:
        print("no weights folder: pass --weights")
        return 2
    unfrozen = [sys.executable, "-m", "konduktor_engine"]
    tmp = Path(tempfile.mkdtemp(prefix="konduktor-engine-test-"))

    print("== info ==")
    info = json.loads(subprocess.run(unfrozen + ["info"], cwd=HERE, capture_output=True, text=True).stdout)
    check("info reports the version, model and devices",
          info.get("event") == "info" and info.get("model") == "htdemucs_ft" and "cpu" in info.get("devices", []), str(info))

    print("== serve ==")
    e = Engine(unfrozen, args.weights)
    ready = e.recv()
    check("serve loads the model once and says so", ready.get("event") == "ready", str(ready))
    e.send(cmd="ping")
    check("ping -> pong", e.recv().get("event") == "pong")
    pcm = clip(8)
    e.separate("a", pcm, tmp, seed=0)
    done, seen = e.until("done", "error", rid="a")
    check("a separation completes", done["event"] == "done", str(done))
    check("…reporting progress on the way", any(m.get("event") == "progress" for m in seen))
    a = read_stems(tmp / "a")
    check("four stems, each the mix's size", all(x.size == pcm.size for x in a.values()))
    e.separate("b", pcm, tmp, seed=0)
    e.until("done", rid="b")
    b = read_stems(tmp / "b")
    check("the same seed gives the SAME stems (reproducible)", all(np.array_equal(a[k], b[k]) for k in a))
    e.separate("c", clip(30, seed=1), tmp)
    e.until("progress", rid="c")
    e.send(cmd="cancel", id="c")
    m, _ = e.until("cancelled", "done", "error", rid="c")
    check("a cancel stops the running separation", m["event"] == "cancelled", str(m))
    e.send(cmd="ping")
    check("…and the engine is still usable", e.until("pong")[0]["event"] == "pong")

    e.separate("d", clip(60, seed=2), tmp)
    e.until("progress", rid="d")
    t0 = time.time()
    e.proc.stdin.close()  # the backend died
    try:
        e.proc.wait(timeout=10)
        check("closing stdin ends the engine even mid-separation", True)
    except subprocess.TimeoutExpired:
        e.proc.kill()
        check("closing stdin ends the engine even mid-separation", False, "still running after 10 s")
    print(f"  (exited {time.time() - t0:.2f} s after stdin closed)")

    if args.frozen:
        print("== golden: frozen vs unfrozen ==")
        fe = Engine([args.frozen], args.weights)
        check("the frozen engine starts and loads the model", fe.recv().get("event") == "ready")
        fe.separate("f", pcm, tmp, seed=0)
        fdone, _ = fe.until("done", "error", rid="f")
        check("the frozen engine separates", fdone["event"] == "done", str(fdone))
        f = read_stems(tmp / "f")
        worst = max(float(np.max(np.abs(f[k] - a[k]))) for k in a)
        check("…bit-for-bit like the unfrozen code", worst == 0.0, f"max |diff| {worst:.3g}")
        fe.proc.stdin.close()
        fe.proc.wait(timeout=10)

    print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
