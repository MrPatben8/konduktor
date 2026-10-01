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
import json
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
# All three platforms Konduktor can OPEN can now also be written from nothing.
# The two registries stay separate anyway: they answer different questions, and
# a future read-only platform would be in one and not the other.
check("so are rekordbox and onelibrary",
      core_export.for_platform("rekordbox") is not None
      and core_export.for_platform("onelibrary") is not None)
check("a platform with no exporter is not a target",
      core_export.for_platform("serato") is None)
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
result = exporter.write(payload, dest)
written = result.library
check("the exporter reports what it wrote", result.all_paths == [written])
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
again = exporter.write(payload, dest).library
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
eset = exports.create(LIB, name="GIG", targets=["traktor"], destination=str(dest))
source_pl = next(n for n in ad.playlist_tree()[0].children if n.kind == "playlist")
exports.add(LIB, eset.id, playlist_ids=[source_pl.id], track_ids=[ad.tracks[3].id])
eset = exports.get(LIB, eset.id)


def run_now(plan):
    job = JOBS.submit("export", lambda h: exporter.run(ad, eset, plan, h))
    while not job.finished:
        time.sleep(0.02)
    return job


print("== the plan mirrors the source tree under Contents/, relative to its shared root ==")
built = exporter.plan(ad, eset)
check("nothing is blocking it", built.blocked is None, str(built.blocked))
check("every track has somewhere to go", all(t.destination for t in built.exportable))
rel = [str(t.destination.relative_to(dest)) for t in built.exportable]
# Under Contents/, so the audio can never land on top of a library's own path.
check("all audio lives under Contents/",
      all(r.startswith(exporter.AUDIO_DIR + "/") for r in rel), rel)
# Mirroring ABSOLUTE paths would put the user's home directory on the stick.
check("the user's own folders are preserved",
      all(r.startswith(("Contents/House/", "Contents/Techno/")) for r in rel), rel)
check("a Traktor-only export never warns about a drive root", built.not_drive_root == [])
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
# A stick's root after its first mount on a Mac: not empty to iterdir(), but
# nothing in it is the user's.
stick = Path(tempfile.mkdtemp()) / "STICK"
for d in (".Spotlight-V100", ".fseventsd", ".Trashes", "System Volume Information"):
    (stick / d).mkdir(parents=True)
for f in (".DS_Store", "._Track.mp3", "Thumbs.db"):
    (stick / f).write_bytes(b"")
check("OS housekeeping does not make a folder occupied",
      exporter.destination_blocked(stick) is None)
(stick / "My Set.mp3").write_bytes(b"")
check("but one file of the user's still does",
      exporter.destination_blocked(stick) == exporter.BLOCKED_OCCUPIED)

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

print("== several targets share ONE copy of the audio ==")
from konduktor.adapters.onelibrary.driver import OneLibraryDriver  # noqa: E402

multi = Path(tempfile.mkdtemp()) / "STICK"
exports.update(LIB, eset.id, destination=str(multi), targets=["traktor", "onelibrary"])
eset = exports.get(LIB, eset.id)
check("the set keeps both targets, in order", eset.targets == ["traktor", "onelibrary"], eset.targets)
mplan = exporter.plan(ad, eset)
check("nothing is blocking it", mplan.blocked is None, str(mplan.blocked))
# A temp folder is not a drive's root, and only OneLibrary cares.
check("only the drive-root target is warned about", mplan.not_drive_root == ["onelibrary"],
      mplan.not_drive_root)
mjob = run_now(mplan)
check("it finishes", mjob.state == "done", f"{mjob.state}: {mjob.error}")
check("the result names both libraries",
      [l["platform"] for l in mjob.result["libraries"]] == ["traktor", "onelibrary"],
      mjob.result.get("libraries"))
mrel = [t.destination.relative_to(multi) for t in mplan.exportable]
audio_files = [p for p in multi.rglob("*") if p.is_file() and p.parts[len(multi.parts)] == "Contents"]
check("the audio was copied once, not once per target", len(audio_files) == len(mrel),
      f"{len(audio_files)} files for {len(mrel)} tracks")
