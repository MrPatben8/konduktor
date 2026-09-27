"""Auto path remapping: the open-time missing-files check.

Two layers. `core.relocate.search` on temp folders, where where-the-files-are is
known; then the Traktor path end to end through the routes, on a collection
built here whose stored volume is a Windows drive letter — the real case that
motivated this (a collection made on Windows with the SSD as `X:`, opened on a
Mac where it is `/Volumes/<name>`).

Pins the things that would fail SILENTLY:

  * a drive and its clone are AMBIGUOUS, never ranked — a wrong guess would play
    and tag files on the wrong drive;
  * a filename alone at a drive's root is not a match;
  * a volume in which even ONE track resolves is never asked about;
  * nothing is applied before the user answers, and only a proposed mapping is;
  * the answer is session-only: the collection and prefs are untouched, and a
    reopen asks again;
  * a saved manual mapping is respected — its volume is not asked about.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

_DATA = tempfile.TemporaryDirectory()
os.environ["KONDUKTOR_DATA_DIR"] = _DATA.name
os.environ.pop("KONDUKTOR_NML", None)

from konduktor.core.pathmap import PathMapping  # noqa: E402
from konduktor.core.relocate import PathGroup, search  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


tmp = Path(tempfile.mkdtemp())
ssd = tmp / "SSD"
rel = [f"Music Library/{g}/t{i}.mp3" for g in ("EDM", "House", "DnB") for i in range(20)]
for r in rel:
    touch(ssd / r)
(ssd / rel[7]).unlink()  # one track genuinely deleted
stored = [f"X:/{r}" for r in rel]
x = PathGroup(label="X:", root="X:", paths=stored)

print("== a changed drive letter is found ==")
[p] = search([x], [ssd, tmp / "Nowhere"])
check("found, with one candidate", p.status == "found", p.status)
c = p.candidates[0]
check("maps the drive letter onto the drive", c.mapping == PathMapping.make("X:", str(ssd)), str(c.mapping))
check("the count is verified over the WHOLE group", c.found == 59 and p.total == 60, f"{c.found}/{p.total}")

print("== a drive and its clone are ambiguous ==")
clone = tmp / "Clone"
shutil.copytree(ssd, clone)
[p] = search([x], [ssd, clone])
check("ambiguous, both offered", p.status == "ambiguous" and len(p.candidates) == 2, p.status)
check("both targets named", {c.mapping.to_prefix for c in p.candidates} == {str(ssd), str(clone)})

print("== a partial copy loses to the whole one ==")
partial = tmp / "Partial"
for r in rel[:8]:
    touch(partial / r)
[p] = search([x], [ssd, partial])
check("found on the full drive only",
      p.status == "found" and p.candidates[0].mapping.to_prefix == str(ssd), p.status)

print("== nothing found ==")
[p] = search([PathGroup("E:", "E:", ["E:/Gone/a.mp3", "E:/Gone/b.mp3"])], [ssd])
check("not found, no candidates", p.status == "not_found" and not p.candidates)

print("== a filename alone at a drive's root is not a match ==")
bare = tmp / "Bare"
for i in range(5):
    touch(bare / f"solo{i}.mp3")
[p] = search([PathGroup("X:", "X:", [f"X:/Deep/Folder/solo{i}.mp3" for i in range(5)])], [bare])
check("not found", p.status == "not_found", p.status)

print("== leading folders are dropped: another user's home ==")
home = tmp / "home"
for i in range(5):
    touch(home / "Music" / "Lib" / f"x{i}.mp3")
[p] = search([PathGroup("Macintosh HD", "/", [f"/Users/alice/Music/Lib/x{i}.mp3" for i in range(5)])], [home])
check("found", p.status == "found", p.status)
check("maps the old home onto the new one",
      p.status == "found" and p.candidates[0].mapping == PathMapping.make("/Users/alice", str(home)),
      str(p.candidates[0].mapping) if p.candidates else "")

print("== a one-track group is taken on one ==")
[p] = search([PathGroup("X:", "X:", [stored[0]])], [ssd])
check("found", p.status == "found" and p.candidates[0].found == 1, p.status)


# ---- the Traktor path, through the routes --------------------------------------
from konduktor import relocation  # noqa: E402

# Search only the fake SSD: the real drives would make the answer depend on the machine.
relocation.candidate_roots = lambda: [ssd]

from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.adapters.traktor.export import SKELETON  # noqa: E402
from konduktor.adapters.traktor.locations import os_path_to_location  # noqa: E402
from konduktor.core.adapter import NewTrack  # noqa: E402
from konduktor.core.model import Track  # noqa: E402

lib = tmp / "lib" / "collection.nml"
lib.parent.mkdir()
lib.write_text(SKELETON, encoding="utf-8")
on_ssd = [ssd / r for r in rel[:3]]
local = touch(tmp / "Local" / "keep.mp3")  # stays where it is: its volume resolves
gone = touch(tmp / "Gone" / "lost.mp3")
a = TraktorAdapter(lib)
a.add_tracks([NewTrack(track=Track(id="", title=p.stem), audio_path=p) for p in [*on_ssd, local, gone]])
a.save()

# Re-home the stored locations as a Windows machine would have written them.
text = lib.read_text(encoding="utf-8")
ssd_dir = os_path_to_location(ssd / "x")[1]  # "/:…/:SSD/:"
gone_dir = os_path_to_location(gone)[1]
text = re.sub(rf'DIR="{re.escape(ssd_dir)}([^"]*)" (FILE="[^"]*") VOLUME="[^"]*"', r'DIR="/:\1" \2 VOLUME="X:"', text)
text = re.sub(rf'DIR="{re.escape(gone_dir)}" (FILE="[^"]*") VOLUME="[^"]*"', r'DIR="/:Gone/:" \1 VOLUME="E:"', text)
lib.write_text(text, encoding="utf-8")
original = lib.read_bytes()
check("the fixture has X: and E: locations", 'VOLUME="X:"' in text and 'VOLUME="E:"' in text)

from fastapi.testclient import TestClient  # noqa: E402

import konduktor.main as main  # noqa: E402
from konduktor import prefs  # noqa: E402


def ssd_ids():
    return [t.id for t in main.require_adapter().tracks if t.id.startswith("X:")]


def plays(c, tid):
    return c.get("/api/tracks/audio", params={"track_id": tid}).status_code == 200


with TestClient(main.app, raise_server_exceptions=False) as c:
    check("opens", c.post("/api/library/open", json={"path": str(lib)}).status_code == 200)

    print("== the check proposes, and applies nothing yet ==")
    r = c.get("/api/library/relocation")
    check("answers 200", r.status_code == 200, r.text[:300])
    vols = {v["label"]: v for v in r.json()["volumes"]}
    check("asks about X: and E: only — the volume that resolves is left alone",
          set(vols) == {"X:", "E:"}, str(sorted(vols)))
    xv = vols.get("X:", {})
    check("X: found on the SSD, 3 of 3",
          xv.get("status") == "found" and xv["candidates"][0]["to"] == str(ssd)
          and xv["candidates"][0]["found"] == 3, str(xv))
    check("E: not found", vols.get("E:", {}).get("status") == "not_found")
    tid = ssd_ids()[0]
    check("before answering, an SSD track does not play", not plays(c, tid))

    print("== only a proposed mapping is accepted ==")
    r = c.post("/api/library/relocation", json={"mappings": [{"from": "X:", "to": str(tmp)}]})
    check("an unproposed mapping is a 400", r.status_code == 400, r.text[:200])

    print("== Apply ==")
    r = c.post("/api/library/relocation", json={"mappings": [{"from": "X:", "to": str(ssd)}]})
    check("answers 200 with the track count", r.status_code == 200 and r.json()["tracks"] == 3, r.text[:200])
    check("the SSD tracks now play", all(plays(c, t) for t in ssd_ids()))
    check("the check is settled", c.get("/api/library/relocation").json()["volumes"] == [])
    check("a second answer is refused",
          c.post("/api/library/relocation", json={"mappings": []}).status_code == 400)
    check("track ids are unchanged (nothing was rewritten)", tid in ssd_ids() and len(ssd_ids()) == 3)
    check("the collection file is untouched", lib.read_bytes() == original)
    check("nothing is saved to prefs", prefs.get_path_mapping(str(lib)) is None)

    print("== session-only: a reopen asks again ==")
    c.post("/api/library/open", json={"path": str(lib)})
    vols = {v["label"] for v in c.get("/api/library/relocation").json()["volumes"]}
    check("asked again", vols == {"X:", "E:"}, str(vols))
    check("and the mapping is gone", not plays(c, tid))

    print("== Not now ==")
    r = c.post("/api/library/relocation", json={"mappings": []})
    check("answers 200", r.status_code == 200, r.text[:200])
    check("settled, nothing applied",
          c.get("/api/library/relocation").json()["volumes"] == [] and not plays(c, tid))

    print("== a saved manual mapping is respected ==")
    c.put("/api/library/path-mapping", json={"from": "X:", "to": str(ssd)})
    c.post("/api/library/open", json={"path": str(lib)})
    vols = {v["label"] for v in c.get("/api/library/relocation").json()["volumes"]}
    check("only E: is asked about", vols == {"E:"}, str(vols))
    c.put("/api/library/path-mapping", json={"from": "", "to": ""})

shutil.rmtree(tmp, ignore_errors=True)
print("\nFAILED" if failed else "\nALL PASSED")
raise SystemExit(1 if failed else 0)
