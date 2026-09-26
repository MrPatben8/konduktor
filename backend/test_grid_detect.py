"""Tests for beatgrid detection (`core/grid_detect.py`) on synthetic audio.

Needs no audio files and no library: every track here is generated, so its
tempo and first beat are KNOWN rather than taken from another analyser. That
is what these can pin that `bench_grid_detect.py` (real music, scored against
Rekordbox) cannot: exactness, and each trap that fooled a band in isolation —
the loud off-beat hat, the syncopated bassline, and the frame-quantised tempo
the previous detector could not escape.
"""
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from konduktor.core.grid_detect import detect_grid  # noqa: E402

SR = 22050
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


rng = np.random.default_rng(0)


def _faded(x: np.ndarray) -> np.ndarray:
    """End a sound smoothly: a hard cut is itself a click, half a beat after the
    kick, which no real sample has and which would test the generator instead."""
    n = min(len(x), int(0.02 * SR))
    x = x.copy()
    x[-n:] *= np.linspace(1.0, 0.0, n)
    return x


def _kick() -> np.ndarray:
    t = np.arange(int(0.25 * SR)) / SR
    body = np.sin(2 * np.pi * (50 * t + 60 * (1 - np.exp(-t / 0.02)) * 0.02)) * np.exp(-t / 0.12)
    click = np.zeros_like(t)
    click[: int(0.002 * SR)] = rng.standard_normal(int(0.002 * SR)) * 0.5
    return _faded(body + click)


def _hat(amp: float) -> np.ndarray:
    n = int(0.04 * SR)
    noise = np.diff(rng.standard_normal(n + 1))  # tilt up: hat-like
    return _faded(amp * noise * np.exp(-np.arange(n) / SR / 0.012))


def _bass(amp: float) -> np.ndarray:
    t = np.arange(int(0.2 * SR)) / SR
    ramp = np.minimum(1.0, t / 0.015)  # a bass note swells; it has no click
    return _faded(amp * np.sin(2 * np.pi * 70 * t) * ramp * np.exp(-t / 0.15))


def track(bpm, first=0.3, seconds=180.0, hat=0.0, bass=0.0, tempo_b=None):
    """Kick on every beat from ``first``; optional off-beat hat and a bass note
    60% of the way to the next beat. ``tempo_b`` is the tempo at the END: the
    tempo glides linearly from ``bpm`` to it, as a live drummer's does."""
    y = np.zeros(int(seconds * SR))

    def put(x, at):
        i = int(round(at * SR))
        if 0 <= i < len(y):
            j = min(len(y), i + len(x))
            y[i:j] += x[: j - i]

    t, beat = first, 60.0 / bpm
    k, h, b = _kick(), _hat(hat), _bass(bass)
    while t < seconds:
        put(k, t)
        if hat:
            put(h, t + beat / 2)
        if bass:
            put(b, t + 0.6 * beat)
        if tempo_b:
            beat = 60.0 / (bpm + (tempo_b - bpm) * t / seconds)
        t += beat
    return (0.3 * y).astype(np.float32)


def detect(y, **kw):
    return detect_grid("", y=y, sr=SR, **kw)


def beat_error_ms(res, bpm, first, at):
    """Distance from the true beat nearest ``at`` to the detected grid's."""
    p = 60.0 / bpm
    true = first + round((at - first) / p) * p
    dp = 60.0 / res.bpm
    got = res.anchor + round((true - res.anchor) / dp) * dp
    return (got - true) * 1000.0


print("== round tempo: exact BPM, anchor on the first kick ==")
r = detect(track(124, first=0.3))
check("124 BPM detected as exactly 124", r.bpm == 124.0, f"got {r.bpm}")
check("anchor on the first kick (±3 ms)", abs(r.anchor - 0.3) <= 0.003, f"got {r.anchor}")
check("still on the beat at the end (±3 ms)", abs(beat_error_ms(r, 124, 0.3, 175)) <= 3)
check("a constant track reports little drift", r.drift_ms is not None and r.drift_ms <= 5, f"{r.drift_ms}")

print("== the old detector's blind spot: 125 BPM ==")
# librosa's frame-quantised tempo could only return 123.05 or 129.20 here.
r = detect(track(125, first=0.05))
check("125 BPM is 125, not 123.05", r.bpm == 125.0, f"got {r.bpm}")

print("== non-round tempo is fitted, not snapped ==")
r = detect(track(123.37, first=0.2))
check("123.37 within 0.005", abs(r.bpm - 123.37) <= 0.005, f"got {r.bpm}")
check("…and on the beat at the end (±5 ms)", abs(beat_error_ms(r, 123.37, 0.2, 175)) <= 5)

print("== octave: a four-on-the-floor kick is not half or double tempo ==")
r = detect(track(128))
check("128 BPM, not 64 or 256", r.bpm == 128.0, f"got {r.bpm}")
r = detect(track(90), bpm_range=(100.0, 200.0))
check("bpm_range folds 90 up to 180", r.bpm == 180.0, f"got {r.bpm}")

print("== phase traps: each fooled one band on real music ==")
r = detect(track(126, first=0.4, hat=1.5))
check("an off-beat hat louder than the kick does not take the beat",
      abs(beat_error_ms(r, 126, 0.4, 60)) <= 3, f"{beat_error_ms(r, 126, 0.4, 60):+.1f} ms")
r = detect(track(124, first=0.25, bass=1.5))
check("a syncopated bassline does not take the beat",
      abs(beat_error_ms(r, 124, 0.25, 60)) <= 3, f"{beat_error_ms(r, 124, 0.25, 60):+.1f} ms")
r = detect(track(124, first=0.25, hat=1.2, bass=1.2))
check("…nor both together", abs(beat_error_ms(r, 124, 0.25, 60)) <= 3,
      f"{beat_error_ms(r, 124, 0.25, 60):+.1f} ms")

print("== a wandering tempo is reported, not hidden ==")
# 124 → 124.15 over three minutes: any one constant grid is ~25 ms off somewhere.
r = detect(track(124, tempo_b=124.15))
check("drift is reported when one grid cannot fit", r.drift_ms is not None and r.drift_ms >= 12, f"{r.drift_ms}")

print("== nothing to fit ==")
for label, y in (("silence", np.zeros(SR * 20, dtype=np.float32)), ("too short", np.zeros(SR, dtype=np.float32))):
    try:
        detect(y)
        check(f"{label} raises ValueError", False, "returned a grid")
    except ValueError:
        check(f"{label} raises ValueError", True)

print()
print("FAILED" if failed else "OK")
raise SystemExit(1 if failed else 0)