t_lib = TraktorDriver().open(multi / "collection.nml")
o_lib = OneLibraryDriver().open(multi)
check("the Traktor collection re-opens with every track", len(t_lib.tracks) == len(mrel))
check("so does the OneLibrary drive", len(o_lib.tracks) == len(mrel))
# Both libraries must point at the SAME files — a second copy would be a bug
# in the plan, and a dangling path would be one in a writer.
check("every OneLibrary track resolves to a copied file",
      all(o_lib.audio_path(t.id) and o_lib.audio_path(t.id).is_file() for t in o_lib.tracks))
check("OneLibrary paths are drive-relative under /Contents/",
      all(t.id.startswith("/Contents/") for t in o_lib.tracks), [t.id for t in o_lib.tracks][:2])
manifest = json.loads((multi / exporter.MANIFEST_NAME).read_text())
check("the manifest lists both libraries",
      sorted(manifest["libraries"]) == sorted(["collection.nml", str(Path("PIONEER/rekordbox/exportLibrary.db"))]),
      manifest["libraries"])
del t_lib, o_lib

print("== unticking a target removes its library on the next export ==")
exports.update(LIB, eset.id, targets=["traktor"])
eset = exports.get(LIB, eset.id)
job3 = run_now(exporter.plan(ad, eset))
check("the re-export succeeds", job3.state == "done", f"{job3.state}: {job3.error}")
check("the OneLibrary library is gone", not (multi / "PIONEER").exists(),
      sorted(str(p.relative_to(multi)) for p in (multi / "PIONEER").rglob("*")) if (multi / "PIONEER").exists() else "")
check("the Traktor one and the audio remain",
      (multi / "collection.nml").is_file() and all((multi / r).is_file() for r in mrel))

print("== a manifest from a single-target export is still cleared ==")
old = Path(tempfile.mkdtemp()) / "OldExport"
old.mkdir()
(old / "collection.nml").write_text("stale")
(old / "House").mkdir()
(old / "House" / "stale.mp3").write_bytes(b"x")
(old / exporter.MANIFEST_NAME).write_text(json.dumps(
    {"library": "collection.nml", "files": ["House/stale.mp3"]}))
exports.update(LIB, eset.id, destination=str(old))
job4 = run_now(exporter.plan(ad, exports.get(LIB, eset.id)))
check("the re-export succeeds", job4.state == "done", f"{job4.state}: {job4.error}")
check("the old root-level audio was cleared", not (old / "House").exists())
check("and the new collection replaced the old one",
      (old / "collection.nml").read_text() != "stale")

print("== a failing LATER target undoes everything, earlier targets included ==")
failing = Path(tempfile.mkdtemp()) / "Failing"
exports.update(LIB, eset.id, destination=str(failing), targets=["traktor", "onelibrary"])
onelib = core_export.for_platform("onelibrary")


def _half_written(payload, destination):
    # Writes files it never gets to REPORT, then fails — the case the rollback's
    # snapshot exists for.
    stray = Path(destination) / "PIONEER" / "USBANLZ" / "P000" / "0000" / "ANLZ0000.DAT"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"partial")
    raise RuntimeError("disk full")


eset = exports.get(LIB, eset.id)   # run_now() runs the module-level set
onelib.write = _half_written
try:
    job5 = run_now(exporter.plan(ad, eset))
finally:
    del onelib.write
check("the export fails", job5.state == "failed", job5.state)
left = sorted(str(p.relative_to(failing)) for p in failing.rglob("*")) if failing.exists() else []
check("nothing at all is left behind — no audio, no Traktor library, no stray analysis",
      left == [], left)
exports.update(LIB, eset.id, destination=str(dest), targets=["traktor"])
eset = exports.get(LIB, eset.id)

print("== a re-export copies only what changed ==")
from konduktor.core import analysis_cache, waveform  # noqa: E402

calls = {"copy": 0, "analyse": 0}
_real_copy, _real_analyse = exporter.copy_file, waveform.analyse


def _counting_copy(*a, **kw):
    calls["copy"] += 1
    return _real_copy(*a, **kw)


def _counting_analyse(*a, **kw):
    calls["analyse"] += 1
    return _real_analyse(*a, **kw)


exporter.copy_file, waveform.analyse = _counting_copy, _counting_analyse


