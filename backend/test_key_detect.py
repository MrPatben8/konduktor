"""Tests for key detection (`core/key_detect.py`) on synthetic audio.

Needs no audio files: every track is generated, so its key is KNOWN. These
pin what `bench_key_detect.py` (real music) cannot: that all 24 keys come out
— a wrong label order or wheel mapping would get exactly some of them wrong —
that loudness and sample rate do not change the answer, that silence gives no
key rather than a made-up one, and that the numpy network computes what was
trained (the convolution against a direct reference implementation).

The progressions are cadences that state their key unambiguously (I-IV-V-I;
i-iv-V-i with the raised leading tone). A pop I-V-vi-IV is deliberately NOT
used: it spends a quarter of its time on the relative minor, and a model
trained on dance music — 85% minor — fairly calls some of those minor.
"""
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from konduktor.core import key_detect  # noqa: E402

SR = 22050
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def tone(midi: int, dur: float, amp: float, harmonics: int = 6, sr: int = SR) -> np.ndarray:
    t = np.arange(int(dur * sr)) / sr
    f = 440.0 * 2 ** ((midi - 69) / 12)
    x = sum(np.sin(2 * np.pi * f * h * t) / h for h in range(1, harmonics + 1) if f * h < sr / 2)
    env = np.minimum(1, t / 0.01) * np.minimum(1, (dur - t) / 0.05)
    return amp * x * env


MAJOR = [(0, "M"), (5, "M"), (7, "M"), (0, "M")]   # I IV V I
MINOR = [(0, "m"), (5, "m"), (7, "M"), (0, "m")]   # i iv V i


def song(tonic: int, mode: str, bars: int = 16, bpm: float = 124.0, sr: int = SR) -> np.ndarray:
    beat = 60.0 / bpm
    prog = MAJOR if mode == "major" else MINOR
    out = []
    for b in range(bars):
        root, q = prog[b % len(prog)]
        r = 48 + (tonic + root) % 12
        chord = sum(tone(r + 12 + i, 4 * beat, 0.12, sr=sr) for i in (0, 4 if q == "M" else 3, 7))
        bass = np.concatenate([tone(r - 12, beat, 0.3, 3, sr=sr) for _ in range(4)])
        n = min(len(chord), len(bass))
        out.append(chord[:n] + bass[:n])
    y = np.concatenate(out)
    return (y / np.abs(y).max() * 0.8).astype(np.float32)


def name(pc: int, mode: str) -> str:
    return key_detect.PITCH_NAMES[pc] + ("m" if mode == "minor" else "")


print("all 24 keys")
for mode in ("major", "minor"):
    for pc in range(12):
        r = key_detect.detect_key_samples(song(pc, mode), SR)
        check(f"{name(pc, mode):4s} -> {r.name if r else None}",
              r is not None and (r.pitch_class, r.mode) == (pc, mode))
        if r:
            check(f"{name(pc, mode):4s} wheel {r.wheel}", r.wheel == key_detect.camelot_wheel(pc, mode))

print("the Camelot mapping")
check("C major = 8B", key_detect.camelot_wheel(0, "major") == 8)
check("A minor = 8A", key_detect.camelot_wheel(9, "minor") == 8)
check("G major = 9B (a fifth up = one step)", key_detect.camelot_wheel(7, "major") == 9)
check("F minor = 4A", key_detect.camelot_wheel(5, "minor") == 4)

print("invariance")
y = song(2, "minor")
base = key_detect.detect_key_samples(y, SR)
quiet = key_detect.detect_key_samples(y * 0.05, SR)
check("a quiet master gives the same key", quiet and quiet.name == base.name)
check("...and the same confidence", quiet and abs(quiet.confidence - base.confidence) < 0.02,
      f"{base.confidence:.3f} vs {quiet and quiet.confidence:.3f}")
hi = key_detect.detect_key_samples(song(2, "minor", sr=44100), 44100)
check("44.1 kHz input gives the same key", hi and hi.name == base.name)
check("confidence is a probability", 0.0 < base.confidence <= 1.0)

print("nothing to say")
check("silence -> None", key_detect.detect_key_samples(np.zeros(SR * 30, np.float32), SR) is None)
check("under 5 s -> None", key_detect.detect_key_samples(song(0, "major")[: SR * 3], SR) is None)

print("the numpy network")
rng = np.random.default_rng(1)
x = rng.standard_normal((3, 20, 300)).astype(np.float32)   # > one time chunk, odd sizes
w = rng.standard_normal((4, 3, 5, 5)).astype(np.float32)
b = rng.standard_normal(4).astype(np.float32)
got = key_detect._conv5(x, w, b)
xp = np.pad(x, ((0, 0), (2, 2), (2, 2)))
ref = np.zeros_like(got)
for o in range(4):
    for i in range(3):
        for di in range(5):
            for dj in range(5):
                ref[o] += w[o, i, di, dj] * xp[i, di:di + 20, dj:dj + 300]
    ref[o] += b[o]
check("5x5 'same' conv == direct cross-correlation", np.allclose(got, ref, atol=1e-3),
      f"max diff {np.abs(got - ref).max():.2e}")
members = key_detect._members()
check("the shipped model is an ensemble of >= 1 network", len(members) >= 1)
spec = key_detect.features(y, SR)
check("24 logits", key_detect.logits(spec).shape == (24,))

print("\nFAILED" if failed else "\nall key-detection tests passed")
raise SystemExit(1 if failed else 0)
