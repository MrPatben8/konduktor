"""The separation itself: htdemucs_ft over a stereo 44.1 kHz mix.

Kept free of I/O and protocol so the CLI (`__main__`), the frozen engine and
the golden test all run the same code. Weights are loaded from a LOCAL folder
(the four `.safetensors` of `adefossez/HTDemucs-ft` plus `htdemucs_ft.yaml`):
the engine never goes online — Konduktor downloads and verifies the weights,
since their licence rules out shipping them inside the engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

#: Demucs' outputs, in the order the stem file stores them after the mix.
SOURCES = ("drums", "bass", "other", "vocals")
MODEL = "htdemucs_ft"
SAMPLE_RATE = 44100
#: As benchmarked (M3 Pro, 2026-09-29): one random shift, 25 % overlap.
SHIFTS, OVERLAP = 1, 0.25


def available_devices() -> list[str]:
    import torch

    devices = ["cpu"]
    if torch.backends.mps.is_available():
        devices.insert(0, "mps")
    if torch.cuda.is_available():
        devices.insert(0, "cuda")
    return devices


def pick_device(requested: str = "auto") -> str:
    """`auto` = the fastest available; `gpu` = MPS or CUDA, else CPU."""
    devices = available_devices()
    if requested in ("auto", "gpu"):
        return devices[0]
    if requested not in devices:
        raise ValueError(f"Device {requested!r} is not available here ({', '.join(devices)})")
    return requested


def load_model(weights: Path | str):
    """The htdemucs_ft bag from a local weights folder."""
    import yaml
    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model

    folder = Path(weights)
    with open(folder / f"{MODEL}.yaml", encoding="utf-8") as f:
        bag = yaml.safe_load(f)
    models = [load_safetensors_model(folder / f"{sig}.safetensors") for sig in bag["models"]]
    model = BagOfModels(models, bag.get("weights"), bag.get("segment"))
    model.eval()
    return model


def separate(model, pcm: np.ndarray, device: str, *, seed: int = 0,
             on_progress: Callable[[float], None] | None = None) -> dict[str, np.ndarray]:
    """Four stems, each shaped like `pcm` (2, n) float32.

    Seeded per call: `shifts=1` applies a RANDOM time shift, so without a seed
    the same track separates slightly differently each time (measured: Vox 9.2
    vs 9.6 dB SDR between runs) and a re-conversion would not be reproducible.
    The shift is drawn from Python's `random` module (`demucs.apply`:
    `random.randint`), NOT torch's generator — seeding torch alone left every
    run different — so both are seeded.
    """
    import random

    import torch
    from demucs.apply import apply_model

    if pcm.ndim != 2 or pcm.shape[0] != 2:
        raise ValueError("Expected a stereo mix shaped (2, n)")
    n = pcm.shape[1]
    random.seed(seed)
    torch.manual_seed(seed)

    def callback(d: dict) -> None:
        if on_progress is None or d.get("state") != "end":
            return
        models = max(1, int(d.get("models", 1)))
        done = min(1.0, (int(d.get("segment_offset", 0)) + 1) / max(1, n))
        on_progress(min(1.0, (int(d.get("model_idx_in_bag", 0)) + done) / models))

    mix = torch.from_numpy(np.ascontiguousarray(pcm, dtype=np.float32))[None]
    with torch.no_grad():
        out = apply_model(model, mix, shifts=SHIFTS, split=True, overlap=OVERLAP,
                          progress=False, device=device, callback=callback)[0]
    out = out.cpu().numpy().astype(np.float32, copy=False)
    order = [model.sources.index(name) for name in SOURCES]
    return {name: np.ascontiguousarray(out[i]) for name, i in zip(SOURCES, order)}
