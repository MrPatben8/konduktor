"""Writing a brand-new Traktor collection from nothing.

Everywhere else in Konduktor the write target was parsed from a file Traktor
wrote, and `test_save_fidelity.py` guards that nothing else in it moves. An
export has no original, so the guarantee has to be different: **the result must
be a collection Traktor would have written** — the right skeleton, a coherent
playlist tree, and prep that survived the crossing intact.

The property with the most riding on it is the LOCATION: Traktor stores a volume
NAME plus a volume-relative path, never a host path, which is the whole reason a
USB export is portable. If an export ever wrote absolute host paths it would
work perfectly on the machine that made it and resolve to nothing at the gig.

Runs against the 8,485-entry fixture collection, so the tracks have real prep —
flexible grids, hot cues on specific pads — rather than anything invented here.
"""
import os
import re
import tempfile
from pathlib import Path

os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp()

from konduktor.adapters.traktor.driver import TraktorDriver  # noqa: E402
from konduktor.adapters.traktor.export import SKELETON  # noqa: E402
from konduktor.core import export as core_export  # noqa: E402
from konduktor.core.export import ExportPayload, ExportPlaylist, ExportTrack  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


FIXTURE = Path(__file__).resolve().parents[1] / "collection.nml"
src = TraktorDriver().open(FIXTURE)
exporter = core_export.for_platform("traktor")

print("== the target declares itself without a library to read ==")
check("traktor is a target", exporter is not None)
check("rekordbox is NOT", core_export.for_platform("rekordbox") is None)
check("onelibrary is NOT", core_export.for_platform("onelibrary") is None)
caps = exporter.capabilities()
check("static capabilities answer with no path", caps.platform == "traktor" and caps.writable)
check("and describe the FORMAT, not an instance", caps.cues.hotcue_slots == 8)

# ---- build a payload out of real tracks --------------------------------------
# One with a flexible (multi-marker) grid, one with several hot cues: the two
# things most likely to be quietly lost.
flexible = next(t for t in src.tracks if (t.grid_marker_count or 0) > 1)
cued = next(t for t in src.tracks if (t.cue_count or 0) >= 3 and t.id != flexible.id)
plain = next(t for t in src.tracks if t.id not in (flexible.id, cued.id))
chosen = [flexible, cued, plain]

dest = Path(tempfile.mkdtemp()) / "GIG"
dest.mkdir(parents=True)
items = []
for track in chosen:
    audio = dest / "Music" / "Library" / Path(track.filepath).name
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"\0" * 64)
    items.append(ExportTrack(track=track, destination=audio, cues=src.track_cues(track.id)))

payload = ExportPayload(
    name="GIG",
    tracks=items,
    playlists=[
        ExportPlaylist(name="Peak Time", track_ids=[flexible.id, cued.id], folders=["House"]),
        ExportPlaylist(name="Warmup", track_ids=[cued.id], folders=["House", "Slow"]),
    ],
)
written = exporter.write(payload, dest)
text = written.read_text(encoding="utf-8")

print("== the skeleton is what Traktor writes ==")
check("named collection.nml", written.name == "collection.nml")
check("the XML declaration is there", text.startswith('<?xml version="1.0" encoding="UTF-8"'))
check("NML VERSION 20", '<NML VERSION="20">' in text)
check("a HEAD element", "<HEAD " in text)
# Measured from a real Traktor file: it has no MUSICFOLDERS at all.
check("no MUSICFOLDERS is required", "<MUSICFOLDERS" not in SKELETON)
check("SETS is present and empty", '<SETS ENTRIES="0">' in text)
# Playlists cannot sit at the top level; they hang off a $ROOT FOLDER node.
check("PLAYLISTS wraps a $ROOT folder", '<NODE TYPE="FOLDER" NAME="$ROOT">' in text)
check("INDEXING is present", '<SORTING_INFO PATH="$COLLECTION">' in text)
check("the ENTRIES header matches the entry count",
      f'<COLLECTION ENTRIES="{len(chosen)}">' in text,
      re.search(r'<COLLECTION ENTRIES="\d+"', text).group(0))

print("== LOCATION is volume-relative — the reason a stick is portable ==")
locations = re.findall(r'<LOCATION DIR="([^"]*)" FILE="([^"]*)" VOLUME="([^"]*)"', text)
check("every track has a location", len(locations) == len(chosen))
check("no host path leaks into DIR",
      all(not d.startswith("/:Users/:") or "/:Music/:Library/:" in d for d, _f, _v in locations),
      [d for d, _f, _v in locations])
check("DIR is volume-relative, ending at the audio folder",
      all(d.endswith("/:Music/:Library/:") for d, _f, _v in locations),
      [d for d, _f, _v in locations])
check("VOLUME is a name, not a path",
      all("/" not in v for _d, _f, v in locations), [v for _d, _f, v in locations])
check("FILE is just the filename",
      all("/" not in f for _d, f, _v in locations))

print("== it re-opens as a library ==")
back = TraktorDriver().open(written)
check("every track came across", len(back.tracks) == len(chosen))
by_title = {t.title: t for t in back.tracks}
check("titles survived", all(t.title in by_title for t in chosen),
      sorted(set(t.title for t in chosen) - set(by_title)))
