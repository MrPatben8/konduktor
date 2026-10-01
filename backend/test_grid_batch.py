"""Batch analysis — grid and Auto Hotcues jobs, on a temp copy of the collection.

Detection itself is `test_grid_detect.py`'s job; this pins the WIRING, and the
decisions only the batch makes: locked grids are skipped always, existing grids
only when asked, a failing track is reported and skipped rather than fatal, a
cancel keeps what is done, and nothing reaches disk before Save. Batch Auto
Hotcues adds: one template on every track, a grid analysed first where there is
none (and an existing one left alone), Replace applying to every track, and one
batch job at a time ACROSS both kinds.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REAL = Path(__file__).resolve().parents[1] / "collection.nml"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  {'✓' if cond else '✗'} {label}" + ("" if cond else f"   [{detail}]"))
    if not cond:
        failed = True


def wait(c, job_id, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


print("== batch grid analysis ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    os.environ["KONDUKTOR_NML"] = str(work)
    from fastapi.testclient import TestClient

    import konduktor.main as main

    detected: list[str] = []

    def fake_detect(path, *a, **k):
        detected.append(path)
        return SimpleNamespace(anchor=0.123, bpm=127.5)

    main.grid_detect.detect_grid = fake_detect

    with TestClient(main.app, raise_server_exceptions=False) as c:
        a = main.require_adapter()
        missing = next(t.id for t in a.tracks if t.grid_marker_count == 0)
        a.audio_path = lambda tid: None if tid == missing else Path(__file__)

        gridded = [t.id for t in a.tracks if t.grid_marker_count > 0 and not t.grid_locked][:2]
        bare = [t.id for t in a.tracks if t.grid_marker_count == 0 and t.id != missing][:2]
        locked = gridded[1]
        a.set_grid_lock(locked, True)
        ids = [gridded[0], locked, *bare, missing]

        # The deck's single-track Analyze shares the batch's helper.
        r = c.post("/api/tracks/grid/auto", json={"track_id": bare[1]})
        check("single-track Analyze still answers 200", r.status_code == 200, r.text[:200])
        r = c.post("/api/tracks/grid/auto", json={"track_id": missing})
        check("…and 400 on a missing file", r.status_code == 400, r.text[:200])
        a.delete_grid(bare[1])

        r = c.post("/api/tracks/grid/auto-batch", json={"track_ids": ids})
        check("the route starts a job", r.status_code == 200, r.text[:200])
        j = wait(c, r.json()["id"])
        res = j["result"]
        check("the job finishes", j["state"] == "done", str(j))
        check("progress reaches the total", j["done"] == j["total"] == len(ids), str(j))
        check("tracks without a grid are analysed", res["analysed"] == bare, str(res))
        check("an existing grid is skipped by default", res["existing"] == 1, str(res))
        check("a locked grid is skipped", res["locked"] == 1, str(res))
        check("a missing file is reported, not fatal",
              len(res["failed"]) == 1 and "not found" in res["failed"][0]["reason"], str(res))
        g = a.track_cues(bare[0]).grid_markers
        check("the analysed grid is written", len(g) == 1 and abs(g[0].bpm - 127.5) < 1e-6, str(g))

        r = c.post("/api/tracks/grid/auto-batch",
                   json={"track_ids": [gridded[0], locked], "replace_existing": True})
        res = wait(c, r.json()["id"])["result"]
        check("replace_existing re-analyses an existing grid", res["analysed"] == [gridded[0]], str(res))
        check("…but never a locked one", res["locked"] == 1, str(res))

        # Cancel: a slow detector gives the cancel time to land mid-run.
        main.grid_detect.detect_grid = lambda p, *a, **k: (time.sleep(0.05), fake_detect(p))[1]
        many = [t.id for t in a.tracks if not t.grid_locked][:40]
        r = c.post("/api/tracks/grid/auto-batch",
                   json={"track_ids": many, "replace_existing": True})
        jid = r.json()["id"]
        r2 = c.post("/api/tracks/grid/auto-batch", json={"track_ids": many})
        check("a second run while one is going is refused", r2.status_code == 409, r2.text[:200])
        time.sleep(0.2)
        c.post(f"/api/jobs/{jid}/cancel")
        j = wait(c, jid)
        check("a cancelled run says so", j["state"] == "cancelled", str(j))
        n = len(j["result"]["analysed"]) if j["result"] else -1
        check("…and still returns what it finished", 0 < n < len(many), str(n))

        # ---- batch Auto Hotcues --------------------------------------------
        print("== batch auto hotcues ==")
        main.grid_detect.detect_grid = fake_detect
        from konduktor.core import structure

        def fake_analyse(path, markers):
            bpm = markers[0][1]
            st = structure.Structure(beats=np.arange(400) * 60.0 / bpm, bar0=0,
                                     n_bars=100, duration=400 * 60.0 / bpm)
            st.events = {"drop_1": 129, "outro": 353}  # clear of any beat-0 grid cue
            return st

        main.structure.analyse = fake_analyse

        def free(tid, n):
            cu = a.track_cues(tid)
            used = {q.slot for q in cu.cues if q.slot is not None}
            return [k for k in range(8) if k not in used][:n]

        gridded = next(t.id for t in a.tracks if t.grid_marker_count > 0 and not t.grid_locked
                       and len(free(t.id, 2)) == 2 and t.id not in many)
        nogrid = next(t.id for t in a.tracks if t.grid_marker_count == 0
                      and t.id not in (missing, *bare, *many))
        before_grid = a.track_cues(gridded).grid_markers
        s0, s1 = free(gridded, 2)
        tmpl = [{"slot": s0, "event": "drop_1"}, {"slot": s1, "event": "outro"}]

        r = c.post("/api/tracks/cue/auto-batch",
                   json={"track_ids": [gridded, nogrid, missing], "slots": tmpl})
        check("the batch route starts a job", r.status_code == 200, r.text[:200])
        j = wait(c, r.json()["id"])
        res = j["result"]
        check("the job finishes", j["state"] == "done", str(j))
        check("both tracks got cues", res["tracks"] == [gridded, nogrid], str(res))
        check("a grid is created only where there was none", res["grids_created"] == 1, str(res))
        check("an existing grid is left untouched",
              a.track_cues(gridded).grid_markers == before_grid)
        check("the track without a grid now has one", len(a.track_cues(nogrid).grid_markers) == 1)
        names = {q.slot: q.name for q in a.track_cues(gridded).cues if q.slot is not None}
        check("the template's cues are placed", names.get(s0) == "Drop 1" and names.get(s1) == "Outro", str(names))
        check("placed cues are counted", res["cues_placed"] == 4, str(res))
        check("a missing file is reported, not fatal",
              len(res["failed"]) == 1 and "not found" in res["failed"][0]["reason"], str(res))

        # Occupied slots: kept, unless ticked — and then replaced on every track.
        # A fresh beat (16 before the drop), so the one-cue-per-beat rule cannot
        # be what decides it.
        tmpl2 = [{"slot": s0, "event": "drop_1", "offset_beats": -16}]
        res = wait(c, c.post("/api/tracks/cue/auto-batch",
                             json={"track_ids": [gridded], "slots": tmpl2}).json()["id"])["result"]
        check("an occupied slot is kept without Replace", res["cues_placed"] == 0, str(res))
        tmpl2[0]["overwrite"] = True
        res = wait(c, c.post("/api/tracks/cue/auto-batch",
                             json={"track_ids": [gridded], "slots": tmpl2}).json()["id"])["result"]
        names = {q.slot: q.name for q in a.track_cues(gridded).cues if q.slot is not None}
        check("…and replaced with it", names.get(s0) == "Drop 1 -16" and res["cues_placed"] == 1,
              f"{names} {res}")

        r = c.post("/api/tracks/cue/auto-batch",
                   json={"track_ids": [gridded], "slots": [{"slot": 99, "event": "outro"}]})
        check("a template the bank cannot hold is refused up front", r.status_code == 400, r.text[:200])
        r = c.post("/api/tracks/cue/auto-batch", json={"track_ids": [gridded], "slots": []})
        check("an empty template is refused", r.status_code == 400, r.text[:200])

        # One batch at a time ACROSS kinds.
        main.grid_detect.detect_grid = lambda p, *a, **k: (time.sleep(0.05), fake_detect(p))[1]
        jid = c.post("/api/tracks/grid/auto-batch",
                     json={"track_ids": many, "replace_existing": True}).json()["id"]
        r = c.post("/api/tracks/cue/auto-batch", json={"track_ids": [gridded], "slots": tmpl})
        check("hotcues are refused while a grid run is going", r.status_code == 409, r.text[:200])
        c.post(f"/api/jobs/{jid}/cancel")
        wait(c, jid)

        # Opening another library cancels a running batch.
        main.structure.analyse = lambda p, m: (time.sleep(0.05), fake_analyse(p, m))[1]
        gridded_many = [t.id for t in a.tracks if t.grid_marker_count > 0][:40]
        jid = c.post("/api/tracks/cue/auto-batch",
                     json={"track_ids": gridded_many, "slots": tmpl}).json()["id"]
        time.sleep(0.1)
        other = Path(d) / "other.nml"
        shutil.copy2(REAL, other)
        c.post("/api/library/open", json={"path": str(other)})
        check("opening another library has STOPPED the run by the time it returns",
              main.JOBS.get(jid).finished and main.JOBS.get(jid).state == "cancelled",
              main.JOBS.get(jid).state)
        check("opening another library cancels the run", wait(c, jid)["state"] == "cancelled")

        # Exclusive submit is checked and registered under one lock: a second
        # batch cannot start while one runs, however the requests interleave.
        from konduktor.jobs import JobBusy
        import threading as _th
        gate = _th.Event()
        first = main.JOBS.submit(main.GRID_JOB, lambda h: gate.wait(5), exclusive_with=main.BATCH_JOBS)
        try:
            main.JOBS.submit(main.CUE_JOB, lambda h: None, exclusive_with=main.BATCH_JOBS)
            check("a second batch kind is refused while one runs", False, "it started")
        except JobBusy:
            check("a second batch kind is refused while one runs", True)
        check("wait() times out on a running job", main.JOBS.wait(first.id, timeout=0.05) is False)
        gate.set()
        check("…and returns once it has finished", main.JOBS.wait(first.id, timeout=5) is True)

        # Within-item progress: what lets the overall bar move inside a long
        # track (99 % of the first of two = ~50 %), and it starts over per item.
        from konduktor.jobs import Job, JobHandle
        j = Job(id="x", kind="t")
        h = JobHandle(j)
        h.progress(done=0, total=2, fraction=0.99, status="separating 99 %")
        d = j.as_dict()
        check("a job reports its current item's fraction and status",
              d["fraction"] == 0.99 and d["status"] == "separating 99 %", str(d))
        h.progress(done=1)
        check("…both reset when the next item starts", j.fraction == 0.0 and j.status == "")
        h.progress(fraction=1.7)
        check("…and the fraction is clamped to 0..1", j.fraction == 1.0)
        from konduktor.schemas import JobStatus
        r = JobStatus(**j.as_dict()).model_dump()
        check("the job's response model carries fraction and status (not silently dropped)",
              r["fraction"] == 1.0 and "status" in r, str(r)[:200])

        check("nothing was written to disk (edits stay in memory until Save)",
              work.read_bytes() == REAL.read_bytes())

        # The status bar tells a byte count from a track count by `unit`; the
        # response model once dropped it on every job route.
        jid = main.JOBS.submit("unit-probe", lambda h: h.progress(done=1, total=2, unit="bytes")).id
        check("a job's unit reaches the client", wait(c, jid).get("unit") == "bytes")

print()
print("FAILED" if failed else "RESULT: ALL PASSED")
raise SystemExit(1 if failed else 0)
