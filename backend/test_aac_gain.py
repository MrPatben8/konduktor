"""AAC-LC's lossless gain edit (`core/aac_gain.py`) on GENERATED files, plus the
user's real ones when present (kits 4 and 5, Kit Bulk's commercial stems).

What would fail silently: a mis-walked frame writing a "global_gain" that is
really spectral data (the walk must end in each frame's last byte, and the
decode of EVERY stream must be the source x 2^(n/4)); a byte changed that is
not a global_gain; a stem file's streams levelled unevenly; HE-AAC (whose SBR
part this edit does not scale) or a non-AAC MP4 treated as AAC-LC.
"""
from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import av  # noqa: E402
import numpy as np  # noqa: E402

from konduktor.core import aac_gain, level_edit, pyav  # noqa: E402
from konduktor.core import stem_file as sf  # noqa: E402
from stem_test_support import make_mp3  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def signal(sr, ch, seconds=5, seed=1):
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(seed)
    beat = np.sin(2 * np.pi * 2 * t) > 0.6
    x = [0.3 * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.3 * np.sin(2 * np.pi * (440 + 30 * c) * t)
                + 0.2 * rng.standard_normal(t.size)) for c in range(ch)]
    if ch == 2:
        x[1] = 0.8 * x[0] + 0.2 * x[1]  # correlated: invites M/S and intensity stereo
    return np.vstack(x).astype(np.float32)


def encode(path: Path, codec: str, sr: int, ch: int, bit_rate: int, options=None, profile=None) -> Path | None:
    layout = "stereo" if ch == 2 else "mono"
    x = signal(sr, ch)
    try:
        with av.open(str(path), "w", format="ipod") as c:
            s = c.add_stream(codec, rate=sr, layout=layout)
            s.bit_rate = bit_rate
            if options:
                s.options = options
            if profile:
                s.codec_context.profile = profile
            for o in range(0, x.shape[1], 1024):
                f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + 1024]), format="fltp", layout=layout)
                f.sample_rate, f.pts = sr, o
                for p in s.encode(f):
                    c.mux(p)
            for p in s.encode(None):
                c.mux(p)
    except Exception:  # noqa: BLE001 — an encoder this machine lacks (aac_at off macOS)
        path.unlink(missing_ok=True)
        return None
    return path


def decode(path: Path, index: int) -> np.ndarray:
    with pyav.open_input(path) as c:
        s = c.streams.audio[index]
        rs = av.AudioResampler(format="fltp", layout="stereo" if s.codec_context.channels > 1 else "mono",
                               rate=int(s.codec_context.sample_rate))
        out = [o.to_ndarray() for fr in c.decode(s) for o in rs.resample(fr)]
        out += [o.to_ndarray() for o in rs.resample(None)]
    return np.concatenate(out, axis=1).astype(np.float64)


def exact(src: Path, dst: Path, steps: int, streams: int) -> tuple[bool, str]:
    worst = 0.0
    for i in range(streams):
        a, b = decode(src, i), decode(dst, i)
        if a.shape != b.shape:
            return False, f"stream {i}: {a.shape} vs {b.shape}"
        worst = max(worst, float(np.abs(b - a * 2 ** (steps / 4)).max() / max(np.abs(a).max(), 1e-12)))
    return worst < 1e-5, f"{worst:.1e}"


def only_global_gains(src: Path, dst: Path, scanned) -> bool:
    a = np.frombuffer(src.read_bytes(), np.uint8)
    b = np.frombuffer(dst.read_bytes(), np.uint8)
    allowed = set()
    for gpos in scanned.positions:
        allowed.update((gpos >> 3).tolist())
        allowed.update(((gpos >> 3) + 1).tolist())
    return a.size == b.size and set(np.nonzero(a != b)[0].tolist()) <= allowed


ROOT = Path(tempfile.mkdtemp(prefix="konduktor-aacgain-"))
CASES = [
    ("FFmpeg stereo 44.1 kHz", "aac", 44100, 2, 192000, None),
    ("FFmpeg mono", "aac", 44100, 1, 96000, None),
    ("FFmpeg 48 kHz", "aac", 48000, 2, 160000, None),
    ("FFmpeg 22.05 kHz", "aac", 22050, 2, 64000, None),
    ("FFmpeg 48 kb/s, PNS + intensity + M/S", "aac", 44100, 2, 48000, {"aac_pns": "1", "aac_is": "1", "aac_ms": "1"}),
    ("FFmpeg with TNS", "aac", 44100, 2, 128000, {"aac_tns": "1"}),
    ("Apple (aac_at) 128 kb/s", "aac_at", 44100, 2, 128000, None),
    ("Apple (aac_at) mono 64 kb/s", "aac_at", 44100, 1, 64000, None),
]