def run_set(s, plan, handle=None):
    if handle is not None:
        return exporter.run(ad, s, plan, handle)
    job = JOBS.submit("export", lambda h: exporter.run(ad, s, plan, h))
    while not job.finished:
        time.sleep(0.02)
    return job


def reset():
    calls["copy"] = calls["analyse"] = 0


def inodes(root):
    return {str(p.relative_to(root)): p.stat().st_ino
            for p in (root / exporter.AUDIO_DIR).rglob("*") if p.is_file()}


inc = Path(tempfile.mkdtemp()) / "INC"
iset = exports.create(LIB, name="INC", targets=["traktor", "onelibrary"], destination=str(inc))
exports.add(LIB, iset.id, track_ids=[t.id for t in ad.tracks[:4]])
iset = exports.get(LIB, iset.id)

first = exporter.plan(ad, iset)
check("a first export reuses nothing", first.unchanged == 0 and first.total_bytes == 4 * 300_000,
      (first.unchanged, first.total_bytes))
reset()
j = run_set(iset, first)
check("it finishes", j.state == "done", f"{j.state}: {j.error}")
check("every file was copied and measured once", calls == {"copy": 4, "analyse": 4}, calls)
manifest = json.loads((inc / exporter.MANIFEST_NAME).read_text())
check("the manifest records each copy's source",
      sorted(r["source"] for r in manifest["audio"].values())
      == sorted(str(ad.audio_path(t.id)) for t in ad.tracks[:4]))
check("no temporary files are left", not any(
    p.name.endswith(exporter.PARTIAL_SUFFIX) for p in inc.rglob("*")))
before_ino = inodes(inc)

again = exporter.plan(ad, iset)
check("nothing changed, so nothing is to be copied",
      again.unchanged == 4 and again.total_bytes == 0 and again.as_dict()["unchanged"] == 4,
      (again.unchanged, again.total_bytes))
reset()
j = run_set(iset, again)
check("the re-export finishes", j.state == "done", f"{j.state}: {j.error}")
check("no audio was copied and no track decoded", calls == {"copy": 0, "analyse": 0}, calls)
check("the kept files are the very same files", inodes(inc) == before_ino)
check("both libraries were still rewritten and re-open with every track",
      len(TraktorDriver().open(inc / "collection.nml").tracks) == 4
      and len(OneLibraryDriver().open(inc).tracks) == 4)



class _Recorder:
    """A handle that keeps every progress report."""

    def __init__(self):
        self.reports = []

    def progress(self, **kw):
        self.reports.append(kw)

    cancelled = False

    def raise_if_cancelled(self):
        pass


rec = _Recorder()
run_set(iset, exporter.plan(ad, iset), rec)
track_steps = [(r["done"], r["total"]) for r in rec.reports
               if r.get("unit") == "tracks" and r.get("total")]
# The bug: the bar counted only bytes, so a re-export that copied nothing sat
# on an indeterminate bar through the whole (slow) library write.
check("with nothing to copy, the bar still moves — per track, while writing",
      track_steps and track_steps[-1] == (3, 4) and len(track_steps) >= 4, track_steps)

print("== an edited source is copied again, and only it ==")
edited = ad.audio_path(ad.tracks[0].id)
edited.write_bytes(b"\1" * 310_000)   # what a tag write does: new bytes, new mtime
plan3 = exporter.plan(ad, iset)
check("one track is to be copied", plan3.unchanged == 3 and plan3.total_bytes == 310_000,
      (plan3.unchanged, plan3.total_bytes))
reset()
j = run_set(iset, plan3)
check("it finishes", j.state == "done", f"{j.state}: {j.error}")
check("one file copied, one decoded", calls == {"copy": 1, "analyse": 1}, calls)
edited_copy = next(Path(t.destination) for t in plan3.exportable if t.source_path == edited)
check("the copy on the stick is the new version", edited_copy.read_bytes() == b"\1" * 310_000)

print("== a copy changed ON the stick is not trusted ==")
tampered = next(Path(t.destination) for t in plan3.exportable if t.source_path != edited)
with tampered.open("ab") as f:
    f.write(b"junk")
plan4 = exporter.plan(ad, iset)
check("it alone is copied again",
      [Path(t.destination) for t in plan4.exportable if t.reuse is None] == [tampered],
      [str(t.destination) for t in plan4.exportable if t.reuse is None])
