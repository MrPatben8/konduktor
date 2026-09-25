"""Batch grid analysis — the route and its job, on a temp copy of the collection.

Detection itself is `test_grid_detect.py`'s job; this pins the WIRING, and the
decisions only the batch makes: locked grids are skipped always, existing grids
only when asked, a failing track is reported and skipped rather than fatal, a
cancel keeps what is done, and nothing reaches disk before Save.
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

        check("nothing was written to disk (edits stay in memory until Save)",
              work.read_bytes() == REAL.read_bytes())

print()
print("FAILED" if failed else "RESULT: ALL PASSED")
raise SystemExit(1 if failed else 0)