print("== generated AAC-LC: the decode is the source x 2^(n/4), only global_gain bytes change ==")
for label, codec, sr, ch, br, opts in CASES:
    src = encode(ROOT / f"{len(list(ROOT.iterdir()))}.m4a", codec, sr, ch, br, opts)
    if src is None:
        print(f"  [SKIP] {label}: no {codec} encoder here")
        continue
    scanned = aac_gain.scan(src)
    check(f"{label}: every frame walked", len(scanned.positions[0]) == len(scanned.tracks[0].sizes) * ch,
          f"{len(scanned.positions[0])} for {len(scanned.tracks[0].sizes)} frames")
    for steps in (-2, 3):
        dst = ROOT / f"{src.stem}{steps:+d}.m4a"
        aac_gain.apply(src, dst, steps, scanned)
        ok, detail = exact(src, dst, steps, 1)
        check(f"{label} {steps:+d}: exact", ok, detail)
        check(f"{label} {steps:+d}: same size, only global_gain bytes differ", only_global_gains(src, dst, scanned))

print("== a stem file: all five streams by the same steps ==")
source = make_mp3(ROOT / "stem-source.mp3", seconds=5)
stem = ROOT / "track.stem.m4a"
sf.build(source, stem, lambda pcm: [pcm * g for g in (0.5, 0.25, -0.2, 0.1)])
scanned = aac_gain.scan(stem)
check("five AAC streams found", len(scanned.tracks) == 5, str(len(scanned.tracks)))
out = ROOT / "track-2.stem.m4a"
aac_gain.apply(stem, out, -2, scanned)
ok, detail = exact(stem, out, -2, 5)
check("every stream decodes at exactly -2 steps", ok, detail)
check("…the stem box and every other byte untouched", only_global_gains(stem, out, scanned)
      and sf.is_stem_file(out))
back = ROOT / "track-back.stem.m4a"
aac_gain.apply(out, back, 2)
check("the opposite steps give the original back, byte for byte", back.read_bytes() == stem.read_bytes())

print("== through level_edit: the generic path ==")
dst = ROOT / "via-level-edit.stem.m4a"
level_edit.apply(stem, dst, -2 * aac_gain.STEP_DB)
try:
    level_edit.verify(stem, dst, -2 * aac_gain.STEP_DB)
    ok = True
except level_edit.LevelEditError as ex:
    ok = str(ex)
check("level_edit applies and verifies an .m4a (all five streams)", ok is True, str(ok))
# One stream levelled unlike the others must fail verification.
part = ROOT / "part.stem.m4a"
one = aac_gain.Scan(scanned.tracks, [p if i == 2 else p[:0] for i, p in enumerate(scanned.positions)],
                    scanned.min_step, scanned.max_step)
aac_gain.apply(dst, part, 2, one)  # stream 2 back up: now it differs from the rest
try:
    level_edit.verify(stem, part, -2 * aac_gain.STEP_DB)
    caught = False
except level_edit.LevelEditError:
    caught = True
check("…and a stem file with one stream at another level fails verification", caught)
try:
    level_edit.apply(stem, ROOT / "half.stem.m4a", -2.0)
    refused = False
except level_edit.LevelEditError:
    refused = True
check("a gain that is not a whole number of steps is refused", refused and not (ROOT / "half.stem.m4a").exists())

print("== refusals leave nothing ==")
he = encode(ROOT / "he.m4a", "aac_at", 44100, 2, 64000, profile="HE-AAC")
if he is None:
    print("  [SKIP] no HE-AAC encoder here")
else:
    try:
        aac_gain.scan(he)
        check("HE-AAC is refused (its SBR part is not scaled by global_gain)", False)
    except aac_gain.AacGainError:
        check("HE-AAC is refused (its SBR part is not scaled by global_gain)", True)
alac = encode(ROOT / "alac.m4a", "alac", 44100, 2, 0)
if alac is not None:
    try:
        aac_gain.scan(alac)
        check("ALAC in an .m4a is refused", False)
    except aac_gain.AacGainError:
        check("ALAC in an .m4a is refused", True)
try:
    aac_gain.apply(stem, ROOT / "far.m4a", scanned.max_step + 1, scanned)
    refused = False
except aac_gain.AacGainError:
    refused = True
check("steps that would push a scalefactor out of range are refused, leaving no file",
      refused and not (ROOT / "far.m4a").exists())
try:
    aac_gain.apply(stem, out, -1)
    refused = False
except aac_gain.AacGainError:
    refused = True
check("an existing target is never overwritten", refused)

print("== real files on this machine (skipped when absent) ==")
kits = Path.home() / "Music" / "Konduktor Stem Test"
real = [kits / "Kit 4" / "K4-8 aac ffmpeg.m4a", kits / "Kit 4" / "K4-9 aac apple.m4a",
        *sorted((kits / "Kit 5").glob("*.stem.m4a"))[:2], *sorted((kits / "Kit Bulk").glob("*.stem.m4a"))[:3]]
for f in real:
    if not f.is_file():
        continue
    s = aac_gain.scan(f)
    dst = ROOT / f"real-{f.name}"
    aac_gain.apply(f, dst, -2, s)
    ok, detail = exact(f, dst, -2, len(s.tracks))
    check(f"{f.name}: every stream exact at -2 steps", ok, detail)
    dst.unlink()

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