j = run_set(iset, plan4)
check("and the original is restored", j.state == "done" and tampered.stat().st_size == 300_000)

print("== a moved shared root renames kept copies instead of copying them ==")
house = [t for t in ad.tracks[:4] if "/House/" in str(ad.audio_path(t.id))]
techno = [t for t in ad.tracks[:4] if t not in house]
moved = Path(tempfile.mkdtemp()) / "MOVED"
mset = exports.create(LIB, name="MOVED", targets=["traktor"], destination=str(moved))
exports.add(LIB, mset.id, track_ids=[t.id for t in house])
mset = exports.get(LIB, mset.id)
run_set(mset, exporter.plan(ad, mset))
old_paths = inodes(moved)
check("with only House tracks, the root is House itself",
      all(not r.startswith("Contents/House/") for r in old_paths), sorted(old_paths))
exports.add(LIB, mset.id, track_ids=[techno[0].id])
mset = exports.get(LIB, mset.id)
mplan = exporter.plan(ad, mset)
check("adding a Techno track moves every path, yet the House copies are reused",
      mplan.unchanged == len(house)
      and mplan.total_bytes == ad.audio_path(techno[0].id).stat().st_size,
      (mplan.unchanged, mplan.total_bytes))
reset()
j = run_set(mset, mplan)
check("it finishes", j.state == "done", f"{j.state}: {j.error}")
check("only the new track was copied", calls["copy"] == 1, calls)
new_paths = inodes(moved)
check("the kept copies now sit under House/, as the same files",
      sorted(v for r, v in new_paths.items() if r.startswith("Contents/House/"))
      == sorted(old_paths.values()), sorted(new_paths))
check("and nothing is left at the old paths or in staging",
      not any((moved / r).exists() for r in old_paths)
      and not (moved / exporter.MOVING_DIR).exists())
check("the collection points at the moved copies",
      all(TraktorDriver().open(moved / "collection.nml").audio_path(t.id).is_file()
          for t in TraktorDriver().open(moved / "collection.nml").tracks))

print("== a removed track's copy is removed ==")
exports.remove(LIB, mset.id, track_ids=[techno[0].id])
mset = exports.get(LIB, mset.id)
j = run_set(mset, exporter.plan(ad, mset))
check("it finishes", j.state == "done", f"{j.state}: {j.error}")
check("the Techno copy is gone", not any("Techno" in str(p) for p in moved.rglob("*")),
      sorted(str(p.relative_to(moved)) for p in moved.rglob("*")))

print("== cancelling a re-export keeps what it kept, and the folder stays ours ==")
edited.write_bytes(b"\2" * 320_000)
cplan = exporter.plan(ad, iset)
kept_before = {Path(t.destination) for t in cplan.exportable if t.reuse is not None}
try:
    run_set(iset, cplan, _CancelAfter(2))
    check("it stops", False, "it did not raise")
except JobCancelled:
    check("it stops", True)
check("no library was left", not (inc / "collection.nml").exists()
      and not (inc / "PIONEER" / "rekordbox" / "exportLibrary.db").exists())
check("the kept copies are still there", all(p.is_file() for p in kept_before))
check("the half-copied file is not", not (edited_copy.exists() or any(
    p.name.endswith(exporter.PARTIAL_SUFFIX) for p in inc.rglob("*"))))
retry = exporter.plan(ad, iset)
check("the folder is still ours, not refused", retry.blocked is None, str(retry.blocked))
check("and the retry still reuses the kept copies", retry.unchanged == len(kept_before),
      (retry.unchanged, len(kept_before)))
reset()
j = run_set(iset, retry)
check("the retry finishes", j.state == "done", f"{j.state}: {j.error}")
check("decoding only the changed track — the rest came from the cache", calls["analyse"] == 1, calls)

print("== the analysis cache ==")
cache_dir = inc / analysis_cache.CACHE_DIR
check("it lives on the drive", cache_dir.is_dir() and len(list(cache_dir.iterdir())) == 4,
      sorted(p.name for p in cache_dir.iterdir()) if cache_dir.exists() else "")
check("and is not in the manifest, so a re-export never clears it",
      not any(f.startswith(analysis_cache.CACHE_DIR)
              for f in json.loads((inc / exporter.MANIFEST_NAME).read_text())["files"]))