check("artist survived", by_title[plain.title].artist == plain.artist)
# Traktor→Traktor copies the key string verbatim: a user's notation is a
# preference their exported library should not silently change.
if plain.key:
    check("key notation is copied, not converted", by_title[plain.title].key == plain.key)

print("== the playlist tree ==")
tree = {}


def walk(nodes, path=()):
    for n in nodes:
        tree[path + (n.name,)] = n
        walk(n.children, path + (n.name,))


walk(back.playlist_tree())
check("one folder named after the export", ("GIG",) in tree and tree[("GIG",)].kind == "folder")
check("nested folders are preserved", ("GIG", "House") in tree)
check("and nest further", ("GIG", "House", "Slow") in tree)
check("playlists land in their folder",
      ("GIG", "House", "Peak Time") in tree and ("GIG", "House", "Slow", "Warmup") in tree)
check("with the right counts",
      tree[("GIG", "House", "Peak Time")].count == 2
      and tree[("GIG", "House", "Slow", "Warmup")].count == 1)
# A track added individually would otherwise be reachable only by search.
check("loose tracks get an 'Other' playlist", ("GIG", "Other") in tree)
check("holding exactly the ones no playlist claimed",
      tree[("GIG", "Other")].count == 1, tree[("GIG", "Other")].count)
other_ids = back.playlist_entries(tree[("GIG", "Other")].id)
check("and it is the right track",
      back.track(other_ids[0]).title == plain.title)

print("== the prep crossed intact ==")
src_grid = src.track_cues(flexible.id)
out_grid = back.track_cues(by_title[flexible.title].id)
check("a flexible multi-marker grid survives",
      len(out_grid.grid_markers) == len(src_grid.grid_markers),
      f"{len(out_grid.grid_markers)} vs {len(src_grid.grid_markers)}")
check("with its tempo changes",
      [round(m.bpm, 3) for m in out_grid.grid_markers]
      == [round(m.bpm, 3) for m in src_grid.grid_markers])
check("and its marker positions",
      [round(m.start, 3) for m in out_grid.grid_markers]
      == [round(m.start, 3) for m in src_grid.grid_markers])
# Ben's decision: exported grids are NOT locked, so they stay editable like any
# other track. If Traktor turns out to re-analyse and flatten them, this is what
# makes that visible rather than hidden behind a lock.
check("grids are not locked", not by_title[flexible.title].grid_locked)

src_cues = src.track_cues(cued.id)
out_cues = back.track_cues(by_title[cued.title].id)
src_hot = {c.slot: round(c.start, 3) for c in src_cues.cues if c.role == "hotcue"}
out_hot = {c.slot: round(c.start, 3) for c in out_cues.cues if c.role == "hotcue"}
check("hot cues keep their PAD, not just their position", out_hot == src_hot,
      f"{out_hot} vs {src_hot}")

print("== writing twice into the same folder replaces, never appends ==")
again = exporter.write(payload, dest)
check("the entry count did not double",
      f'<COLLECTION ENTRIES="{len(chosen)}">' in again.read_text(encoding="utf-8"))
check("and the tree did not duplicate",
      len(TraktorDriver().open(again).playlist_tree()) == 1)

# ==============================================================================
# Running an export: the copy, the safety rules, and the rollback.
# ==============================================================================
import tempfile as _tf  # noqa: E402
import time  # noqa: E402

os.environ.setdefault("KONDUKTOR_DATA_DIR", _tf.mkdtemp())
from konduktor import exporter, exports  # noqa: E402
from konduktor.app_state import STATE  # noqa: E402
from konduktor.core.export import ExportPayload as _Payload  # noqa: E402
from konduktor.jobs import JOBS, JobCancelled  # noqa: E402

# A SOURCE library whose audio genuinely exists on disk — built with the very
# exporter tested above, which is the cheapest way to get real LOCATIONs.
lib_root = Path(tempfile.mkdtemp()) / "MyMusic"
seed_items = []
for i, track in enumerate(src.tracks[:4]):
    audio = lib_root / ("House" if i % 2 else "Techno") / Path(track.filepath).name
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"\0" * 300_000)
    seed_items.append(ExportTrack(track=track, destination=audio, cues=src.track_cues(track.id)))
exporter_obj = core_export.for_platform("traktor")
exporter_obj.write(
    _Payload(name="Source", tracks=seed_items,
             playlists=[ExportPlaylist(name="Peak Time",
                                       track_ids=[src.tracks[0].id, src.tracks[1].id])]),
    lib_root,
)
STATE.open(lib_root / "collection.nml")
ad = STATE.adapter
LIB = STATE.library_id

dest = Path(tempfile.mkdtemp()) / "GIG"
eset = exports.create(LIB, name="GIG", target="traktor", destination=str(dest))
source_pl = next(n for n in ad.playlist_tree()[0].children if n.kind == "playlist")
exports.add(LIB, eset.id, playlist_ids=[source_pl.id], track_ids=[ad.tracks[3].id])
eset = exports.get(LIB, eset.id)


