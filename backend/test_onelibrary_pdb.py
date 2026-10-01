"""The legacy Device Library (`export.pdb`) kept in step with a stick's OneLibrary.

On a temp copy of Goober (`fixtures/onelibrary-goober/`), whose three databases
are rekordbox's own bytes. Pins:

  * reading: every rekordbox track row decodes, and re-encodes to the same
    meaningful bytes (rekordbox leaves stale bytes in alignment padding and
    between rows — `track_extent`);
  * ids are mirrored: rekordbox exported both libraries from the same ids, so a
    no-op rebuild changes NOTHING, and a no-op save leaves the file byte-identical;
  * an edit reaches the pdb (title, rating, artist, a new playlist, a reorder —
    the last two are what rekordbox's own edits failed to carry over), every table
    the edit does not touch stays byte-identical, and `exportExt.pdb` is never
    written;
  * another app's write to `export.pdb` since open is refused, not undone.

Verified in rekordbox 7 on the real Goober (2026-10-01): both library views
matched after exactly these edits.
"""
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-olpdb-appdata-")

from konduktor.adapters.onelibrary import device_library  # noqa: E402
from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.adapters.rekordbox import pdb  # noqa: E402
from konduktor.core.adapter import InvalidCommand  # noqa: E402

HERE = Path(__file__).resolve().parent
GOOBER = HERE / "fixtures" / "onelibrary-goober"
DEMO = HERE / "fixtures" / "onelibrary"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def copy_of(fixture: Path, name: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="konduktor-olpdb-")) / name
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns("after", "rekordbox-edited", "README.md"))
    return root


PDB = GOOBER / "PIONEER" / "rekordbox" / "export.pdb"
raw = PDB.read_bytes()

print("== reading rekordbox's rows ==")
tracks = pdb.table_rows(raw, pdb.TRACKS)
check("every track row is found (as many as the reader sees)", len(tracks) == len(pdb.read(raw).tracks) == 11)
same = [pdb.track_row(*pdb.track_fields(r)) for r in tracks]
check("each re-encodes to the same bytes up to its 4-byte alignment padding",
      all(a[:len(a) - 3] == b[:len(a) - 3] and len(a) == len(b) for a, b in zip(tracks, same)))
check("a row ends at its last string, not at the next row (stale heap tails)",
      all(pdb.track_extent(r) == len(r) for r in tracks))
check("artists, keys, artwork and the playlist tree all read",
      [len(pdb.table_rows(raw, t)) for t in (pdb.ARTISTS, pdb.KEYS, pdb.ARTWORK, pdb.PLAYLIST_TREE)]
      == [5, 4, 3, 2])

print("== ids are mirrored: nothing to do on an unedited stick ==")
drive = copy_of(GOOBER, "Goober")
a = OneLibraryAdapter(drive)
rebuilt = device_library.rebuild(raw, a._store._require_db().session)
check("a rebuild of an unedited stick changes nothing", rebuilt is None)
a.set_track_metadata(a.tracks[0].id, {"title": a.tracks[0].title})  # dirty, but a no-op
a.save()
check("a save that changes nothing in it leaves export.pdb byte-identical",
      (drive / "PIONEER" / "rekordbox" / "export.pdb").read_bytes() == raw)
a.close()

print("== an edit reaches the Device Library, and only the edit ==")
drive = copy_of(GOOBER, "Goober")
P = drive / "PIONEER" / "rekordbox"
ext_before = (P / "exportExt.pdb").read_bytes()
a = OneLibraryAdapter(drive)
by = lambda prefix: next(t for t in a.tracks if t.title.startswith(prefix))  # noqa: E731
tian, bensley, alkyn = by("Tian"), by("Bensley"), by("Alkyn")
a.set_track_metadata(tian.id, {"title": tian.title + " X"})
a.set_track_metadata(bensley.id, {"rating": 3})
a.set_track_metadata(alkyn.id, {"artist": "Alkyn"})
demos = next(n for n in a.playlist_tree() if n.name == "demos")
order = a.playlist_entries(demos.id)
a.set_playlist_entries(demos.id, order[::-1])
probe = a.create_playlist("Probe")
a.set_playlist_entries(probe, [alkyn.id])
a.save()
after = (P / "export.pdb").read_bytes()
A, B = pdb.read(raw), pdb.read(after)
old = {t["id"]: t for t in A.tracks}
new = {t["id"]: t for t in B.tracks}
diffs = {i: {k for k in old[i] if old[i][k] != new[i][k] and k != "index_shift"} for i in old}
check("only the edited fields of the edited tracks changed",
      {i: d for i, d in diffs.items() if d} == {6: {"title"}, 7: {"rating"}, 8: {"artist_id"}},
      str({i: d for i, d in diffs.items() if d}))
check("the new artist is mirrored under OneLibrary's own id",
      B.artists.get(new[8]["artist_id"]) == "Alkyn" and new[8]["artist_id"] == 6)
tree = sorted((p["name"], p["sort"]) for p in B.playlists)
check("the new playlist reached it, on top (rekordbox's own edit did not carry it over)",
      tree == [("Para Ben", 1), ("Probe", 0), ("demos", 2)], str(tree))
demos_entries = [t for _i, t, p in sorted(B.entries) if p == int(demos.id)]
check("and so did the reorder", [new[t]["file_path"] for t in demos_entries] == order[::-1])
for table, name in ((pdb.COLORS, "colours"), (pdb.KEYS, "keys"), (pdb.ARTWORK, "artwork"),
                    (pdb.GENRES, "genres"), (pdb.LABELS, "labels"), (pdb.ALBUMS, "albums"),
                    (pdb.HISTORY, "history"), (16, "browse columns"), (17, "menus")):
    check(f"untouched table ({name}) is byte-identical",
          pdb.table_rows(raw, table) == pdb.table_rows(after, table))
check("an unchanged track row keeps rekordbox's own bytes (stale padding and all)",
      [r for r in pdb.table_rows(after, pdb.TRACKS) if pdb.row_id(pdb.TRACKS, r) == 1]
      == [r for r in tracks if pdb.row_id(pdb.TRACKS, r) == 1])
check("the file did not grow", len(after) == len(raw))
check("exportExt.pdb is never written", (P / "exportExt.pdb").read_bytes() == ext_before)
check("the previous export.pdb was backed up",
      any(p.read_bytes() == raw for p in
          (Path(os.environ["KONDUKTOR_DATA_DIR"]) / "onelibrary" / "backups").rglob("export.pdb")))
a.close()

print("== another app wrote export.pdb since open ==")
drive = copy_of(GOOBER, "Goober")
a = OneLibraryAdapter(drive)
a.set_track_metadata(a.tracks[0].id, {"rating": 5})
time.sleep(0.01)
os.utime(drive / "PIONEER" / "rekordbox" / "export.pdb")
try:
    a.save()
    refused = False
except InvalidCommand:
    refused = True
check("Save refuses", refused and a.dirty)
a.close()

print("== a stick with no Device Library ==")
drive = copy_of(DEMO, "Dingus")
a = OneLibraryAdapter(drive)
a.set_track_metadata(a.tracks[0].id, {"rating": 2})
a.save()
check("saving does not create one", not (drive / "PIONEER" / "rekordbox" / "export.pdb").exists())
a.close()

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