exports.remove(LIB, iset.id, track_ids=[ad.tracks[3].id])
iset = exports.get(LIB, iset.id)
run_set(iset, exporter.plan(ad, iset))
check("a removed track's entry is pruned", len(list(cache_dir.iterdir())) == 3)
# Round-trips a real measurement, not just the undecodable marker the silent
# fixture files produce.
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

tone_dir = Path(tempfile.mkdtemp())
tone = tone_dir / "tone.wav"
sf.write(tone, 0.5 * np.sin(np.linspace(0, 2000 * np.pi, 44100 * 3)), 44100)
c = analysis_cache.AnalysisCache(tone_dir / "cache")
key = analysis_cache.fingerprint(tone, tone.stat().st_size, tone.stat().st_mtime_ns)
fresh_a = c.analyse(tone, key, lead=0.025)
reset()
cached_a = c.analyse(tone, key, lead=0.025)
check("a hit does not decode", calls["analyse"] == 0, calls)
check("and returns the same measurement, exactly",
      fresh_a is not None and cached_a is not None
      and np.array_equal(fresh_a.columns.rms, cached_a.columns.rms)
      and np.array_equal(fresh_a.columns.brightness, cached_a.columns.brightness)
      and np.array_equal(fresh_a.frames.bands, cached_a.frames.bands)
      and fresh_a.duration == cached_a.duration)
c.analyse(tone, key, lead=0.0)
check("a different lead is a separate entry", calls["analyse"] == 1, calls)
next((tone_dir / "cache").glob("*-25000-400.npz")).write_bytes(b"garbage")
reset()
check("a damaged entry is a miss, not a failure",
      c.analyse(tone, key, lead=0.025) is not None and calls["analyse"] == 1)

exporter.copy_file, waveform.analyse = _real_copy, _real_analyse
exports.delete(LIB, iset.id)
exports.delete(LIB, mset.id)

print("== targets are validated and migrated ==")
check("a set saved with the old single `target` loads as a list",
      exports._targets({"target": "onelibrary"}) == ["onelibrary"])
check("an empty list falls back rather than yielding a set with no target",
      exports._targets({"targets": []}) == ["traktor"])
dup = exports.create(LIB, name="Dup", targets=["traktor", "traktor"], destination=str(dest / "x"))
check("duplicate targets collapse", dup.targets == ["traktor"], dup.targets)
exports.delete(LIB, dup.id)
bad = exports.create(LIB, name="Bad", targets=["traktor", "serato"], destination=str(dest / "y"))
check("one unsupported target blocks the whole plan",
      exporter.plan(ad, bad).blocked == exporter.BLOCKED_NO_TARGET)
exports.delete(LIB, bad.id)

print("== a missing file is skipped, not fatal ==")
gone = Path(seed_items[0].destination)
gone.unlink()
partial = exporter.plan(ad, exports.get(LIB, eset.id))
check("it is reported", len([t for t in partial.tracks if t.missing]) == 1)
check("the rest still export", len(partial.exportable) == len(built.exportable) - 1)
check("and it is not blocking", partial.blocked is None)

print("== the source is where the ADAPTER resolves it, not the display path ==")
# The bug: a collection on a drive (or a Windows collection with an X: → /Volumes
# mapping) exported "nothing", because the plan read `Track.filepath` — a display
# path without the volume or the active mapping — and called every track missing.
import shutil  # noqa: E402
from konduktor.core.pathmap import PathMapping  # noqa: E402

moved_root = Path(tempfile.mkdtemp()) / "Elsewhere"
for sub in ("House", "Techno"):
    shutil.copytree(lib_root / sub, moved_root / sub)
    shutil.rmtree(lib_root / sub)
ad.set_path_mapping(PathMapping.make(str(lib_root), str(moved_root)))
remapped = exporter.plan(ad, exports.get(LIB, eset.id))
check("every surviving track is found through the mapping",
      len(remapped.exportable) == len(partial.exportable),
      f"{len(remapped.exportable)} of {len(partial.exportable)}")
check("and it is copied from the mapped location",
      all(str(t.source_path).startswith(str(moved_root)) for t in remapped.exportable))
ad.set_path_mapping(PathMapping())

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
