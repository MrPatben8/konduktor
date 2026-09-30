"""DEV ONLY — convert audio files to stem files with an UNFROZEN engine.

Not shipped, not in run_tests.sh. It exists so real stem files can be made (and
checked in Traktor) before the frozen engine and its download exist: the stem
writer (`core/stem_file.py`) runs here in the backend venv, and the separation
runs in any Python that has torch + demucs, through the engine's own one-shot
CLI — the same raw-f32 exchange the batch will use.

    python dev_stems.py --engine-python ~/demucs-venv/bin/python \\
        --weights ~/.cache/huggingface/hub/models--adefossez--HTDemucs-ft/snapshots/<rev> \\
        [--out-dir DIR] [--device auto] FILE...

Each FILE becomes `<name>.stem.m4a` beside it (or in --out-dir); an existing
target is skipped, never overwritten.

With `--collection NML` it also does what the batch will do at its end: park
each original (`.<name>.konduktor-parked`, beside it), repoint the collection
entry that names it at the stem file (`apply_stem_swaps`), and SAVE — after
copying the collection into its `backups/` folder. Traktor must be closed.
`--keep-audio-id` keeps Traktor's analysis fingerprint on the swapped entries
(the milestone-2 check compares kept vs cleared).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402

from konduktor.core import stem_file as sf  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"


def engine_separator(python: str, weights: str, device: str):
    def separate(pcm: np.ndarray) -> list[np.ndarray]:
        with tempfile.TemporaryDirectory(prefix="konduktor-dev-stems-") as d:
            mix = Path(d) / "mix.f32"
            np.ascontiguousarray(pcm, dtype=np.float32).tofile(mix)
            cmd = [python, "-m", "konduktor_engine", "separate", "--input", str(mix),
                   "--samples", str(pcm.shape[1]), "--output-dir", str(Path(d) / "out"),
                   "--weights", weights, "--device", device]
            proc = subprocess.Popen(cmd, cwd=ENGINE_DIR, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            last = None
            for line in proc.stdout:
                msg = json.loads(line)
                if msg["event"] == "progress":
                    print(f"\r    separating {msg['fraction'] * 100:5.1f} %", end="", flush=True)
                last = msg
            proc.wait()
            print()
            if not last or last["event"] != "done":
                raise sf.StemFileError(f"engine failed: {last}")
            return [np.fromfile(Path(d) / "out" / f"{name}.f32", dtype=np.float32).reshape(2, -1)
                    for name in sf.DEMUCS_ORDER]
    return separate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-python", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out-dir")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--collection")
    ap.add_argument("--keep-audio-id", action="store_true")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    separate = engine_separator(args.engine_python, args.weights, args.device)
    adapter = None
    swaps = []
    if args.collection:
        if subprocess.run(["pgrep", "-x", "Traktor"], capture_output=True).returncode == 0:
            print("Traktor is running — close it first (it rewrites the collection on exit).")
            return 1
        from konduktor.adapters.traktor.adapter import TraktorAdapter
        adapter = TraktorAdapter(Path(args.collection))
        adapter.store._KEEP_AUDIO_ID_ON_STEM = args.keep_audio_id
        by_path = {str(adapter.audio_path(t.id)): t.id for t in adapter.tracks}
    for f in (Path(x).expanduser().absolute() for x in args.files):
        target = (Path(args.out_dir) if args.out_dir else f.parent) / sf.stem_target_name(f.name)
        if target.exists():
            print(f"skip (exists): {target}")
            continue
        partial = target.with_name(target.name + ".konduktor-partial")
        t0 = time.time()
        print(f"{f.name}")
        try:
            written = sf.build(f, partial, separate)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, target)
        print(f"  -> {target.name}  {written.duration:.0f} s, {written.bit_rate // 1000} kbps x5, "
              f"{written.size / 1e6:.1f} MB, {time.time() - t0:.0f} s")
        if adapter is not None:
            from konduktor.core.adapter import StemSwap
            tid = by_path.get(str(f))
            if tid is None:
                print(f"  (not in the collection: {f})")
                continue
            parked = f.with_name(f".{f.name}.konduktor-parked")
            os.replace(f, parked)
            swaps.append(StemSwap(tid, target, "repoint", parked, written.bit_rate,
                                  written.duration, written.size))
    if adapter is not None and swaps:
        import shutil
        nml = Path(args.collection)
        backups = nml.parent / "backups"
        backups.mkdir(exist_ok=True)
        shutil.copy2(nml, backups / f"{nml.name}.{time.strftime('%Y%m%d-%H%M%S')}.dev-stems.bak")
        res = adapter.apply_stem_swaps(swaps)
        adapter.save()
        print(f"swapped {len(res.renamed)} entries; clamped: {res.clamped or 'none'}; saved {nml}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
