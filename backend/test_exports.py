"""Export sets: curation that outlives the session, keyed to a library.

The properties worth pinning are the ones that fail SILENTLY:

  * a **live** playlist reference picks up later edits — a set that quietly
    froze at curation time would still look right and ship the wrong tracks;
  * contents **dedupe** across playlists and loose tracks, so one track is one
    file copy;
  * a deleted playlist or track is **surfaced**, never dropped — a gig stick
    missing four tracks with no warning is this feature's worst failure;
  * sets are **per library**, so opening another collection shows another shelf;
  * two sets pointing at one destination are **caught**, because an export
    CLEARS its destination and would wipe the other's output.

Runs against a temp COPY of the real collection, and a temp app-data dir, so it
touches neither the user's library nor their real export sets.
"""
import os
import shutil
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["KONDUKTOR_DATA_DIR"] = _TMP.name

from konduktor import exports  # noqa: E402
from konduktor.app_state import STATE  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


REAL = Path(__file__).resolve().parents[1] / "collection.nml"
work = Path(tempfile.mkdtemp())
shutil.copy2(REAL, work / "collection.nml")
STATE.open(work / "collection.nml")
adapter = STATE.adapter
LIB = STATE.library_id
check("opening a library issues an id", bool(LIB))

# A real playlist with tracks in it, to reference.
def _first_playlist(nodes):
    for node in nodes or []:
        if node.kind == "playlist" and (node.count or 0) >= 2:
            return node
        found = _first_playlist(getattr(node, "children", None))
        if found:
            return found
    return None


playlist = _first_playlist(adapter.playlist_tree())
assert playlist is not None, "the fixture collection has no playlist with 2+ tracks"
playlist_tracks = adapter.playlist_entries(playlist.id) or []
all_tracks = [t.id for t in adapter.tracks]

print("== creating, listing, updating, deleting ==")
s1 = exports.create(LIB, name="GIG", target="traktor", destination=str(work / "out"))
check("a set is created with an id", bool(s1.id))
check("it lists", [s.id for s in exports.all_sets(LIB)] == [s1.id])
check("it survives a reload from disk", exports.get(LIB, s1.id).name == "GIG")
exports.update(LIB, s1.id, name="Ibiza")
check("rename persists", exports.get(LIB, s1.id).name == "Ibiza")
check("and bumps modified", exports.get(LIB, s1.id).modified >= s1.created)

print("== membership: ordered, deduped, and removable ==")
exports.add(LIB, s1.id, track_ids=[all_tracks[0], all_tracks[1]])
exports.add(LIB, s1.id, track_ids=[all_tracks[1], all_tracks[2]])
check("duplicates are ignored, order kept",
      exports.get(LIB, s1.id).track_ids == all_tracks[:3],
      exports.get(LIB, s1.id).track_ids)
exports.add(LIB, s1.id, playlist_ids=[playlist.id, playlist.id])
check("a playlist is added once", exports.get(LIB, s1.id).playlist_ids == [playlist.id])
exports.remove(LIB, s1.id, track_ids=[all_tracks[1]])
check("a loose track is removable",
      exports.get(LIB, s1.id).track_ids == [all_tracks[0], all_tracks[2]])

print("== resolution is LIVE and deduped ==")
resolved = exports.resolve(adapter, exports.get(LIB, s1.id))
check("the referenced playlist resolves by name",
      resolved.playlists[0].name == playlist.name and not resolved.playlists[0].missing)
check("with its CURRENT tracks", resolved.playlists[0].track_ids == playlist_tracks)
# The whole point of a reference: what ships is what the playlist holds at
# export time, not what it held when it was dragged in.
spare = next(t for t in all_tracks if t not in playlist_tracks)
adapter.set_playlist_entries(playlist.id, playlist_tracks + [spare])
again = exports.resolve(adapter, exports.get(LIB, s1.id))
check("a track added to the playlist LATER is picked up",
      spare in again.playlists[0].track_ids)
check("without the set itself changing",
      exports.get(LIB, s1.id).playlist_ids == [playlist.id])
adapter.set_playlist_entries(playlist.id, playlist_tracks)  # put it back

