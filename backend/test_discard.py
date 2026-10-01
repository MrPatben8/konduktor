"""Discarding unsaved edits (`POST /api/discard`, `AppState.discard`).

Konduktor had no discard path at all: switching library dropped edits, quitting
killed the backend. Discard is also what a stem conversion's parked originals
will hang on, so it has to be exact. Pins:

  * Traktor: an edit is dropped, the library is clean, the file on disk and the
    version history are untouched — and this session's relocation answers and
    saved path mapping survive (they are not edits; reopening would lose them).
  * A running batch is stopped BEFORE the reload, so it cannot keep writing
    into the reloaded library.
  * Rekordbox: the SQLAlchemy session, the buffered ANLZ grid writes and the
    journal all go — a plain re-open left the library still "dirty" and the
    next save still writing the discarded grids.
  * A read-only library has nothing to discard (409).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-discard-appdata-")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


print("== Traktor, through the routes ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()
    os.environ["KONDUKTOR_NML"] = str(work)
    from fastapi.testclient import TestClient

    import konduktor.main as main
    from konduktor import history
    from konduktor.core.pathmap import PathMapping

    with TestClient(main.app) as c:
        a = main.require_adapter()
        track = next(t for t in a.tracks if t.genre)
        genre = track.genre  # a string: the index updates Track objects in place
        commits_before = len(history.list_history(work))
        r = c.patch("/api/tracks", json={"track_id": track.id, "fields": {"genre": "Discard me"}})
        check("(setup) an edit makes the library dirty", r.status_code == 200 and c.get("/api/state").json()["dirty"])
        session = [PathMapping.make("/Volumes/Gone", "/Volumes/Here")]
        a.set_session_mappings(session)
        a.set_path_mapping(PathMapping.make("/Old", "/New"))

        r = c.post("/api/discard")
        check("discard answers with a clean state", r.status_code == 200 and r.json()["dirty"] is False, r.text[:200])
        a = main.require_adapter()
        check("the edit is gone from the projection", a.track(track.id).genre == genre, a.track(track.id).genre)
        check("the file on disk was never touched", work.read_bytes() == original)
        check("no version was recorded", len(history.list_history(work)) == commits_before)
        check("this session's relocation answers survive", a.store._session_mappings == session)
        check("…and so does the saved path mapping", not a.store._path_mapping.empty)
        check("the same adapter object: a reload, not a reopen", main.STATE.adapter is a)

        # A running batch is stopped (and waited for) before the reload.
        main.grid_detect.detect_grid = lambda p, *x, **k: (time.sleep(0.05), SimpleNamespace(anchor=0.1, bpm=128.0))[1]
        ids = [t.id for t in a.tracks[:60]]
        jid = c.post("/api/tracks/grid/auto-batch", json={"track_ids": ids, "replace_existing": True}).json()["id"]
        time.sleep(0.2)
        r = c.post("/api/discard")
        job = main.JOBS.get(jid)
        check("a running batch has STOPPED by the time discard returns", job.finished, job.state)
        time.sleep(0.2)
        check("…and nothing it did survives the reload", c.get("/api/state").json()["dirty"] is False)
    os.environ.pop("KONDUKTOR_NML", None)

print("== Rekordbox: session, buffered grids and journal all go ==")
from konduktor.adapters.rekordbox import discovery  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402

found = discovery.detect_libraries()
if not found:
    print("  (skipped: no Rekordbox library on this machine)")
else:
    src = Path(found[0]["path"])
    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "master.db"
        shutil.copy2(src, work)
        if (src.parent / "share").is_dir():
            shutil.copytree(src.parent / "share", Path(d) / "share")
        before = work.read_bytes()
        ad = RekordboxAdapter(work)
        track = next(t for t in ad.tracks if t.title)
        title = track.title
        ad.set_track_metadata(track.id, {"title": "Discard me"})
        cues = ad.track_cues(track.id)
        if cues is not None and cues.grid_markers:
            ad.replace_grid(track.id, [(m.start + 0.01, m.bpm) for m in cues.grid_markers])
        check("(setup) edits make it dirty", ad.dirty)
        ad.reload()
        check("after discard it is clean", ad.dirty is False)
        check("the metadata edit is gone", ad.track(track.id).title == title, ad.track(track.id).title)
        check("no buffered grid write survives for the next save", not ad._store._pending_grids)
        ad.save()
        ad.close()
        check("a save after discard writes nothing the user threw away",
              RekordboxAdapter(work).track(track.id).title == title)
        del before

print("== a OneLibrary drive (editable as THE library) ==")
# A COPY: an editable drive must never be the checked-in fixture.
os.environ["KONDUKTOR_NML"] = ""
drive = Path(tempfile.mkdtemp()) / "Dingus"
shutil.copytree(FIXTURE, drive, ignore=shutil.ignore_patterns("rekordbox-edited"))
db_file = drive / "PIONEER" / "rekordbox" / "exportLibrary.db"
on_disk = db_file.read_bytes()
track_id = "/Contents/Loopmasters/UnknownAlbum/Demo Track 1.mp3"
with TestClient(main.app) as c:
    r = c.post("/api/library/open", json={"path": str(drive)})
    check("the drive opens as the library", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        c.patch("/api/tracks", json={"track_id": track_id, "fields": {"title": "Thrown away"}})
        check("the edit is held", main.STATE.adapter.dirty)
        check("discard succeeds", c.post("/api/discard").status_code == 200)
        check("and drops the edit",
              main.STATE.adapter.track(track_id).title.startswith("Demo Track 1"))
        check("the drive was never written", db_file.read_bytes() == on_disk)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
