"""Tests for Auto Hotcues: structure analysis, slot resolution and the route.

Three layers, because each failed silently before:
- `structure.analyse` on SYNTHETIC audio whose sections are known by
  construction (intro / breakdown / drop / …), so a wrong bar is a wrong answer
  rather than a disagreement with another analyser;
- `auto_hotcues.plan`, the pure part: offsets in beats (across a tempo change),
  and every outcome a slot can report;
- the ROUTE, end to end against a temp copy of the real collection. The previous
  implementation's route raised a 500 on every call for a week after a model
  change, and nothing noticed, because only its placement helper was tested.

Accuracy on real music is measured against Rekordbox's phrase analysis, which
needs local audio; see the Auto Hotcues section of CLAUDE.md.
"""
import os
import shutil
import tempfile
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-autocue-appdata-")

from konduktor.core import auto_hotcues as ah  # noqa: E402
from konduktor.core import structure  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
SR = 22050
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


# ---- synthetic track ----------------------------------------------------------
rng = np.random.default_rng(0)
BPM, FIRST = 128.0, 0.25
BEAT = 60.0 / BPM
BAR = 4 * BEAT


def _fade(x):
    n = min(len(x), int(0.02 * SR))
    x = x.copy()
    x[-n:] *= np.linspace(1, 0, n)
    return x


def _kick():
    t = np.arange(int(0.25 * SR)) / SR
    return _fade(np.sin(2 * np.pi * 52 * t) * np.exp(-t / 0.1) + np.r_[rng.standard_normal(40) * 0.4, np.zeros(len(t) - 40)])


def _hat():
    n = int(0.04 * SR)
    return _fade(np.diff(rng.standard_normal(n + 1)) * np.exp(-np.arange(n) / SR / 0.01) * 0.3)


# (name, bars, kick, bass, pad, hats)
LAYOUT = [
    ("intro", 16, True, False, False, True),
    ("breakdown", 16, False, False, True, False),
    ("drop", 32, True, True, True, True),
    ("breakdown", 16, False, False, True, False),
    ("drop", 32, True, True, True, True),
    ("outro", 16, True, False, False, True),
]


def synth():
    total = sum(b for _, b, *_ in LAYOUT) * BAR + FIRST + 1.0
    y = np.zeros(int(total * SR))
    t_all = np.arange(len(y)) / SR
    k, h = _kick(), _hat()
    bar = 0
    for _, bars, kick, bass, pad, hats in LAYOUT:
        t0 = FIRST + bar * BAR
        t1 = t0 + bars * BAR
        m = (t_all >= t0) & (t_all < t1)
        if bass:  # a loud sustained 55 Hz bass with some harmonics
            y[m] += 0.5 * np.sign(np.sin(2 * np.pi * 55 * t_all[m])) * 0.6
        if pad:  # a mid-range chord
            y[m] += 0.08 * sum(np.sin(2 * np.pi * f * t_all[m]) for f in (330, 415, 494))
        for b in range(bars * 4):
            tb = t0 + b * BEAT
            i = int(tb * SR)
            if kick:
                y[i : i + len(k)] += k[: len(y) - i]
            if hats:
                j = int((tb + BEAT / 2) * SR)
                y[j : j + len(h)] += h[: max(0, len(y) - j)]
        bar += bars
    return (0.3 * y).astype(np.float32)


