"""Benchmark Auto Hotcues' structure events against Rekordbox's phrase analysis.

NOT part of `run_tests.sh`: it needs real audio and a Rekordbox library whose
tracks were analysed with phrase analysis on (the `PSSI` tag in each `.EXT`).
Read-only — nothing is saved.

    python bench_structure.py                 # the local Rekordbox library
    python bench_structure.py -v              # per-track events, detected vs reference

The reference is Rekordbox's own analysis, not ground truth, and it is only
comparable on "High"-mood tracks, whose labels map onto what a DJ cues:
Intro / Up (build) / Down (breakdown) / Chorus (drop) / Outro. "Mid" tracks are
labelled Verse / Chorus / Bridge and are reported separately for that reason.
Tracks whose detected BPM is a different octave from Rekordbox's are skipped:
their bars are not the same bars.

Events are compared in bars from each side's own first downbeat:
- drop_n      = start of the n-th Chorus run;
- breakdown_n = end of the n-th drop, when another drop follows;
- build_n     = the Up run right before drop_n (Rekordbox's Up usually spans the
                whole pre-drop groove, so this one is indicative at best);
- outro       = start of the Outro.
"""
from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("KONDUKTOR_DATA_DIR", tempfile.mkdtemp(prefix="konduktor-bench-"))

import konduktor.adapters  # noqa: E402,F401 — registers the drivers
from konduktor.core import grid_detect, registry, structure  # noqa: E402

DROP = {1: 5, 2: 9}     # PSSI kind of a Chorus, by mood (1 = High, 2 = Mid)
OUTRO = {1: 6, 2: 10}
UP = {1: 2}


def references(db: Path) -> list[dict]:
    from pyrekordbox.anlz import AnlzFile

    a = registry.open_library(db)
    out = []
    try:
        st = a._store
        for t in a.tracks:
            path = a.audio_path(t.id)
            rel = getattr(st.content(t.id), "AnalysisDataPath", None)
            if not path or not path.exists() or not rel:
                continue
            base = st.path.parent / "share" / str(rel).lstrip("/\\")
            ext = base.with_suffix(".EXT")
            if not (base.is_file() and ext.is_file()):
                continue
            rec = {"path": str(path), "title": t.title or path.stem}
            for tag in AnlzFile.parse_file(str(base)).tags:
                if tag.type == "PQTZ" and len(tag.content.entries):  # one-shots have none
                    e = tag.content.entries
                    rec["bpm"] = e[0].tempo / 100
                    rec["first_down"] = next(i for i, x in enumerate(e) if x.beat == 1)
            for tag in AnlzFile.parse_file(str(ext)).tags:
                if tag.type == "PSSI":
                    rec["mood"] = tag.content.mood
                    rec["phrases"] = [(x.beat, x.kind) for x in tag.content.entries]
            if "phrases" in rec and "bpm" in rec:
                out.append(rec)
    finally:
        a.close()
    return out


def truth(rec: dict) -> dict[str, int]:
    runs: list[tuple[int, int]] = []
    for beat, kind in rec["phrases"]:
        bar = round((beat - 1 - rec["first_down"]) / 4)
        if not runs or runs[-1][1] != kind:
            runs.append((bar, kind))
    m, ev = rec["mood"], {}
    drops = [i for i, (_, k) in enumerate(runs) if k == DROP.get(m)]
    for n, i in enumerate(drops, start=1):
        ev[f"drop_{n}"] = runs[i][0]
        if n < len(drops) and i + 1 < len(runs):
            ev[f"breakdown_{n}"] = runs[i + 1][0]
        if m in UP and i > 0 and runs[i - 1][1] == UP[m]:
            ev[f"build_{n}"] = runs[i - 1][0]
    outro = [b for b, k in runs if k == OUTRO.get(m)]
    if outro:
        ev["outro"] = outro[0]
    return ev


def main() -> int:
    verbose = "-v" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    db = Path(args[0]) if args else Path.home() / "Library/Pioneer/rekordbox/master.db"
    refs = references(db)
    if not refs:
        print("No tracks with Rekordbox phrase analysis and reachable audio.")
        return 1

    for mood, label in ((1, "High"), (2, "Mid")):
        stats: dict[str, list[int]] = {}
        for rec in (r for r in refs if r["mood"] == mood):
            g = grid_detect.detect_grid(rec["path"])
            if abs(g.bpm - rec["bpm"]) >= 1:
                continue  # a different octave: not the same bars
            st = structure.analyse(rec["path"], [(g.anchor, g.bpm)])
            det = {k: (v - st.bar0) // 4 for k, v in st.events.items()}
            tru = truth(rec)
            if verbose:
                print(f"  {rec['title'][:28]:28} ref {tru}\n  {'':28} got { {k: det[k] for k in tru if k in det} }")
            for e, bar in tru.items():
                key = e if e in ("drop_1", "drop_2", "breakdown_1", "build_1", "build_2", "outro") else e.split("_")[0] + "_n"
                s = stats.setdefault(key, [0, 0, 0])
                s[0] += 1
                if e in det:
                    s[1] += det[e] == bar
                    s[2] += abs(det[e] - bar) <= 4
        if not stats:
            continue
        print(f"== {label}-mood tracks")
        for k in sorted(stats):
            n, exact, near = stats[k]
            print(f"  {k:12} n={n:2}  exact bar {exact:2} ({exact / n:4.0%})  within 4 bars {near:2} ({near / n:4.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
