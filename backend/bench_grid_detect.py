"""Benchmark beatgrid detection against grids a DJ platform already analysed.

NOT part of `run_tests.sh`: it needs real audio, and the answer is a score to
compare rather than a pass/fail. Every open is read-only — nothing is saved.

    python bench_grid_detect.py                      # the local Rekordbox library
    python bench_grid_detect.py path/to/collection.nml path/to/master.db
    python bench_grid_detect.py --legacy             # score the old librosa beat_track

The reference is each track's own grid, and only single-marker grids are used:
a multi-marker grid is either a real tempo change (which a constant-tempo
detector cannot match by design) or a hand edit, and neither is ground truth
for "what is this track's tempo".

Per track it reports:
  - BPM error, and whether it is an octave error (half/double tempo);
  - PHASE error at 30 s in and 30 s before the end — the distance between the
    detected grid's nearest beat and the reference's, in ms. Measuring at two
    ends is what separates "anchor wrong" (both off by the same amount) from
    "BPM wrong" (error grows along the track), which one number would conflate.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("KONDUKTOR_DATA_DIR", tempfile.mkdtemp(prefix="konduktor-bench-"))

import konduktor.adapters  # noqa: E402,F401 — registers the drivers
from konduktor.core import registry  # noqa: E402

BPM_OK = 0.02        # a grid this close drifts < 1/50 beat per minute...
PHASE_OK_MS = 10.0   # roughly where a DJ starts to hear flamming


def _phase_error_ms(t: float, ref: tuple[float, float], det: tuple[float, float]) -> float:
    """Signed distance (ms) from the reference's nearest beat at ``t`` to the
    detected grid's nearest beat, wrapped to half the reference beat."""
    r0, rb = ref
    d0, db = det
    rp, dp = 60.0 / rb, 60.0 / db
    ref_beat = r0 + round((t - r0) / rp) * rp
    det_beat = d0 + round((ref_beat - d0) / dp) * dp
    e = det_beat - ref_beat
    e = (e + rp / 2) % rp - rp / 2
    return e * 1000.0


def _octave(det: float, ref: float) -> str:
    for k, name in ((2.0, "x2"), (0.5, "/2"), (1.5, "x1.5"), (2 / 3, "/1.5")):
        if abs(det - ref * k) < 0.5:
            return name
    return ""


def legacy_detect(audio_path: str):
    """The detector `core/grid_detect.py` replaced (librosa ``beat_track``), kept
    here so the two can still be compared on the same references."""
    import librosa
    import numpy as np
    from konduktor.core.grid_detect import GridResult

    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    return GridResult(float(np.ravel(tempo)[0]), float(librosa.frames_to_time(beats, sr=sr)[0]), len(y) / sr)


def references(paths: list[Path]) -> list[tuple[str, Path, float, float]]:
    out = []
    for p in paths:
        a = registry.open_library(p)
        try:
            for t in a.tracks:
                cues = a.track_cues(t.id)
                if cues is None or len(cues.grid_markers) != 1:
                    continue
                audio = a.audio_path(t.id)
                if audio is None or not audio.exists():
                    continue
                m = cues.grid_markers[0]
                out.append((f"{t.artist or ''} - {t.title or audio.stem}".strip(" -"), audio, m.start, m.bpm))
        finally:
            close = getattr(a, "close", None)
            if close:
                close()
    # One row per audio file: the same file in two libraries would count twice.
    seen, uniq = set(), []
    for r in out:
        if r[1] not in seen:
            seen.add(r[1])
            uniq.append(r)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("libraries", nargs="*", type=Path)
    ap.add_argument("--legacy", action="store_true", help="score the old librosa beat_track detector")
    args = ap.parse_args()

    libs = args.libraries or [Path.home() / "Library/Pioneer/rekordbox/master.db"]
    refs = references(libs)
    if not refs:
        print("No single-marker reference grids with reachable audio.")
        return 1

    if args.legacy:
        detect = legacy_detect
    else:
        from konduktor.core.grid_detect import detect_grid as detect

    rows = []
    for name, audio, r0, rb in refs:
        t0 = time.perf_counter()
        try:
            res = detect(str(audio))
        except Exception as ex:  # a failure is a data point, not a crash
            print(f"  FAIL  {name[:48]:48}  {ex}")
            rows.append(None)
            continue
        dt = time.perf_counter() - t0
        bpm, first = res.bpm, res.anchor
        import soundfile as sf
        try:
            dur = sf.info(str(audio)).duration
        except Exception:
            dur = res.duration
        early = _phase_error_ms(min(30.0, dur / 3), (r0, rb), (first, bpm))
        late = _phase_error_ms(max(dur - 30.0, 2 * dur / 3), (r0, rb), (first, bpm))
        oct_ = _octave(bpm, rb) if abs(bpm - rb) >= 0.5 else ""
        ok = abs(bpm - rb) <= BPM_OK and abs(early) <= PHASE_OK_MS and abs(late) <= PHASE_OK_MS
        lossy = audio.suffix.lower() not in (".wav", ".aif", ".aiff", ".flac")
        rows.append((abs(bpm - rb), early, late, oct_, ok, dt, lossy))
        conf = f"{res.drift_ms:5.1f}" if res.drift_ms is not None else "    -"
        print(
            f"  {'ok  ' if ok else 'BAD '}  {name[:48]:48}  ref {rb:7.3f}  got {bpm:8.3f} {oct_:5}"
            f"  phase {early:+7.1f} / {late:+7.1f} ms  drift {conf} ms  {dt:4.1f}s"
        )

    good = [r for r in rows if r]
    n = len(rows)
    same_octave = [r for r in good if not r[3]]
    print()
    print(f"tracks                 {n}")
    print(f"all correct            {sum(r[4] for r in good)}/{n}")
    print(f"BPM within {BPM_OK}      {sum(r[0] <= BPM_OK for r in good)}/{n}")
    print(f"octave errors          {sum(bool(r[3]) for r in good)}/{n}")
    if same_octave:
        import statistics as st
        print(f"median |phase| early   {st.median(abs(r[1]) for r in same_octave):.1f} ms")
        print(f"median |phase| late    {st.median(abs(r[2]) for r in same_octave):.1f} ms")
        print(f"phase within {PHASE_OK_MS:.0f} ms     "
              f"{sum(abs(r[1]) <= PHASE_OK_MS and abs(r[2]) <= PHASE_OK_MS for r in same_octave)}/{n}")
    print(f"mean time per track    {sum(r[5] for r in good) / max(1, len(good)):.2f} s")

    # A platform's MP3/AAC time base can differ from the decoded audio's by a
    # constant (Rekordbox: ~+26 ms, the decoder delay it does not trim). That
    # is not detection error, and it would swamp it — so report the constant
    # per format, and how tightly the tracks sit around it.
    import statistics as st
    print()
    for label, want in (("lossless", False), ("lossy", True)):
        grp = [r for r in same_octave if r[6] == want and r[0] <= BPM_OK]
        if len(grp) < 2:
            continue
        offs = [(r[1] + r[2]) / 2 for r in grp]
        med = st.median(offs)
        spread = [abs(o - med) for o in offs]
        within = sum(abs(r[1] - med) <= PHASE_OK_MS and abs(r[2] - med) <= PHASE_OK_MS for r in grp)
        print(f"{label:9} n={len(grp):2}  constant offset {med:+6.1f} ms   "
              f"|deviation| median {st.median(spread):4.1f} max {max(spread):5.1f} ms   "
              f"within {PHASE_OK_MS:.0f} ms of it: {within}/{len(grp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
