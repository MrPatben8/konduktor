"""The context menu's Remove ▸ commands, through their routes, on a temp copy.

Removing from the COLLECTION is new — the protocol's only destroying verb for
tracks — and its bytes are pinned by invariant L in `test_save_fidelity.py`.
This pins what the routes decide: which tracks each command skips (locked
grids, memory cues), that "clear hotcues" empties the whole bank but leaves the
grid, that a removed track leaves the projection AND every playlist while its
audio file is untouched, that the capability gates the route, and that a
running batch analysis blocks all three.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REAL = Path(__file__).resolve().parents[1] / "collection.nml"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  {'✓' if cond else '✗'} {label}" + ("" if cond else f"   [{detail}]"))
    if not cond:
        failed = True


print("== bulk remove ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    os.environ["KONDUKTOR_NML"] = str(work)
    from fastapi.testclient import TestClient

    import konduktor.main as main

    with TestClient(main.app, raise_server_exceptions=False) as c:
        a = main.require_adapter()
        gridded = [t.id for t in a.tracks if t.grid_marker_count > 0 and not t.grid_locked][:3]
        locked = gridded[2]
        a.set_grid_lock(locked, True)
        bare = next(t.id for t in a.tracks if t.grid_marker_count == 0)

        # ---- grids ----------------------------------------------------------
        r = c.post("/api/tracks/grid/clear", json={"track_ids": [*gridded, bare]})
        check("grid clear answers 200", r.status_code == 200, r.text[:200])
        check("…counts cleared / locked / empty",
              r.json() == {"cleared": 2, "locked": 1, "empty": 1}, r.text)
        check("the grids are gone", all(not a.track_cues(t).grid_markers for t in gridded[:2]))
        check("a locked grid is kept", bool(a.track_cues(locked).grid_markers))

        # ---- hotcues --------------------------------------------------------
        with_cues = [t.id for t in a.tracks
                     if (cu := a.track_cues(t.id)) and len(cu.grid_markers) > 0
                     and any(q.slot is not None and q.grid_marker is not None for q in cu.cues)
                     and sum(q.slot is not None for q in cu.cues) >= 2][:2]
        before = {t: a.track_cues(t) for t in with_cues}
        n_cues = sum(sum(q.slot is not None for q in cu.cues) for cu in before.values())
        r = c.post("/api/tracks/cue/clear", json={"track_ids": with_cues})
        check("hotcue clear answers 200", r.status_code == 200, r.text[:200])
        check("…counts tracks and cues", r.json() == {"tracks": 2, "cues": n_cues}, f"{r.text} vs {n_cues}")
        check("the whole bank is empty, incl. the grid's beat-1 cue",
              all(not any(q.slot is not None for q in a.track_cues(t).cues) for t in with_cues))
        # Positions and tempos, not the whole marker: its `companion` slot is
        # SUPPOSED to become None, since the paired cue was just deleted.
        def grid(cu):
            return [(m.start, m.bpm) for m in cu.grid_markers]
        check("…and the grid itself is untouched (only its pairing ends)",
              all(grid(a.track_cues(t)) == grid(before[t]) for t in with_cues)
              and all(m.companion is None for t in with_cues for m in a.track_cues(t).grid_markers))

        # ---- from collection ------------------------------------------------
        caps = c.get("/api/capabilities").json()
        check("Traktor advertises removable tracks", caps["tracks"]["removable"] is True)
        tree = c.get("/api/playlists").json()

        def lists(nodes):
            for n in nodes:
                yield n
                yield from lists(n["children"])

        pl = next(n for n in lists(tree) if n.get("count") and n.get("can_add_tracks"))
        in_pl = c.get(f"/api/playlists/{pl['id']}/tracks").json()
        in_pl = in_pl["tracks"] if isinstance(in_pl, dict) else in_pl
        victim = in_pl[0]["id"]
        audio = a.audio_path(victim)
        existed = audio is not None and audio.exists()
        n_before = len(a.tracks)

        r = c.post("/api/tracks/remove", json={"track_ids": [victim, victim, "nope"]})
        check("remove answers 200", r.status_code == 200, r.text[:200])
        check("…and removes exactly one (duplicates / unknowns ignored)", r.json() == {"removed": 1}, r.text)
        check("the track leaves the projection", a.track(victim) is None and len(a.tracks) == n_before - 1)
        after = c.get(f"/api/playlists/{pl['id']}/tracks").json()
        after = after["tracks"] if isinstance(after, dict) else after
        check("…and the playlist", all(t["id"] != victim for t in after) and len(after) == len(in_pl) - 1)
        check("the audio file is untouched", not existed or audio.exists())
        check("the edit is unsaved (Save lights up)", c.get("/api/state").json().get("dirty") is True)

        # ---- gating ----------------------------------------------------------
        orig_caps = a.capabilities
        a.capabilities = lambda: orig_caps().model_copy(update={
            "tracks": orig_caps().tracks.model_copy(update={"removable": False})})
        r = c.post("/api/tracks/remove", json={"track_ids": [gridded[0]]})
        check("remove is refused without the capability", r.status_code == 422, r.text[:200])
        a.capabilities = orig_caps

        main.grid_detect.detect_grid = lambda p, *a_, **k: (time.sleep(0.05), SimpleNamespace(anchor=0.1, bpm=128.0))[1]
        a.audio_path = lambda tid: Path(__file__)
        jid = c.post("/api/tracks/grid/auto-batch",
                     json={"track_ids": [t.id for t in a.tracks][:40], "replace_existing": True}).json()["id"]
        for route in ("/api/tracks/grid/clear", "/api/tracks/cue/clear", "/api/tracks/remove"):
            r = c.post(route, json={"track_ids": [gridded[0]]})
            check(f"{route} is refused while a batch runs", r.status_code == 409, r.text[:200])
        c.post(f"/api/jobs/{jid}/cancel")

        check("nothing was written to disk (edits stay in memory until Save)",
              work.read_bytes() == REAL.read_bytes())

print()
print("FAILED" if failed else "RESULT: ALL PASSED")
raise SystemExit(1 if failed else 0)
