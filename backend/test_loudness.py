"""Loudness measurement (`core/loudness.py`) on GENERATED signals.

Integrated loudness, LRA and true peak are checked against FFmpeg's own
`ebur128` filter, run through the PyAV wheel the app already ships — a
reference meter nothing has to be installed for. True peak is also checked
against the exact answer on an inter-sample peak, where FFmpeg's cheaper
interpolator overshoots. RMS has no reference meter here, so its definition is
pinned on signals whose answer is known: a full-scale sine reads 0 dB (AES17),
a square +3 dB, and a long quiet intro moves it not at all (it is the loudest
fifth of the 3 s blocks).
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import av
import numpy as np
import soundfile as sf

from konduktor.core import loudness

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def ebur128(pcm: np.ndarray, sr: int) -> dict:
    """FFmpeg's measurement of `pcm` (channels, n): the last frame's metadata."""
    from av.filter import Graph

    pcm = np.ascontiguousarray(np.atleast_2d(pcm).astype(np.float32))
    layout = "mono" if pcm.shape[0] == 1 else "stereo"
    graph = Graph()
    src = graph.add_abuffer(format="fltp", sample_rate=sr, layout=layout)
    meter = graph.add("ebur128", "peak=true:metadata=1")
    sink = graph.add("abuffersink")
    src.link_to(meter)
    meter.link_to(sink)
    graph.configure()
    last = None

    def drain():
        nonlocal last
        while True:
            try:
                last = sink.pull()
            except (av.BlockingIOError, av.EOFError):
                return

    pts = 0
    for start in range(0, pcm.shape[1], 4096):
        block = pcm[:, start:start + 4096]
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(block), format="fltp", layout=layout)
        frame.sample_rate, frame.pts = sr, pts
        pts += block.shape[1]
        src.push(frame)
        drain()
    try:
        src.push(None)
    except Exception:  # noqa: BLE001 — some builds raise at EOF; the frames are out
        pass
    drain()
    m = last.metadata
    return {"lufs": float(m["lavfi.r128.I"]), "lra": float(m["lavfi.r128.LRA"]),
            "true_peak": 20 * math.log10(float(m["lavfi.r128.true_peak"]))}


rng = np.random.default_rng(7)


def tone(freq, seconds, sr, amp=0.5, phase=0.0):
    t = np.arange(int(sr * seconds)) / sr
    return amp * np.sin(2 * np.pi * freq * t + phase)


def signals(sr):
    t = np.arange(sr * 30) / sr
    loud_quiet = np.where((t % 10) < 5, 1.0, 0.05)
    kick = np.exp(-(t % 0.5) * 20) * np.sin(2 * np.pi * 55 * t)
    return {
        "1 kHz sine": np.vstack([tone(1000, 30, sr, 0.1)] * 2),
        "noise": 0.2 * rng.standard_normal((2, t.size)),
        "loud/quiet sections": np.vstack([loud_quiet * tone(220, 30, sr, 0.3), loud_quiet * tone(330, 30, sr, 0.3)]),
        "kick + noise": np.vstack([0.9 * kick + 0.05 * rng.standard_normal(t.size)] * 2),
        "mono noise": (0.2 * rng.standard_normal(t.size))[None],
    }


print("== against FFmpeg's ebur128 ==")
for sr in (44100, 48000):
    for name, x in signals(sr).items():
        ours = loudness.measure(x.astype(np.float32), sr)
        ref = ebur128(x, sr)
        check(f"{sr} {name}: integrated within 0.1 LU ({ours.lufs:.2f} vs {ref['lufs']:.2f})",
              abs(ours.lufs - ref["lufs"]) <= 0.1)
        check(f"{sr} {name}: LRA within 0.3 LU ({ours.lra:.2f} vs {ref['lra']:.2f})",
              abs(ours.lra - ref["lra"]) <= 0.3)
        check(f"{sr} {name}: true peak within 0.2 dB ({ours.true_peak:.2f} vs {ref['true_peak']:.2f})",
              abs(ours.true_peak - ref["true_peak"]) <= 0.2)

print("== the standard's anchors ==")
sr = 48000
r = loudness.measure(np.vstack([tone(1000, 20, sr, 10 ** (-23 / 20))] * 2), sr)
check("a stereo 1 kHz sine at -23 dBFS is -23.0 LUFS", abs(r.lufs + 23.0) <= 0.1, f"{r.lufs:.2f}")
# A sine at a quarter of the rate, 45 degrees off the samples: every sample is
# 0.707 of the peak, which only oversampling can see. Exact: -6.02 dBTP.
r = loudness.measure(np.vstack([tone(sr / 4, 20, sr, 0.5, math.pi / 4)] * 2), sr)
check("an inter-sample peak is found (exact -6.02 dBTP)", abs(r.true_peak + 6.02) <= 0.2, f"{r.true_peak:.2f}")
check("…well above the sample peak (-9.03 dB)", r.true_peak > -8.0)
check("PLR is true peak minus integrated", abs(r.plr - (r.true_peak - r.lufs)) < 1e-9)
steps = signals(sr)["loud/quiet sections"]
r = loudness.measure(steps, sr)
check("alternating loud and quiet sections have a wide LRA", r.lra > 5, f"{r.lra:.2f}")

print("== RMS: AES17, loudest fifth of 3 s blocks ==")
r = loudness.measure(np.vstack([tone(997, 30, sr, 1.0)] * 2), sr)
check("a full-scale sine reads 0.0 dB", abs(r.rms) <= 0.05, f"{r.rms:.3f}")
square = np.sign(tone(997, 30, sr, 1.0))
r = loudness.measure(np.vstack([square] * 2), sr)
check("a full-scale square reads +3.01 dB", abs(r.rms - 3.01) <= 0.05, f"{r.rms:.3f}")
body = np.vstack([tone(440, 60, sr, 0.5)] * 2)
intro = np.vstack([tone(440, 120, sr, 0.02)] * 2)
plain = loudness.measure(body, sr)
long_intro = loudness.measure(np.concatenate([intro, body], axis=1), sr)
check("a long quiet intro leaves RMS where it was", abs(plain.rms - long_intro.rms) <= 0.05,
      f"{plain.rms:.2f} vs {long_intro.rms:.2f}")
check("…and RMS is the loud part's: a -6 dBFS sine reads -6 dB", abs(plain.rms + 6.02) <= 0.05, f"{plain.rms:.2f}")

print("== nothing to measure ==")
check("silence is None", loudness.measure(np.zeros((2, sr * 10), dtype=np.float32), sr) is None)
check("under 3 s is None", loudness.measure(np.vstack([tone(440, 2.5, sr)] * 2), sr) is None)
check("a mono (n,) array is accepted", loudness.measure(tone(440, 10, sr), sr) is not None)

print("== streaming: chunked feeding equals one pass ==")
x = signals(44100)["kick + noise"].astype(np.float32)
whole = loudness.measure(x, 44100)
meter = loudness._Meter(44100, 2)
for start in range(0, x.shape[1], 1153):           # odd, frame-like chunks
    meter.feed(x[:, start:start + 1153])
chunked = meter.result()
check("identical to the last digit that matters",
      all(abs(getattr(whole, k) - getattr(chunked, k)) < 1e-6 for k in ("lufs", "rms", "true_peak", "lra")),
      f"{whole} vs {chunked}")

print("== measure_file: the file's own rate and channels ==")
with tempfile.TemporaryDirectory() as d:
    for sr_, suffix in ((44100, "flac"), (48000, "wav")):
        x = np.vstack([tone(1000, 10, sr_, 0.1), tone(1000, 10, sr_, 0.1)]).T
        path = Path(d) / f"t.{suffix}"
        sf.write(path, x, sr_)
        r = loudness.measure_file(path)
        check(f"a {sr_} Hz {suffix} reads as its samples do", r is not None and abs(r.lufs + 20.0) <= 0.1,
              str(r))
    mono = Path(d) / "mono.flac"
    sf.write(mono, tone(1000, 10, 44100, 0.1), 44100)
    r = loudness.measure_file(mono)
    check("a mono file is measured as ONE channel (3 dB under its stereo twin)",
          r is not None and abs(r.lufs + 23.0) <= 0.1, str(r))
    # A stem file is its four stems SUMMED (what a stem deck plays, and what
    # Traktor measures), not its mix stream — commercial mixes are mastered
    # hotter than their stems. Here each stem is an eighth of the mix, so the
    # sum is the mix at half amplitude: -6.02 dB.
    from konduktor.core import stem_file

    src = Path(d) / "src.flac"
    sf.write(src, np.vstack([tone(1000, 10, 44100, 0.5)] * 2).T, 44100)
    stem = Path(d) / "s.stem.m4a"
    stem_file.build(src, stem, lambda pcm: [pcm * 0.125] * 4)
    mix = loudness._measure_stream(stem)
    summed = loudness.measure_file(stem)
    check("a stem file measures its stem SUM (6.02 dB under its mix stream)",
          summed is not None and mix is not None and abs((mix.lufs - summed.lufs) - 6.02) <= 0.1,
          f"mix {mix and mix.lufs:.2f}, sum {summed and summed.lufs:.2f}")
    try:
        loudness.measure_file(Path(d) / "missing.flac")
        check("a missing file raises", False)
    except Exception:  # noqa: BLE001
        check("a missing file raises", True)

print()
if failed:
    print("FAILED")
    raise SystemExit(1)
print("All loudness checks passed.")
