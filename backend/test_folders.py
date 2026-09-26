"""Browsing drives and folders, and adding loose files, through the routes.

Runs on a temp copy of the real collection, a temp app-data dir, and FLAC files
generated here — so the tags are known rather than borrowed from a real file.

Pins the things that would fail SILENTLY or dangerously:

  * a folder lists only the audio DIRECTLY in it, and never hidden or
    OS-housekeeping files (macOS writes a `._` twin beside every file on FAT);
  * the audio route streams only files a scan found — it is not a way to read
    any path on the disk;
  * a file the collection already points at is never added twice. On Traktor two
    entries for one path share a primary key, which is a corrupt collection —
    and that is exactly what "add in place" would do on a second click;
  * such a file still reaches the playlist or export it was headed for;
  * "copy" leaves the collection pointing at the COPY, and "reference" at the
    original, with nothing copied.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
_DATA = tempfile.TemporaryDirectory()
os.environ["KONDUKTOR_DATA_DIR"] = _DATA.name

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def flac(path: Path, **tags) -> Path:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((44100, 2), dtype="float32"), 44100, format="FLAC")
    audio = FLAC(path)
    for k, v in tags.items():
        audio[k] = str(v)
    audio.save()
    return path


# ---- the folder on disk ------------------------------------------------------
music = Path(tempfile.mkdtemp()) / "Loose"
one = flac(music / "one.flac", title="First Light", artist="Someone", genre="House", bpm=124)
two = flac(music / "two.flac")  # untagged: titled by its filename
flac(music / "Deeper" / "three.flac", title="Not Listed")
(music / "notes.txt").write_text("not audio")
shutil.copy2(one, music / "._one.flac")  # a macOS AppleDouble twin
flac(music / ".hidden.flac")
(music / ".secret-folder").mkdir()
(music / ".Trashes").mkdir()

work = Path(tempfile.mkdtemp()) / "collection.nml"
shutil.copy2(REAL, work)
os.environ["KONDUKTOR_NML"] = str(work)

from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import exports  # noqa: E402
from konduktor.app_state import STATE  # noqa: E402
from konduktor.core import places  # noqa: E402


def wait(c, job):
    while job["state"] == "running":
        time.sleep(0.05)
        job = c.get(f"/api/jobs/{job['id']}").json()
    return job


with TestClient(main.app, raise_server_exceptions=False) as c:
    a = main.require_adapter()
    before = len(a.tracks)

    print("== drives ==")
    r = c.get("/api/fs/drives")
    check("answers 200", r.status_code == 200, r.text[:200])
    drives = r.json()
    check("the boot disk is local, at the filesystem root",
          any(d["kind"] == "local" and d["path"] == "/" for d in drives), str(drives))
    check("each drive is listed once", len({d["path"] for d in drives}) == len(drives))

    print("== folder tree: visible folders only ==")
    r = c.get("/api/fs/folders", params={"path": str(music)})
    names = [e["name"] for e in r.json()]
    check("lists the subfolder", names == ["Deeper"], str(names))
    if sys.platform == "darwin":
        flagged = music / "Flagged"
        flagged.mkdir()
        os.chflags(flagged, 0x8000)  # UF_HIDDEN, as macOS sets on /usr and /bin
        names = [e["name"] for e in c.get("/api/fs/folders", params={"path": str(music)}).json()]
        check("a folder hidden by FLAG is left out, as Finder does", "Flagged" not in names, str(names))
    check("a missing folder is a 404",
          c.get("/api/fs/folders", params={"path": str(music / "nope")}).status_code == 404)

    print("== a folder's tracks: this folder only, audio only ==")
    r = c.get("/api/folder/tracks", params={"path": str(music)})
    check("answers 200", r.status_code == 200, r.text[:300])
    body = r.json()
    ids = [t["id"] for t in body["tracks"]]
    check("exactly the two visible audio files", ids == [str(one), str(two)], str(ids))
    first = body["tracks"][0]
    check("tags are read", (first["title"], first["artist"], first["genre"], first["bpm"])
          == ("First Light", "Someone", "House", 124.0), str(first))
    check("an untagged file is titled by its name", body["tracks"][1]["title"] == "two")
    check("the length comes from the audio", first["length"] == 1, str(first["length"]))
    check("nothing is in the collection yet", body["in_collection"] == [])
    caps = c.get("/api/folder/capabilities", params={"path": str(music)}).json()
    check("the view is read-only, and says why",
          caps["writable"] is False and caps["readonly_cause"] == "not_in_library")

    print("== the audio route streams only what a scan found ==")
    r = c.get("/api/folder/tracks/audio", params={"track_id": str(one)})
    check("a scanned file streams", r.status_code == 200 and len(r.content) > 0)
    check("an arbitrary file does not",
          c.get("/api/folder/tracks/audio", params={"track_id": "/etc/passwd"}).status_code == 404)
    check("nor does real audio in a folder nobody browsed",
          c.get("/api/folder/tracks/audio",
                params={"track_id": str(music / "Deeper" / "three.flac")}).status_code == 404)
    check("nor a hidden file beside a scanned one",
          c.get("/api/folder/tracks/audio",
                params={"track_id": str(music / ".hidden.flac")}).status_code == 404)
    check("cues are empty, not an error",
          c.get("/api/folder/tracks/cues", params={"track_id": str(one)}).json()["cues"] == [])

    print("== reference in place, into a playlist ==")
    playlist = a.create_playlist("Folder Test")
    req = {"track_ids": [str(one)], "mode": "reference", "playlist_id": playlist}
    p = c.post("/api/folder/add/preview", json=req).json()
    check("the preview counts one to add", p["importable"] == 1 and p["existing"] == [], str(p))
    check("and nothing to copy", p["total_bytes"] == 0)
    job = wait(c, c.post("/api/folder/add", json=req).json())
    check("the job finishes", job["state"] == "done", str(job))
    check("one entry was added", len(a.tracks) == before + 1)
    added = job["result"]["track_ids"][0]
    check("it points at the ORIGINAL file", a.audio_path(added) == one, str(a.audio_path(added)))
    check("its tags came with it", a.track(added).title == "First Light")
    check("it is in the playlist", a.playlist_entries(playlist) == [added])
    check("it was saved, not left pending", not a.dirty)
    body = c.get("/api/folder/tracks", params={"path": str(music)}).json()
    check("the folder now marks it as held", body["in_collection"] == [str(one)], str(body["in_collection"]))

    print("== a held file is never added twice, but still reaches its target ==")
    library = STATE.library_id
    eset = exports.create(library, name="Folder Export", target="traktor",
                          destination=str(Path(tempfile.mkdtemp()) / "out"))
    req = {"track_ids": [str(one), str(two)], "mode": "reference",
           "playlist_id": playlist, "export_id": eset.id}
    p = c.post("/api/folder/add/preview", json=req).json()
    check("the preview reports it as already held", p["existing"] == ["First Light"], str(p))
    job = wait(c, c.post("/api/folder/add", json=req).json())
    check("the job finishes", job["state"] == "done", str(job))
    check("only the NEW file was added", len(a.tracks) == before + 2, f"{len(a.tracks) - before}")
    check("the held one kept its id", job["result"]["track_ids"][0] == added)
    new = job["result"]["track_ids"][1]
    check("the playlist holds both, once each", a.playlist_entries(playlist) == [added, new],
          str(a.playlist_entries(playlist)))
    check("and so does the export", exports.get(library, eset.id).track_ids == [added, new])

    print("== copy: the collection points at the copy ==")
    three_dir = music / "Deeper"
    c.get("/api/folder/tracks", params={"path": str(three_dir)})
    dest = Path(tempfile.mkdtemp()) / "Imported"
    req = {"track_ids": [str(three_dir / "three.flac")], "mode": "copy", "destination": str(dest)}
    p = c.post("/api/folder/add/preview", json=req).json()
    check("the preview sizes the copy", p["total_bytes"] > 0, str(p))
    job = wait(c, c.post("/api/folder/add", json=req).json())
    check("the job finishes", job["state"] == "done", str(job))
    copied_id = job["result"]["track_ids"][0]
    check("the audio was copied", (dest / "three.flac").is_file())
    check("the entry points at the copy", a.audio_path(copied_id) == dest / "three.flac",
          str(a.audio_path(copied_id)))
    check("the original is untouched", (three_dir / "three.flac").is_file())
    check("copy without a destination is refused",
          c.post("/api/folder/add", json={"track_ids": [str(one)], "mode": "copy"}).status_code == 400)
    check("an unscanned file cannot be added",
          c.post("/api/folder/add/preview",
                 json={"track_ids": ["/etc/passwd"], "mode": "reference"}).status_code == 404)

    print("== hidden entries ==")
    check("AppleDouble twins are hidden", places.is_hidden(music / "._one.flac"))
    check("OS housekeeping is hidden", places.is_hidden(music / ".Trashes"))
    check("an ordinary file is not", not places.is_hidden(one))

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
_DATA.cleanup()
raise SystemExit(1 if failed else 0)
