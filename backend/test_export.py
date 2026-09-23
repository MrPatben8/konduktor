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

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
