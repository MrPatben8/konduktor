"""How Traktor's auto-gain relates to Konduktor's loudness measures.

Traktor stores its gain per ENTRY as `<LOUDNESS ANALYZED_DB>` (its own
measurement) and `PERCEIVED_DB` (the gain the deck applies; equal to
ANALYZED after Traktor's own analysis — kit G1, 2026-10-19). If Traktor
levels toward a fixed reference, `ANALYZED_DB = reference − loudness`, so
`ANALYZED_DB + LUFS` is that reference, and its spread is how far Konduktor's
measure is from Traktor's.

    python bench_traktor_gain.py FOLDER [--nml collection.nml]

FOLDER's audio must be in the collection and analysed by Traktor (Gain).
A stem file is measured as its four stems SUMMED, as Traktor measures it.

Result (2026-10-19, 80 MP3 + 40 commercial stem files, Traktor 4.5):
ANALYZED_DB = -9.2 - LUFS (free slope 0.99), sd 0.80 dB, |miss| p50 0.56 /
p90 1.35; RMS fits worse (sd 1.17); the miss follows nothing measured here
(LRA, PLR, true peak, bass weight: |r| < 0.2) — it is Traktor's own measure.
Fits LUFS and RMS (slope and fixed-slope), then asks whether what is left
over follows something about the music (LRA, PLR, bass weight, format, genre).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_NML = Path.home() / "Documents/Native Instruments/Traktor 4.5.0/collection.nml"


def _key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def traktor_values(nml: Path) -> dict[str, dict]:
    """Absolute path (normalised) -> Traktor's LOUDNESS + genre, for analysed entries."""
    out = {}
    text = nml.read_text(encoding="utf-8")
    for e in re.findall(r"<ENTRY .*?</ENTRY>", text, re.S):
        loc = re.search(r'<LOCATION DIR="([^"]*)" FILE="([^"]*)"', e)
        loud = re.search(r'<LOUDNESS PEAK_DB="([-\d.]+)" PERCEIVED_DB="([-\d.]+)" ANALYZED_DB="([-\d.]+)"', e)
        if not (loc and loud):
            continue
        path = html.unescape(loc.group(1)).replace("/:", "/") + html.unescape(loc.group(2))
        genre = re.search(r'<INFO [^>]*GENRE="([^"]*)"', e)
        out[_key(path)] = {
            "peak": float(loud.group(1)),
            "perceived": float(loud.group(2)),
            "analyzed": float(loud.group(3)),
            "genre": html.unescape(genre.group(1)) if genre else "",
        }
    return out


def _measure(path: str):
    """`loudness.measure_file` — which measures a stem file as its four stems
    summed, as Traktor does (found by this bench: against the mix stream the
    reference was -6.6, sd 1.18; against the sum -9.3, sd 0.68)."""
    from konduktor.core import loudness

    try:
        return path, loudness.measure_file(path)
    except Exception as e:  # an undecodable file is reported, not fatal
        return path, e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--nml", type=Path, default=DEFAULT_NML)
    args = ap.parse_args()

    tk = traktor_values(args.nml)
    files = sorted(p for p in args.folder.rglob("*") if p.is_file() and not p.name.startswith("."))
    held = [p for p in files if _key(str(p)) in tk]
    print(f"{len(files)} files, {len(held)} analysed by Traktor")
    for p in files:
        if _key(str(p)) not in tk:
            print(f"  not in the collection / not analysed: {p.name}")

    rows = []
    with ProcessPoolExecutor(4) as pool:  # a stem file holds 4 decoded streams
        for path, m in pool.map(_measure, [str(p) for p in held]):
            if m is None or isinstance(m, Exception):
                print(f"  not measured: {Path(path).name}: {m}")
                continue
            t = tk[_key(path)]
            rows.append((Path(path), m, t))

    name = np.array([r[0].name for r in rows])
    lufs = np.array([r[1].lufs for r in rows])
    rms = np.array([r[1].rms for r in rows])
    tp = np.array([r[1].true_peak for r in rows])
    lra = np.array([r[1].lra for r in rows])
    plr = np.array([r[1].plr for r in rows])
    an = np.array([r[2]["analyzed"] for r in rows])
    pc = np.array([r[2]["perceived"] for r in rows])
    pk = np.array([r[2]["peak"] for r in rows])
    fmt = np.array([r[0].suffix.lower() for r in rows])
    genre = np.array([r[2]["genre"] or "(none)" for r in rows])
    n = len(rows)

    print(f"\n{n} tracks; LUFS {lufs.min():.1f} .. {lufs.max():.1f}, "
          f"PERCEIVED == ANALYZED on {int(np.sum(np.isclose(pc, an)))}")

    print("\n== reference = ANALYZED_DB + measure (slope fixed at 1)")
    for label, v in (("LUFS", lufs), ("RMS", rms)):
        ref = an + v
        k, b = np.polyfit(-v, an, 1)
        free = an - (k * -v + b)
        q = np.percentile(np.abs(ref - np.median(ref)), [50, 90])
        print(f"  {label:4s}: mean {ref.mean():6.2f}  median {np.median(ref):6.2f}  sd {ref.std():.2f}  "
              f"|miss| p50 {q[0]:.2f} p90 {q[1]:.2f}   (free slope {k:.3f}, sd {free.std():.2f})")

    print("\n== PEAK_DB + true peak (Traktor's peak gain vs our true peak)")
    d = pk + tp
    print(f"  mean {d.mean():+.2f}  sd {d.std():.2f}  min {d.min():+.2f}  max {d.max():+.2f}")

    ref = an + lufs
    resid = ref - ref.mean()
    print("\n== does the LUFS miss follow the music? (correlation with the residual)")
    bass = rms - lufs  # unweighted vs K-weighted: higher = more low end
    for label, v in (("LRA", lra), ("PLR", plr), ("true peak", tp), ("bass weight (RMS-LUFS)", bass), ("LUFS", lufs)):
        r = np.corrcoef(v, resid)[0, 1]
        k = np.polyfit(v, resid, 1)[0]
        print(f"  {label:24s} r = {r:+.2f}   slope {k:+.3f} dB per unit")
    X = np.column_stack([bass, lra, np.ones(n)])
    coef, *_ = np.linalg.lstsq(X, resid, rcond=None)
    print(f"  bass + LRA together: sd {resid.std():.2f} -> {(resid - X @ coef).std():.2f}")

    print("\n== by format")
    for f in sorted(set(fmt)):
        s = fmt == f
        print(f"  {f:5s} n={s.sum():3d}  reference mean {ref[s].mean():6.2f}  sd {ref[s].std():.2f}")
    print("\n== by genre (n >= 3)")
    for g in sorted(set(genre), key=lambda g: -np.sum(genre == g)):
        s = genre == g
        if s.sum() >= 3:
            print(f"  {g[:24]:24s} n={s.sum():3d}  reference mean {ref[s].mean():6.2f}  sd {ref[s].std():.2f}")

    print("\n== largest misses (LUFS)")
    for i in np.argsort(-np.abs(resid))[:8]:
        print(f"  {resid[i]:+5.2f}  LUFS {lufs[i]:6.2f} LRA {lra[i]:4.1f} bass {bass[i]:+5.2f}  {genre[i][:14]:14s} {name[i][:50]}")


if __name__ == "__main__":
    main()
