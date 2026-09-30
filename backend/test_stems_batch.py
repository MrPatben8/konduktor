"""The Convert to Stems batch end to end, through the routes, with a FAKE engine.

Against temp copies of the real collection whose playlisted tracks are pointed
at generated MP3s. What would fail silently or destroy something:

  * skips are reported with reasons (missing, already a stem file, target
    exists, two sources onto one target) and never converted;
  * a Replace batch leaves the SAVED collection untouched, parks the originals,
    swaps every entry at the end — prep unmoved in decoded time — and blocks
    the operations that would save or drop the swap;
  * Save deletes the parked originals and retargets export sets; Discard and
    opening another library put them back;
  * Cancel deletes what the run made and nothing else; one bad track fails
    alone;
  * destination + add mirrors the tree, never touches the originals, fills a
    new playlist;
  * after a CRASH, the next open restores (or, if the save got through,
    finishes) from what the saved collection says — and keeps the finished
    stem files, which the next batch reuses without separating again.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-stemsbatch-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import exports  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.adapters.traktor.store import TraktorStore  # noqa: E402
from konduktor.core import stem_file as sf  # noqa: E402
from konduktor.stems import engine_manager as em  # noqa: E402
from konduktor.stems.pending import parked_name, partial_name  # noqa: E402
from stem_test_support import install_fake_engine, make_mp3  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


ROOT = Path(tempfile.mkdtemp(prefix="konduktor-stemsbatch-"))
em._DEFAULT = install_fake_engine(ROOT / "engine")


def library(name: str, files: list[str]) -> tuple[Path, list[str], Path]:
    """A temp collection whose first len(files) playlisted tracks point at
    `files` in a music folder (each created as an MP3 unless it is 'missing'),
    each with a hotcue at 1.000 s and a grid at 0.500 s, saved."""
    d = ROOT / name
    music = d / "Music"
    music.mkdir(parents=True)
    work = d / "collection.nml"
    shutil.copy2(REAL, work)
    st = TraktorStore(work)
    in_pl = {k for n in st._iter_nodes(st._root()) if n.playlist for k in st._entry_keys_of(n.playlist)}
    entries = [e for e in st._nml.collection.entry if e.location and st._key_of(e) in in_pl and e.stems is None]
    ids = []
    for f, e in zip(files, entries):
        path = music / f
        if f.endswith(".mp3") and not f.startswith("missing"):
            make_mp3(path, seed=len(ids) + 1)
        old = st._key_of(e)
        e.location.volume, e.location.dir, e.location.file = os_path_to_location(path)
        new = st._key_of(e)
        for n in st._iter_nodes(st._root()):
            for pe in (n.playlist.entry if n.playlist else None) or []:
                if pe.primarykey and pe.primarykey.key == old:
                    pe.primarykey.key = new
        st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
        e.cue_v2 = []
        if path.exists():
            st.set_hotcue(new, 1, 1.0, 0, name="Drop")
            st.replace_grid(new, [(0.5, 120.0)])
        ids.append(new)
    st.save()
    return work, ids, music


def open_lib(c, work):
    r = c.post("/api/library/open", json={"path": str(work)})
    assert r.status_code == 200, r.text


def wait(c, jid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def convert(c, ids, **opts):
    r = c.post("/api/tracks/stems/convert", json={"track_ids": ids, "mode": "replace", **opts})
    assert r.status_code == 200, r.text
    return wait(c, r.json()["id"])


with TestClient(main.app) as c:
    print("== preview: skips, with reasons ==")
    work, ids, music = library("preview", ["ok.mp3", "missing.mp3", "have.mp3", "dup.mp3", "dup.m4a", "stemsrc.mp3"])
    sf.build(make_mp3(ROOT / "tmp-src.mp3"), music / "already.stem.m4a", lambda p: [p * g for g in (0.5, 0.25, -0.2, 0.1)])
    (music / "have.stem.m4a").write_bytes(b"a stem the user already owns")
    make_mp3(music / "dup.m4a")  # (MP3 bytes under an .m4a name: only its target name matters here)
    st = TraktorStore(work)
    e = st.model_entry(ids[5])
    e.location.file = "already.stem.m4a"
    st._entry_by_key = {st._key_of(x): x for x in st._nml.collection.entry if x.location}
    ids[5] = st._key_of(e)
    st.save()
    open_lib(c, work)
    pv = c.post("/api/tracks/stems/preview", json={"track_ids": ids, "mode": "replace"}).json()
    reasons = [s["reason"] for s in pv["skipped"]]
    check("the convertible tracks are planned (ok.mp3, dup.mp3)",
          sorted(Path(x["target"]).name for x in pv["convert"]) == ["dup.stem.m4a", "ok.stem.m4a"], str(pv["convert"]))
    check("a missing file is skipped, saying so", any("missing" in r for r in reasons), str(reasons))
    check("a file that is already a stem file is skipped", any("already a stem file" in r for r in reasons))
    check("an existing target is skipped, NEVER overwritten", any("already exists" in r for r in reasons)
          and (music / "have.stem.m4a").read_bytes() == b"a stem the user already owns")
    check("two sources onto one target (dup.mp3 + dup.m4a): the second is skipped",
          any("same file" in r for r in reasons), str(reasons))
    check("no disk-space block for a few small files", pv["blocked"] is None, str(pv["blocked"]))

    print("== Replace: convert, park, swap at the end; the saved file untouched ==")
    work, ids, music = library("replace", ["a.mp3", "b.mp3"])
    open_lib(c, work)
    a = main.require_adapter()
    before = {t: a.track_cues(t) for t in ids}
    disk = work.read_bytes()
    lib_id = main.STATE.library_id
    es = exports.create(lib_id, name="Gig", targets=["traktor"], destination=str(ROOT / "stick"))
    exports.add(lib_id, es.id, track_ids=[ids[0]])
    j = convert(c, ids)
    res = j["result"]
    check("the job finishes, both converted", j["state"] == "done" and len(res["converted"]) == 2, str(j)[:400])
    new = [res["renamed"][t] for t in ids]
    check("stem files sit beside the originals", all((music / n).is_file() for n in ("a.stem.m4a", "b.stem.m4a")))
    check("the originals are PARKED, not gone",
          all(parked_name(music / n).is_file() and not (music / n).exists() for n in ("a.mp3", "b.mp3")))
    check("no unfinished files are left", not list(music.glob("*.konduktor-partial")))
    check("the saved collection is untouched until Save", work.read_bytes() == disk)
    state = c.get("/api/state").json()
    check("the library is dirty and reports the pending conversions",
          state["dirty"] and state["pending_stems"]["tracks"] == 2 and state["pending_stems"]["parked"] == 2, str(state))
    a = main.require_adapter()
    check("the entries are stems now", all(a.track(n).media_kind == "stem" for n in new))
    check("prep reads back at the same decoded seconds",
          all([round(x.start, 6) for x in a.track_cues(n).cues] == [round(x.start, 6) for x in before[o].cues]
              for o, n in zip(ids, new)))
    for route, body in (("/api/reload", None), ("/api/library/remap-paths", {"from": "/x", "to": "/y"}),
                        ("/api/history/deadbeef/restore", None)):
        r = c.post(route, json=body) if body else c.post(route)
        check(f"{route} is refused while conversions await Save (409)", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    r = c.post("/api/save")
    check("Save succeeds", r.status_code == 200, r.text[:200])
    check("…and deletes the parked originals", not any(music.glob(".*.konduktor-parked")))
    check("…keeps the stem files", all((music / n).is_file() for n in ("a.stem.m4a", "b.stem.m4a")))
    check("…and nothing is pending any more", c.get("/api/state").json()["pending_stems"] is None)
    check("the saved collection names the stem files as STEM entries", all(n in TraktorStore(work).stem_keys() for n in new))
    check("the export set follows the converted track to its new id", exports.get(lib_id, es.id).track_ids == [new[0]],
          str(exports.get(lib_id, es.id).track_ids))

    print("== Discard puts everything back ==")
    work, ids, music = library("discard", ["a.mp3", "b.mp3"])
    open_lib(c, work)
    convert(c, ids)
    r = c.post("/api/discard")
    check("discard succeeds", r.status_code == 200 and r.json()["dirty"] is False, r.text[:200])
    check("the originals are back under their own names", all((music / n).is_file() for n in ("a.mp3", "b.mp3")))
    check("the stem files are gone, and nothing parked is left",
          not list(music.glob("*.stem.m4a")) and not list(music.glob(".*.konduktor-parked")))
    check("the entries name the MP3s again", all(main.require_adapter().track(t) is not None for t in ids))

    print("== opening another library also puts them back ==")
    convert(c, ids)
    other, _o, _m = library("other", [])
    open_lib(c, other)
    check("the originals are back when switching library", all((music / n).is_file() for n in ("a.mp3", "b.mp3"))
          and not list(music.glob("*.stem.m4a")))

    print("== Cancel deletes what the run made, nothing else ==")
    work, ids, music = library("cancel", ["a.mp3", "b.mp3", "c.mp3"])
    open_lib(c, work)
    os.environ["FAKE_DELAY"] = "0.15"
    jid = c.post("/api/tracks/stems/convert", json={"track_ids": ids, "mode": "replace"}).json()["id"]
    end = time.time() + 30
    while time.time() < end and "separating" not in c.get(f"/api/jobs/{jid}").json()["message"]:
        time.sleep(0.05)
    time.sleep(1.8)  # into the second track
    c.post(f"/api/jobs/{jid}/cancel")
    j = wait(c, jid)
    os.environ.pop("FAKE_DELAY")
    check("the job is cancelled", j["state"] == "cancelled" and j["result"]["cancelled"], str(j)[:300])
    check("no stem file, no unfinished file is left",
          not list(music.glob("*.stem.m4a*")) and not list(music.glob("*.konduktor-partial")), str(list(music.iterdir())))
    check("the originals were never touched", all((music / n).is_file() for n in ("a.mp3", "b.mp3", "c.mp3")))
    check("nothing is dirty", c.get("/api/state").json()["dirty"] is False)

    print("== one bad track fails alone ==")
    work, ids, music = library("fail", ["a.mp3", "bad.mp3"])
    (music / "bad.mp3").write_bytes(b"not audio at all")
    open_lib(c, work)
    j = convert(c, ids)
    res = j["result"]
    check("the good track converts", ids[0] in res["renamed"], str(res)[:300])
    check("the bad one is reported with a reason and keeps its original",
          len(res["failed"]) == 1 and res["failed"][0]["reason"] and (music / "bad.mp3").is_file(), str(res["failed"]))
    c.post("/api/discard")

    print("== destination + add: a new playlist, originals untouched ==")
    work, ids, music = library("dest", ["x.mp3", "sub/y.mp3"])
    open_lib(c, work)
    dest = ROOT / "dest-out"
    j = convert(c, ids, mode="destination", destination=str(dest), collection="add", new_playlist="Stems")
    res = j["result"]
    check("both added", len(res["added"]) == 2 and not res["renamed"], str(res)[:300])
    check("the source tree is mirrored under the destination",
          (dest / "x.stem.m4a").is_file() and (dest / "sub" / "y.stem.m4a").is_file(), str(list(dest.rglob("*"))))
    check("the originals are untouched, not parked", (music / "x.mp3").is_file() and not list(music.rglob("*.konduktor-parked")))
    a = main.require_adapter()
    pl = next(n for n in a.store._iter_nodes(a.store._root()) if n.playlist is not None and n.name == "Stems")
    check("the new entries fill the new playlist", a.playlist_entries(pl.playlist.uuid) == [res["added"][t] for t in ids])
    check("the original entries are still plain tracks", all(a.track(t).media_kind != "stem" for t in ids))
    c.post("/api/discard")

    print("== after a crash: restore from the saved file, keep the work, reuse it ==")
    work, ids, music = library("crash", ["a.mp3", "b.mp3"])
    open_lib(c, work)
    convert(c, ids)
    main.STATE.pending = None  # the process died: nothing ran the discard
    open_lib(c, work)
    rec = c.get("/api/state").json()["stem_recovery"]
    check("the next open restored the originals", all((music / n).is_file() for n in ("a.mp3", "b.mp3")) and rec["restored"] == 2, str(rec))
    check("…and kept the finished stem files aside (not as finished files)",
          all(partial_name(music / n).is_file() and not (music / n).exists() for n in ("a.stem.m4a", "b.stem.m4a"))
          and rec["kept"] == 2, str(list(music.iterdir())))
    count = ROOT / "count.txt"
    os.environ["FAKE_COUNT"] = str(count)
    pv = c.post("/api/tracks/stems/preview", json={"track_ids": ids, "mode": "replace"}).json()
    check("a new batch plans to REUSE them", all(x["reuse"] for x in pv["convert"]), str(pv))
    j = convert(c, ids)
    check("…and converts without separating again", j["state"] == "done" and len(j["result"]["converted"]) == 2
          and not count.exists(), f"{j['result']} engine calls: {count.read_text() if count.exists() else 0}")
    os.environ.pop("FAKE_COUNT")
    c.post("/api/discard")

    print("== after a crash between the save and the clean-up ==")
    work, ids, music = library("crash2", ["a.mp3"])
    open_lib(c, work)
    convert(c, ids)
    main.require_adapter().save()  # the library write happened…
    main.STATE.pending = None  # …then the process died before deleting the parked original
    open_lib(c, work)
    rec = c.get("/api/state").json()["stem_recovery"]
    check("the next open sees the swap was committed and finishes it",
          rec["committed"] == 1 and not list(music.glob(".*.konduktor-parked")) and (music / "a.stem.m4a").is_file(), str(rec))

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
