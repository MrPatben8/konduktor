"""Musical key detection: a small convolutional network, run in numpy.

Why a network and not key profiles. The classic method — fold the spectrum
into a 12-bin chroma vector and correlate it with a major / minor template —
was benchmarked first on GiantSteps (604 Beatport previews, keys corrected by
hand), over Krumhansl, Temperley, Sha'ath (KeyFinder) and Faraldo's EDM
templates and every combination of octave range, compression and harmonic
filtering tried: the best got 49.5% exactly right. A template sees only how
much of each pitch class there is; a network also sees WHERE in pitch it is
(the bass line, the pad, the lead) and learns from data what states a key.

The model follows Korzeniowski & Widmer, "End-to-end musical key estimation
using a convolutional neural network" (EUSIPCO 2017): five 5x5 convolutions,
then a per-frame dense layer over the whole pitch axis — a key is NOT
pitch-invariant, so nothing pools over frequency — then 24 logits averaged
over the track. ~76 k weights per network; `key_model.npz` holds an ensemble
of three (logits averaged), trained by `backend/train_key_model.py` on the
SEPARATE GiantSteps MTG dataset. torch is used only to train: the backend
never imports it.

Measured (`bench_key_detect.py`, 2026-10-04): GiantSteps 69.4% exact, 83.4%
harmonically mixable (exact, a fifth, or the relative key), MIREX 0.758 —
about the published figure for this design. Against the key tags of 47 local
files (Traktor's analysis, not ground truth) it agrees on 62%, 77% mixable.
`confidence` tracks correctness (82% exact above 0.7, ~40% below 0.4) but even
a low one is ten times better than chance, so a key is always returned for
music; only silence and clips under 5 s get None.

Cost on a 6.7-minute track: CQT 0.1 s, the three networks 0.5 s — beside the
decode, which the grid analysis shares.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np

from . import musical_key

SR = 22050
HOP = 4096                 # ~5.4 frames/s: key is a slow property
BINS_PER_OCTAVE = 36       # three per semitone, so a detuned track still lands in one
FMIN_NOTE = "C1"
OCTAVES = 7                # C1..C8: the input window plus room to pitch-shift it
# The network's input: C2..C7 (65 Hz - 2.1 kHz), where bass lines, pads and
# leads state the key. The octave either side is only there so training can
# shift the window by up to six semitones.
WINDOW = (36, 216)

_MODEL = Path(__file__).with_name("key_model.npz")
_TIME_CHUNK = 256          # frames per im2col block: bounds memory to tens of MB

PITCH_NAMES = musical_key.SPELLING


@dataclass(frozen=True)
class KeyResult:
    pitch_class: int                       # tonic, 0 = C
    mode: Literal["major", "minor"]
    wheel: int                             # Camelot position 1-12, the generic model's key
    confidence: float                      # the network's probability for this key, 0-1

    @property
    def name(self) -> str:
        return PITCH_NAMES[self.pitch_class] + ("m" if self.mode == "minor" else "")


def camelot_wheel(pitch_class: int, mode: str) -> int:
    """Camelot position: C major = 8B, A minor = 8A; one step = a fifth."""
    return musical_key.wheel_of(pitch_class, mode)


def features(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """Log-magnitude constant-Q spectrogram, (OCTAVES * 36, frames), float32.

    Normalised to the track's own loudest bin, so a quiet master and a loud
    one give the same picture. Training calls this same function."""
    import librosa

    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
    C = np.abs(librosa.cqt(
        y, sr=SR, hop_length=HOP, fmin=librosa.note_to_hz(FMIN_NOTE),
        n_bins=OCTAVES * BINS_PER_OCTAVE, bins_per_octave=BINS_PER_OCTAVE, tuning=0.0,
    ))
    peak = float(C.max()) or 1.0
    return np.log1p(1000.0 * C / peak).astype(np.float32)


@lru_cache(maxsize=1)
def _members() -> list[dict[str, np.ndarray]]:
    """The ensemble: `key_model.npz` holds N networks as `m<i>.<param>`, plus
    `pool` (frequency bins averaged before the dense layer; 1 = none)."""
    with np.load(_MODEL) as z:
        flat = {k: z[k] for k in z.files}
    pool = int(flat.pop("pool"))
    members: dict[int, dict[str, np.ndarray]] = {}
    for k, v in flat.items():
        i, name = k.split(".", 1)
        members.setdefault(int(i[1:]), {})[name] = v.astype(np.float32)
    for m in members.values():
        m["pool"] = np.array(pool)
    return [members[i] for i in sorted(members)]


def _elu(x: np.ndarray) -> np.ndarray:
    return np.where(x > 0, x, np.expm1(np.minimum(x, 0)))


def _conv5(x: np.ndarray, w: np.ndarray, b: np.ndarray) -> np.ndarray:
    """'same' 5x5 convolution (cross-correlation, as torch's Conv2d).
    x: (cin, F, T), w: (cout, cin, 5, 5) -> (cout, F, T)."""
    cin, f, t = x.shape
    xp = np.pad(x, ((0, 0), (2, 2), (2, 2)))
    out = np.empty((w.shape[0], f, t), np.float32)
    for t0 in range(0, t, _TIME_CHUNK):
        t1 = min(t, t0 + _TIME_CHUNK)
        view = np.lib.stride_tricks.sliding_window_view(xp[:, :, t0:t1 + 4], (5, 5), axis=(1, 2))
        out[:, :, t0:t1] = np.tensordot(w, view, axes=([1, 2, 3], [0, 3, 4]))
    return out + b[:, None, None]


def _network(x: np.ndarray, w: dict[str, np.ndarray]) -> np.ndarray:
    """One member's 24 logits, averaged over time. x: (1, F, T)."""
    n = sum(1 for k in w if k.startswith("conv") and k.endswith(".weight"))
    for i in range(n):
        x = _elu(_conv5(x, w[f"conv{i}.weight"], w[f"conv{i}.bias"]))
    pool = int(w["pool"])                               # average within a semitone
    if pool > 1:
        f = x.shape[1] // pool * pool
        x = x[:, :f].reshape(x.shape[0], f // pool, pool, -1).mean(axis=2)
    dw = w["dense.weight"][..., 0]                      # (hid, ch, F)
    h = _elu(np.tensordot(dw, x, axes=([1, 2], [0, 1])) + w["dense.bias"][:, None])
    out = w["out.weight"][:, :, 0, 0] @ h + w["out.bias"][:, None]   # (24, T)
    return out.mean(axis=1)


def logits(spec: np.ndarray) -> np.ndarray:
    """The ensemble's 24 logits — major C..B, then minor C..B — for a
    `features()` spectrogram: each member's, averaged."""
    x = spec[WINDOW[0]:WINDOW[1]][None]
    return np.mean([_network(x, w) for w in _members()], axis=0)


def detect_key_samples(y: np.ndarray, sr: int) -> KeyResult | None:
    """The key of decoded mono audio, or None for silence / too short to say."""
    if y.size < sr * 5 or not np.any(y):
        return None
    z = logits(features(y, sr)).astype(np.float64)
    p = np.exp(z - z.max())
    p /= p.sum()
    k = int(np.argmax(p))
    pc, mode = k % 12, ("minor" if k >= 12 else "major")
    return KeyResult(pc, mode, camelot_wheel(pc, mode), float(p[k]))


def with_detected_key(track, y: np.ndarray | None, sr: int):
    """`track` (a generic `Track`) with a detected key if it has none of its
    own — for a file being ADDED, which is decoded anyway. A key the track
    already carries (its tags, its source library) is never second-guessed,
    and a detection failure leaves the track as it was: a missing key is not
    worth failing an add over."""
    if getattr(track, "key_wheel", None) is not None or y is None:
        return track
    try:
        found = detect_key_samples(y, sr)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("key detection failed", exc_info=True)
        return track
    if found is None:
        return track
    return track.model_copy(update={"key": found.name, "key_wheel": found.wheel, "key_mode": found.mode})


def detect_key(audio_path: str) -> KeyResult | None:
    import librosa

    y, sr = librosa.load(audio_path, sr=SR, mono=True)
    return detect_key_samples(y, sr)