print("== structure on a track whose sections are known ==")
y = synth()
st = structure.analyse("", [(FIRST, BPM)], y=y)
bar_of = {e: (b - st.bar0) // 4 for e, b in st.events.items()}
check("first drop at bar 32", bar_of.get("drop_1") == 32, str(bar_of))
check("first breakdown at bar 64 (where the first drop ends)", bar_of.get("breakdown_1") == 64, str(bar_of))
check("second drop at bar 80", bar_of.get("drop_2") == 80, str(bar_of))
check("the intro is NOT a drop, though it has a kick", bar_of.get("drop_1") != 0, str(bar_of))
check("intro ends at bar 16", bar_of.get("intro_end") == 16, str(bar_of))
check("no third drop is invented", "drop_3" not in bar_of, str(bar_of))
check("no breakdown after the LAST drop (that is the outro)", "breakdown_2" not in bar_of, str(bar_of))
check("outro starts at bar 112", bar_of.get("outro") == 112, str(bar_of))
check("first beat is the first kick", abs(st.beat_time(st.events["first_beat"]) - FIRST) < 0.01,
      f"{st.beat_time(st.events['first_beat'])}")
check("every section boundary is on a 4-bar line", all(s.start % 4 == 0 for s in st.sections),
      str([s.start for s in st.sections]))

print("== a flexible grid: beats follow each marker's tempo ==")
beats, bar0 = structure.grid_beats([(1.0, 120.0), (9.0, 60.0)], duration=20.0)
check("beats before the first marker are extrapolated back", bar0 == 2 and abs(beats[0] - 0.0) < 1e-9, f"{bar0} {beats[:3]}")
check("0.5 s apart under the first marker", abs(beats[bar0 + 1] - beats[bar0] - 0.5) < 1e-9)
check("1.0 s apart after the second", abs(beats[-1] - beats[-2] - 1.0) < 1e-9)


# ---- plan: the pure part ----------------------------------------------------------
def fake_structure(events, n_beats=400, bpm=120.0):
    s = structure.Structure(beats=np.arange(n_beats) * 60.0 / bpm, bar0=0, n_bars=n_beats // 4,
                            duration=n_beats * 60.0 / bpm)
    s.events = dict(events)
    return s


print("== plan: offsets and outcomes ==")
fs = fake_structure({"drop_1": 128, "outro": 384, "first_beat": 0})
R = ah.SlotRequest
E = ah.ExistingCue
out = {o.slot: o for o in ah.plan(fs, [
    R(0, "first_beat"),
    R(1, "drop_1", -16),
    R(2, "drop_3"),
    R(3, "first_beat", -4),
    R(4, "drop_1"),
    R(5, "drop_1", 0, overwrite=True),
    R(6, "drop_1", 0, overwrite=True),
], existing={4: E(True, 190.0), 5: E(True, 190.5), 6: E(False, 191.0)})}
check("placed on the event's beat", out[0].status == ah.PLACED and out[0].start == 0.0)
check("an offset counts BEATS: drop -16 is 8 s earlier at 120 BPM",
      out[1].status == ah.PLACED and abs(out[1].start - (128 - 16) * 0.5) < 1e-9, str(out[1]))
check("the offset is in the cue's name", out[1].name == "Drop 1 -16", str(out[1].name))
check("a missing event is reported, not placed", out[2].status == ah.NOT_FOUND)
check("an offset before the track start is out of range", out[3].status == ah.OUT_OF_RANGE)
check("an occupied slot is left alone by default", out[4].status == ah.OCCUPIED)
check("…and replaced when overwrite is asked for", out[5].status == ah.PLACED)
check("a protected cue is never replaced, even with overwrite", out[6].status == ah.PROTECTED)

print("== plan: two slots on the same beat get ONE cue, in the lower slot ==")
dup = fake_structure({"drop_1": 128, "breakdown_1": 112, "build_2": 112})
out = {o.slot: o for o in ah.plan(dup, [
    R(3, "breakdown_1"),
    R(4, "build_2"),                 # the same beat as slot 3, by a different event
    R(5, "drop_1", -16),             # …and again, by an offset (128 - 16 = 112)
    R(6, "drop_1", -15),             # one beat later: a different beat, placed
], existing={})}
check("the lower slot keeps the beat", out[3].status == ah.PLACED)
check("a higher slot on the same beat is skipped", out[4].status == ah.DUPLICATE and out[4].duplicate_of == 3, str(out[4]))
check("…whichever event or offset got it there", out[5].status == ah.DUPLICATE and out[5].duplicate_of == 3, str(out[5]))
check("one beat apart is not a duplicate", out[6].status == ah.PLACED)
rev = {o.slot: o.status for o in ah.plan(dup, [R(4, "build_2"), R(3, "breakdown_1")], existing={})}
check("lower slot wins whatever order the request lists them in",
      rev == {3: ah.PLACED, 4: ah.DUPLICATE}, str(rev))

print("== plan: cues already in the bank claim their beats too ==")
# beat k is at k * 0.5 s in fake_structure; breakdown_1 / build_2 are beat 112 = 56 s.
o = {x.slot: x for x in ah.plan(dup, [R(3, "breakdown_1")], existing={0: E(False, 56.0)})}
check("a new cue on the GRID cue's beat is skipped as its duplicate",
      o[3].status == ah.DUPLICATE and o[3].duplicate_of == 0, str(o[3]))
o = {x.slot: x for x in ah.plan(dup, [R(3, "breakdown_1")], existing={6: E(True, 56.03)})}
check("…and on a hand-placed cue a few ms off the grid (higher slot, still kept)",
      o[3].status == ah.DUPLICATE and o[3].duplicate_of == 6, str(o[3]))
o = {x.slot: x for x in ah.plan(dup, [R(3, "breakdown_1")], existing={6: E(True, 56.25)})}
check("a cue on the half-beat is a different beat", o[3].status == ah.PLACED, str(o[3]))
o = {x.slot: x.status for x in ah.plan(dup, [R(3, "breakdown_1"), R(4, "build_2")], existing={3: E(True, 10.0)})}
check("a kept cue elsewhere claims only ITS beat", o == {3: ah.OCCUPIED, 4: ah.PLACED}, str(o))

print("== plan: a replaced cue frees its beat only if its replacement is placed ==")
o = {x.slot: x.status for x in ah.plan(dup, [
    R(2, "drop_1", overwrite=True), R(5, "breakdown_1"),
], existing={2: E(True, 56.0)})}
check("replaced → its old beat is free for another slot", o == {2: ah.PLACED, 5: ah.PLACED}, str(o))
o = {x.slot: x for x in ah.plan(dup, [
    R(1, "drop_1"), R(2, "drop_1", overwrite=True), R(5, "breakdown_1"),
], existing={2: E(True, 56.0)})}
check("a replacement that is itself a duplicate is skipped…",
      o[2].status == ah.DUPLICATE and o[2].duplicate_of == 1, str(o[2]))
check("…so the old cue stays, and still claims its beat",
      o[5].status == ah.DUPLICATE and o[5].duplicate_of == 2, str(o[5]))

flex = structure.Structure(beats=np.r_[np.arange(0, 10, 0.5), np.arange(10, 30, 1.0)], bar0=0, n_bars=10, duration=30)
flex.events = {"drop_1": 24}  # beat 24 is at 14 s (20 beats at 0.5 s, then 4 at 1.0 s)
o = ah.plan(flex, [R(0, "drop_1", -8)], {})[0]
# 8 beats back is beat 16, at 8.0 s; 8 beats at the LOCAL tempo would be 6.0 s.
check("an offset across a tempo change counts beats, not seconds", abs(o.start - 8.0) < 1e-9, str(o.start))


# ---- the route --------------------------------------------------------------------
print("== the route, end to end ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    os.environ["KONDUKTOR_NML"] = str(work)
    from fastapi.testclient import TestClient

    import konduktor.main as main

    # The real collection's audio is not on every machine, so the route gets a
    # stand-in structure and an existing file: this pins the WIRING — request
    # validation, the plan, the adapter write and the response model.
    # Its events are put on beats chosen per track (below), clear of the cues
    # already there — otherwise the one-cue-per-beat rule, not the wiring, would
    # decide the outcome.
    ROUTE_EVENTS: dict = {}
    main.structure.analyse = lambda path, markers: fake_structure(ROUTE_EVENTS, bpm=markers[0][1])
    with TestClient(main.app, raise_server_exceptions=False) as c:
        a = main.require_adapter()
        a.audio_path = lambda track_id: Path(__file__)
        def free_slots(cu):
            return [s for s in range(8) if not any(q.slot == s for q in cu.cues)]
        tid = next(t.id for t in a.tracks
                   if (cu := a.track_cues(t.id)) and len(cu.grid_markers) == 1 and len(free_slots(cu)) >= 4
                   and any(q.grid_marker is not None and q.slot is not None for q in cu.cues))
        cu = a.track_cues(tid)
        step = 60.0 / cu.grid_markers[0].bpm
        taken_t = [q.start for q in cu.cues if q.slot is not None]
        clear = [k for k in range(8, 390) if all(abs(k * step - t) > step for t in taken_t)]
        drop = next(k for k in clear if k - 16 in clear and k >= 80)
        outro = next(k for k in clear if k > drop + 40)
        grid_cue = next(q for q in cu.cues if q.grid_marker is not None and q.slot is not None)
        ROUTE_EVENTS.update(drop_1=drop, outro=outro, first_beat=round(grid_cue.start / step))
        f0, f1, f2, f3 = free_slots(cu)[:4]
        r = c.post("/api/tracks/cue/auto", json={"track_id": tid, "slots": [
            {"slot": f0, "event": "first_beat"},
            {"slot": f1, "event": "drop_1", "offset_beats": -16},
            {"slot": f2, "event": "drop_3"},
            {"slot": f3, "event": "outro"},
        ]})
        check("the route answers 200", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            j = r.json()
            oc = {o["slot"]: o for o in j["outcomes"]}
            st_ = {k: o["status"] for k, o in oc.items()}
            check("outcomes per slot", st_ == {f0: "duplicate", f1: "placed", f2: "not_found", f3: "placed"}, str(st_))
            check("a new cue on the track's real GRID cue is skipped as its duplicate",
                  oc[f0]["duplicate_of"] == grid_cue.slot, str(oc[f0]))
            names = {q["slot"]: q["name"] for q in j["cues"]["cues"] if q["slot"] is not None}
            check("the cues are in the returned projection", names.get(f1) == "Drop 1 -16", str(names))
            r2 = c.post("/api/tracks/cue/auto", json={"track_id": tid, "slots": [{"slot": f1, "event": "drop_1"}]})
            check("running again leaves the now-occupied slot alone",
                  r2.status_code == 200 and r2.json()["outcomes"][0]["status"] == "occupied", r2.text[:200])
            f4 = free_slots(a.track_cues(tid))[0]
            r3 = c.post("/api/tracks/cue/auto", json={"track_id": tid, "slots": [{"slot": f4, "event": "outro"}]})
            o3 = r3.json()["outcomes"][0] if r3.status_code == 200 else {}
            check("…and a later run will not stack a cue on one it placed earlier",
                  o3.get("status") == "duplicate" and o3.get("duplicate_of") == f3, r3.text[:300])
        r = c.post("/api/tracks/cue/auto", json={"track_id": tid, "slots": [{"slot": f0, "event": "drop_1"}, {"slot": f0, "event": "outro"}]})
        check("a slot requested twice is refused", r.status_code == 400)
        r = c.post("/api/tracks/cue/auto", json={"track_id": tid, "slots": [{"slot": f0, "event": "the_best_bit"}]})
        check("an unknown event is refused", r.status_code == 422)
        check("nothing was written to disk (edits stay in memory until Save)",
              work.read_bytes() == REAL.read_bytes())

print()
print("FAILED" if failed else "RESULT: ALL PASSED")
raise SystemExit(1 if failed else 0)