# One track, one file copy — even when it is both loose and in a playlist.
exports.add(LIB, s1.id, track_ids=[playlist_tracks[0]])
deduped = exports.resolve(adapter, exports.get(LIB, s1.id)).track_ids
check("a track in a playlist AND loose appears once",
      deduped.count(playlist_tracks[0]) == 1, deduped)
check("the total is the union, not the sum",
      len(deduped) == len(set(deduped)) and len(deduped) >= len(playlist_tracks))
exports.remove(LIB, s1.id, track_ids=[playlist_tracks[0]])

print("== what is gone is SURFACED, never silently dropped ==")
exports.add(LIB, s1.id, track_ids=["Macintosh HD/:nowhere/:ghost.mp3"])
gone = exports.resolve(adapter, exports.get(LIB, s1.id))
check("a track the library no longer has is reported",
      gone.dangling_track_ids == ["Macintosh HD/:nowhere/:ghost.mp3"])
check("and is NOT counted as exportable",
      "Macintosh HD/:nowhere/:ghost.mp3" not in gone.track_ids)
exports.remove(LIB, s1.id, track_ids=["Macintosh HD/:nowhere/:ghost.mp3"])

s2 = exports.create(LIB, name="Deleted playlist test", target="traktor",
                    destination=str(work / "out2"))
exports.add(LIB, s2.id, playlist_ids=["no-such-playlist"])
ghost = exports.resolve(adapter, exports.get(LIB, s2.id))
check("a deleted playlist is shown as missing, not dropped",
      len(ghost.playlists) == 1 and ghost.playlists[0].missing)
check("and contributes no tracks", ghost.track_ids == [])

print("== two sets, one destination ==")
# An export CLEARS its destination, so the second would wipe the first's output.
# Caught when the destination is chosen, not when it is too late to warn.
check("no conflict for distinct destinations",
      exports.destination_conflict(LIB, str(work / "out"), ignore=s1.id) is None)
s3 = exports.create(LIB, name="Clash", target="traktor", destination=str(work / "out"))
check("a clash is reported by NAME",
      exports.destination_conflict(LIB, str(work / "out"), ignore=s3.id) == "Ibiza")
check("a set never conflicts with itself",
      exports.destination_conflict(LIB, str(work / "out"), ignore=s1.id) == "Clash")
check("paths are compared normalised, not as strings",
      exports.destination_conflict(LIB, str(work / "out") + "/./", ignore=s3.id) == "Ibiza")
exports.delete(LIB, s3.id)

print("== following track ids that change under a path remap ==")
exports.retarget(LIB, {all_tracks[0]: "Macintosh HD/:Moved/:one.mp3"})
check("a renamed id is followed",
      "Macintosh HD/:Moved/:one.mp3" in exports.get(LIB, s1.id).track_ids)
check("and the old one is gone", all_tracks[0] not in exports.get(LIB, s1.id).track_ids)
exports.retarget(LIB, {"Macintosh HD/:Moved/:one.mp3": all_tracks[0]})
check("an empty mapping is a no-op",
      (exports.retarget(LIB, {}), exports.get(LIB, s1.id).track_ids[0])[1] == all_tracks[0])

print("== sets belong to ONE library ==")
other = Path(tempfile.mkdtemp()) / "Other"
other.mkdir()
shutil.copy2(REAL, other / "collection.nml")
STATE.open(other / "collection.nml")
check("a different library has a different id", STATE.library_id != LIB)
check("and an empty shelf", exports.all_sets(STATE.library_id) == [])
check("the first library's sets are untouched", len(exports.all_sets(LIB)) == 2)
# Stored per library, so one shelf cannot be read through the other's id.
check("stores are separate files",
      exports._store_path(LIB) != exports._store_path(STATE.library_id))

print("== deleting a set leaves the destination alone ==")
dest = work / "out"
dest.mkdir(parents=True, exist_ok=True)
(dest / "already-there.txt").write_text("x")
exports.delete(LIB, s1.id)
check("the set is gone", exports.get(LIB, s1.id) is None)
check("the destination folder is NOT touched", (dest / "already-there.txt").is_file())

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
_TMP.cleanup()
raise SystemExit(1 if failed else 0)