def run_now(plan):
    job = JOBS.submit("export", lambda h: exporter.run(ad, eset, plan, h))
    while not job.finished:
        time.sleep(0.02)
    return job


print("== the plan mirrors the source tree, relative to its shared root ==")
built = exporter.plan(ad, eset)
check("nothing is blocking it", built.blocked is None, str(built.blocked))
check("every track has somewhere to go", all(t.destination for t in built.exportable))
rel = [str(t.destination.relative_to(dest)) for t in built.exportable]
# Mirroring ABSOLUTE paths would put the user's home directory on the stick.
check("the user's own folders are preserved",
      all(r.startswith(("House/", "Techno/")) for r in rel), rel)
check("and nothing above the shared root comes with them",
      not any("Users" in r or r.startswith("/") for r in rel), rel)
check("space is checked before starting", exporter.space_for(built) is not None)

print("== running it ==")
job = run_now(built)
check("it finishes", job.state == "done", f"{job.state}: {job.error}")
check("every track was copied", job.result["tracks"] == len(built.exportable))
check("the library was written", (dest / "collection.nml").is_file())
check("and a manifest beside it", (dest / exporter.MANIFEST_NAME).is_file())
check("the audio is really there",
      all((dest / r).stat().st_size == 300_000 for r in rel))
check("the result re-opens as a library",
      len(TraktorDriver().open(dest / "collection.nml").tracks) == len(built.exportable))

print("== Konduktor only ever deletes what Konduktor wrote ==")
mine = dest / "MY OWN NOTES.txt"
mine.write_text("do not delete")
again = exporter.plan(ad, exports.get(LIB, eset.id))
# The folder is now non-empty AND has a manifest, so it is ours to replace.
check("a folder we wrote is not blocked", again.blocked is None)
check("and is reported as a replacement", again.replacing)
job2 = run_now(again)
check("the re-export succeeds", job2.state == "done", f"{job2.state}: {job2.error}")
check("a file the USER put there survives", mine.is_file() and mine.read_text() == "do not delete")
check("the audio did not double up",
      len([p for p in dest.rglob("*") if p.is_file()]) == len(rel) + 3,  # + nml, manifest, notes
      sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()))

print("== a folder we did NOT write is refused, not confirmed ==")
foreign = Path(tempfile.mkdtemp()) / "Documents"
foreign.mkdir()
(foreign / "tax return.pdf").write_text("important")
exports.update(LIB, eset.id, destination=str(foreign))
blocked = exporter.plan(ad, exports.get(LIB, eset.id))
check("it is blocked", blocked.blocked == exporter.BLOCKED_OCCUPIED, str(blocked.blocked))
check("the user's file is untouched", (foreign / "tax return.pdf").read_text() == "important")
# There is deliberately no "do it anyway": run() refuses a blocked plan even if
# a caller ignored the preview.
try:
    exporter.run(ad, exports.get(LIB, eset.id), blocked, None)
    check("run refuses a blocked plan", False, "it ran")
except ValueError:
    check("run refuses a blocked plan even when asked directly", True)
check("and still nothing was deleted", (foreign / "tax return.pdf").is_file())
# An EMPTY folder is fine — there is nothing there to lose.
empty = Path(tempfile.mkdtemp()) / "Empty"
empty.mkdir()
check("an empty folder is allowed", exporter.destination_blocked(empty) is None)
check("a folder that does not exist yet is allowed",
      exporter.destination_blocked(empty / "nested" / "deeper") is None)

print("== cancelling rolls back, leaving an empty folder ==")
fresh = Path(tempfile.mkdtemp()) / "Cancelled"
exports.update(LIB, eset.id, destination=str(fresh))
cancel_plan = exporter.plan(ad, exports.get(LIB, eset.id))


class _CancelAfter:
    """Deterministic cancellation: stop partway through the second file."""

    def __init__(self, after):
        self.n, self.after = 0, after

    def progress(self, **kw):
        pass

    @property
    def cancelled(self):
        return self.n >= self.after

    def raise_if_cancelled(self):
        self.n += 1
        if self.cancelled:
            raise JobCancelled()


try:
    exporter.run(ad, exports.get(LIB, eset.id), cancel_plan, _CancelAfter(3))
    check("it stops", False, "it did not raise")
except JobCancelled:
    check("it stops", True)
check("NO library file was written", not (fresh / "collection.nml").exists())
check("no manifest either", not (fresh / exporter.MANIFEST_NAME).exists())
# Including the file being written when the cancel landed. Cleaning up only
# COMPLETED copies leaves the in-flight one behind — the bug import had.
leftover = [p for p in fresh.rglob("*") if p.is_file()] if fresh.exists() else []
check("and no audio was left behind", leftover == [], [str(p) for p in leftover])

print("== a missing file is skipped, not fatal ==")
gone = Path(seed_items[0].destination)
gone.unlink()
partial = exporter.plan(ad, exports.get(LIB, eset.id))
check("it is reported", len([t for t in partial.tracks if t.missing]) == 1)
check("the rest still export", len(partial.exportable) == len(built.exportable) - 1)
check("and it is not blocking", partial.blocked is None)

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
