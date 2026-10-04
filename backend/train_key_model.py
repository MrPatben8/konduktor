"""Train the key-detection network shipped as `konduktor/core/key_model.npz`.

NOT part of the app or of `run_tests.sh`: a dev tool, run when the model
changes. Two steps in two environments, because the backend never imports
torch and torch is only installed in the stem engine's venv:

    # 0. the data (Beatport EDM previews, hand-annotated keys — ~2.9 GB):
    git clone https://github.com/GiantSteps/giantsteps-mtg-key-dataset DATA/mtg
    git clone https://github.com/GiantSteps/giantsteps-key-dataset DATA/gs
    (cd DATA/mtg && bash audio_dl.sh); (cd DATA/gs && bash audio_dl.sh)

    # 1. features, with the app's own `key_detect.features` (backend venv)
    python train_key_model.py features DATA

    # 2. train + export (engine venv: torch)
    ../engine/.venv/bin/python train_key_model.py train DATA

The protocol is the literature's (Korzeniowski & Widmer 2017): TRAIN on
GiantSteps MTG (1,159 tracks with one confident key), TEST on the separate
GiantSteps key dataset (604) — `bench_key_detect.py` scores it. Choices made
on a held-out 20% of MTG, three seeds each (2026-10-04):
  - the per-frame dense layer over the full pitch axis beat averaging each
    semitone's three bins before it (68.1% vs 66.9% on GiantSteps, val equal);
  - a fixed 30-epoch cosine schedule, keeping the LAST epoch: validation on
    ~230 tracks is too noisy to pick an epoch from;
  - weight decay made no difference and is off.
The shipped file is an ENSEMBLE of three seeds trained on all of MTG, their
logits averaged — each costs ~0.2 s per six-minute track.

Augmentation: the input window (C2..C7) is shifted by up to six semitones
inside the 7-octave spectrogram, and the label rotated with it, so every
tonic is seen in every position; plus random 128-frame (~24 s) crops.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

PC = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
      "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
LO, HI, MAX_SHIFT = 36, 216, 6     # must match key_detect.WINDOW
CROP, BATCH, EPOCHS, LR = 128, 16, 30, 1e-3
SEEDS = (11, 12, 13)
OUT = Path(__file__).parent / "konduktor" / "core" / "key_model.npz"


def label(name: str, mode: str) -> int:
    """Class index: major C..B = 0..11, minor C..B = 12..23 (key_detect's order)."""
    return PC[name] + (12 if mode == "minor" else 0)


def mtg_labels(data: Path) -> dict[str, int]:
    """MTG ids with ONE key annotated at the dataset's top confidence (2)."""
    out = {}
    for line in (data / "mtg/annotations/annotations.txt").read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        m = re.fullmatch(r"([A-G][#b]?) (major|minor)", parts[1].strip())
        if m and parts[2].strip() == "2":
            out[f"{parts[0]}.LOFI"] = label(*m.groups())
    return out


def gs_labels(data: Path) -> dict[str, int]:
    out = {}
    for f in (data / "gs/annotations/key").glob("*.key"):
        name, mode = f.read_text().split()
        out[f.stem] = label(name, mode)
    return out


# ---- step 1: features (backend venv) ---------------------------------------


def _feature_job(args):
    import warnings

    warnings.filterwarnings("ignore")
    import librosa
    import numpy as np
    from konduktor.core import key_detect

    src, dst, md5 = args
    if dst.exists():
        return "cached"
    if not src.exists():
        return "missing"
    if hashlib.md5(src.read_bytes()).hexdigest() != md5:
        return "bad md5"
    y, sr = librosa.load(str(src), sr=key_detect.SR, mono=True)
    np.save(dst, key_detect.features(y, sr))
    return "ok"


def features(data: Path) -> None:
    from collections import Counter
    from concurrent.futures import ProcessPoolExecutor

    for which in ("mtg", "gs"):
        out = data / "features" / which
        out.mkdir(parents=True, exist_ok=True)
        jobs = [
            (data / which / "audio" / f"{m.stem}.mp3", out / f"{m.stem}.npy", m.read_text().split()[0])
            for m in sorted((data / which / "md5").iterdir())
        ]
        with ProcessPoolExecutor() as ex:
            print(which, dict(Counter(ex.map(_feature_job, jobs, chunksize=4))), flush=True)


# ---- step 2: train + export (engine venv) ----------------------------------


def _net(torch):
    nn, F = torch.nn, torch.nn.functional

    class Net(nn.Module):
        """Five 5x5 convs (8 ch, ELU), a dense layer per frame over the whole
        pitch axis (48, ELU, dropout), 24 logits averaged over time.
        `key_detect._network` is this, in numpy."""

        def __init__(self, ch=8, layers=5, hid=48):
            super().__init__()
            self.convs = nn.ModuleList(
                [nn.Conv2d(1 if i == 0 else ch, ch, 5, padding=2) for i in range(layers)])
            self.dense = nn.Conv2d(ch, hid, (HI - LO, 1))
            self.out = nn.Conv2d(hid, 24, 1)
            self.drop = nn.Dropout(0.3)

        def forward(self, x):  # (B, 1, F, T)
            for c in self.convs:
                x = F.elu(c(x))
            x = self.drop(F.elu(self.dense(x)))
            return self.out(x).mean((2, 3))

    return Net


def train(data: Path, seeds=SEEDS, out: Path = OUT) -> None:
    import random

    import numpy as np
    import torch
    import torch.nn.functional as F

    Net = _net(torch)
    labels = mtg_labels(data)
    X, Y = [], []
    for stem, y in sorted(labels.items()):
        f = data / "features/mtg" / f"{stem}.npy"
        if f.exists():
            X.append(np.load(f))
            Y.append(y)
    print(f"training on {len(X)} tracks", flush=True)
    dev = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"

    arrays: dict[str, np.ndarray] = {"pool": np.array(1)}
    for m, seed in enumerate(seeds):
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        net = Net().to(dev)
        opt = torch.optim.Adam(net.parameters(), LR)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, EPOCHS)
        order = list(range(len(X)))
        for ep in range(EPOCHS):
            net.train()
            random.shuffle(order)
            total = 0.0
            for i in range(0, len(order), BATCH):
                xb, yb = [], []
                for j in order[i:i + BATCH]:
                    s = random.randint(-MAX_SHIFT, MAX_SHIFT)
                    x = X[j][LO + 3 * s:HI + 3 * s]
                    if x.shape[1] > CROP:
                        o = random.randint(0, x.shape[1] - CROP)
                        x = x[:, o:o + CROP]
                    else:
                        x = np.pad(x, ((0, 0), (0, CROP - x.shape[1])))
                    xb.append(x)
                    # The window moved UP s semitones, so the music moved DOWN.
                    yb.append((Y[j] % 12 - s) % 12 + Y[j] // 12 * 12)
                xt = torch.from_numpy(np.stack(xb))[:, None].to(dev)
                loss = F.cross_entropy(net(xt), torch.tensor(yb).to(dev))
                opt.zero_grad()
                loss.backward()
                opt.step()
                total += loss.item()
            sched.step()
            print(f"  seed {seed} epoch {ep + 1}/{EPOCHS} loss {total / (len(order) / BATCH):.3f}", flush=True)
        for k, v in net.state_dict().items():
            arrays[f"m{m}." + k.replace("convs.", "conv")] = v.detach().cpu().numpy().astype(np.float32)
    np.savez_compressed(out, **arrays)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(seeds)} members)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("features", "train"))
    ap.add_argument("data", type=Path)
    a = ap.parse_args()
    if a.step == "features":
        sys.path.insert(0, str(Path(__file__).parent))
        features(a.data)
    else:
        train(a.data)
