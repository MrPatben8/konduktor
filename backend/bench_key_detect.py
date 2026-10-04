"""Benchmark key detection (`core/key_detect.py`).

NOT part of `run_tests.sh`: it needs real audio, and the answer is a score to
compare rather than a pass/fail.

    python bench_key_detect.py --giantsteps DATA/gs     # hand-annotated EDM
    python bench_key_detect.py ~/Music                  # files with a key TAG

GiantSteps (604 Beatport previews, keys corrected by hand) is ground truth
and the number to quote; the model never trained on it (see
`train_key_model.py`). A folder's key tags were written by some other
analyser — usually Traktor, sometimes Mixed In Key or a store — so that score
is AGREEMENT with it, not accuracy. Copies of one song (a stem file beside its
source) count once, by title.

Errors are classed the MIREX way, because they are not equally wrong for a
DJ: a FIFTH (one step round the Camelot wheel) or the RELATIVE key (same
number, other letter) still mixes harmonically; PARALLEL (same tonic, other
mode) and OTHER do not. MIREX's weighted score is 1 / 0.5 / 0.3 / 0.2 / 0.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

warnings.filterwarnings("ignore")

from konduktor.core import key_detect, musical_key  # noqa: E402

WEIGHT = {"exact": 1.0, "fifth": 0.5, "relative": 0.3, "parallel": 0.2, "other": 0.0}
AUDIO = {".mp3", ".m4a", ".mp4", ".flac", ".aif", ".aiff", ".wav", ".ogg"}


def relation(det: tuple[int, str], ref: tuple[int, str]) -> str:
    """How a detected (wheel, mode) relates to the reference."""
    (dw, dm), (rw, rm) = det, ref
    if (dw, dm) == (rw, rm):
        return "exact"
    if dw == rw:
        return "relative"
    if dm == rm and (dw - rw) % 12 in (1, 11):
        return "fifth"
    if dm != rm:
        major, minor = (dw, rw) if dm == "major" else (rw, dw)
        if (major - minor) % 12 == 3:   # C major 8B vs C minor 5A
            return "parallel"
    return "other"


def parse_any(text: str) -> tuple[int, str] | None:
    """Open Key, Camelot or a named key ("Abm", "F# minor", "Gmin")."""
    w, m = musical_key.parse(text)
    return (w, m) if w else None


def _tag_key(path: Path) -> tuple[str, str] | None:
    import mutagen

    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if not f or not f.tags:
        return None
    t = f.tags

    def has(name: str) -> bool:
        try:
            return name in t
        except ValueError:  # a Vorbis comment refuses an MP4-style atom name
            return False

    key = title = None
    for name in ("TKEY", "----:com.apple.iTunes:initialkey", "initialkey", "INITIALKEY"):
        if has(name):
            v = t[name]
            v = v.text[0] if hasattr(v, "text") else v[0]
            key = v.decode() if isinstance(v, bytes) else str(v)
            break
    for name in ("TIT2", "\xa9nam", "title", "TITLE"):
        if has(name):
            v = t[name]
            title = str(v.text[0] if hasattr(v, "text") else v[0])
            break
    return (key, title or path.stem) if key else None


def folder_refs(roots: list[Path]) -> list[tuple[Path, tuple[int, str], str]]:
    seen, out = set(), []
    for root in roots:
        for dp, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                p = Path(dp) / name
                if p.suffix.lower() not in AUDIO or name.startswith("."):
                    continue
                tagged = _tag_key(p)
                if not tagged or not (ref := parse_any(tagged[0])):
                    continue
                title = re.sub(r"\.stem$", "", tagged[1].strip().lower())
                if title not in seen:
                    seen.add(title)
                    out.append((p, ref, tagged[1]))
    return out


def giantsteps_refs(gs: Path) -> list[tuple[Path, tuple[int, str], str]]:
    out = []
    for f in sorted((gs / "annotations/key").glob("*.key")):
        audio = gs / "audio" / f"{f.stem}.mp3"
        ref = parse_any(f.read_text())     # "C minor"
        if audio.exists() and ref:
            out.append((audio, ref, f.stem))
    return out


def _detect(path: Path):
    warnings.filterwarnings("ignore")
    try:
        r = key_detect.detect_key(str(path))
    except Exception as ex:  # an undecodable file is reported, not fatal
        return None, str(ex)
    return (r.wheel, r.mode, r.name, r.confidence) if r else None, None


def run(refs, label: str, verbose: bool) -> None:
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(_detect, [p for p, _, _ in refs], chunksize=2))
    rels, misses = [], []
    for (path, ref, title), (det, err) in zip(refs, results):
        if det is None:
            print(f"  could not analyse {title}: {err or 'silent / too short'}")
            continue
        rel = relation(det[:2], ref)
        rels.append(rel)
        if rel != "exact":
            misses.append((title, ref, det, rel))
    n = len(rels)
    if not n:
        print(f"{label}: nothing to score")
        return
    c = Counter(rels)
    print(f"\n{label}: {n} tracks")
    for k in WEIGHT:
        print(f"  {k:9s} {c[k]:4d}  {100 * c[k] / n:5.1f}%")
    print(f"  MIREX weighted score {sum(WEIGHT[r] for r in rels) / n:.3f}")
    print(f"  harmonically mixable (exact + fifth + relative) {100 * (c['exact'] + c['fifth'] + c['relative']) / n:.1f}%")
    if verbose:
        for title, ref, det, rel in misses:
            print(f"    {rel:9s} ref {ref[0]}{'A' if ref[1] == 'minor' else 'B'}  "
                  f"got {det[0]}{'A' if det[1] == 'minor' else 'B'} ({det[2]}, p={det[3]:.2f})  {title[:60]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", type=Path, help="folders of audio files carrying a key tag")
    ap.add_argument("--giantsteps", type=Path, help="a downloaded giantsteps-key-dataset clone")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every miss")
    a = ap.parse_args()
    if not a.folders and not a.giantsteps:
        ap.error("give --giantsteps and/or folders")
    if a.giantsteps:
        run(giantsteps_refs(a.giantsteps), "GiantSteps key (hand-annotated)", a.verbose)
    if a.folders:
        run(folder_refs(a.folders), "Key tags (agreement with another analyser)", a.verbose)
    sys.exit(0)
