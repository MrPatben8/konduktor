"""Command line: `info` and a one-shot `separate`.

Audio crosses as RAW planar float32 files — shape (2, n), C order — rather than
WAV: nothing to parse, `np.fromfile`/`memmap` either side, and no clamping (a
mastered track decodes above 0 dBFS as float, and its stems may too).

Protocol output is JSON lines on stdout; everything else (torch and demucs
print) goes to stderr, so a stray print can never corrupt a message.

    python -m konduktor_engine info
    python -m konduktor_engine separate --input mix.f32 --samples N \\
        --output-dir OUT --weights DIR [--device auto|cpu|gpu|mps|cuda] [--seed 0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import VERSION


def _protocol():
    """A private copy of stdout for messages; `sys.stdout` becomes stderr."""
    fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "w", buffering=1, encoding="utf-8")


def _emit(out, **msg) -> None:
    out.write(json.dumps(msg) + "\n")
    out.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="konduktor-engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    sep = sub.add_parser("separate")
    sep.add_argument("--input", required=True)
    sep.add_argument("--samples", type=int, required=True)
    sep.add_argument("--output-dir", required=True)
    sep.add_argument("--weights", required=True)
    sep.add_argument("--device", default="auto")
    sep.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    out = _protocol()
    from . import core

    if args.command == "info":
        import torch

        _emit(out, event="info", version=VERSION, torch=torch.__version__,
              devices=core.available_devices(), model=core.MODEL)
        return 0

    import numpy as np

    try:
        device = core.pick_device(args.device)
        pcm = np.fromfile(args.input, dtype=np.float32)
        if pcm.size != 2 * args.samples:
            raise ValueError(f"{args.input}: expected {2 * args.samples} floats, found {pcm.size}")
        pcm = pcm.reshape(2, args.samples)
        t0 = time.time()
        model = core.load_model(args.weights)
        _emit(out, event="loaded", device=device, seconds=round(time.time() - t0, 2))
        stems = core.separate(model, pcm, device, seed=args.seed,
                              on_progress=lambda f: _emit(out, event="progress", fraction=round(f, 4)))
        folder = Path(args.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for name, x in stems.items():
            x.tofile(folder / f"{name}.f32")
        _emit(out, event="done", seconds=round(time.time() - t0, 2), device=device)
        return 0
    except Exception as ex:  # noqa: BLE001 — reported to the caller, not raised
        _emit(out, event="error", message=f"{type(ex).__name__}: {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
