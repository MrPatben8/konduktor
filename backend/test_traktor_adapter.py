"""Tests for the generic adapter layer.

`test_save_fidelity.py` exercises the Traktor STORE and the bytes it writes;
nothing there touches the projection. Without this file the entire generic layer
— the single parse, the index refresh after each command, the cue-type
translation, capabilities — would be untested by the suite that gates saves.

Runs against a temp copy of the real collection, like the other suites.
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-adapter-appdata-")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"

import traktor_nml_utils as tnu  # noqa: E402

from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.core.adapter import LibraryAdapter, Unsupported  # noqa: E402
from konduktor.core.model import AutoHotcue  # noqa: E402
from konduktor.core.pathmap import PathMapping  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


# ---- opening: exactly one parse ---------------------------------------
print("== opening a library parses it exactly once ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)

    parses = {"n": 0}
    original_init = tnu.TraktorCollection.__init__

    def counting_init(self, *a, **kw):
        parses["n"] += 1
        return original_init(self, *a, **kw)

    tnu.TraktorCollection.__init__ = counting_init
    try:
        adapter = TraktorAdapter(work)
    finally:
        tnu.TraktorCollection.__init__ = original_init

    # The whole point of collapsing the read model into the adapter: the 14MB
    # file used to be parsed twice into two graphs that could drift apart.
    check("parsed once, not twice", parses["n"] == 1, f"{parses['n']} parses")
    check("projection is populated", len(adapter.tracks) == 8485, str(len(adapter.tracks)))
    check("primary-key index agrees with the list", len(adapter.by_key) > 8000)
    check("implements the LibraryAdapter protocol", isinstance(adapter, LibraryAdapter))

    # runtime_checkable only verifies method PRESENCE, so check a signature too.
    import inspect

    sig = inspect.signature(adapter.set_cue)
    check(
        "set_cue takes the generic cue vocabulary",
        {"slot", "start_sec", "cue_type", "role"} <= set(sig.parameters),
        str(list(sig.parameters)),
    )

# ---- capabilities -------------------------------------------------------
print("== capabilities describe Traktor, not an assumed floor ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    adapter = TraktorAdapter(work)
    caps = adapter.capabilities()
    check("platform reported", caps.platform == "traktor")
    check("the library is writable, with no read-only cause",
          caps.writable is True and caps.readonly_cause is None)
    check("version read from the NML header", caps.version == "20", str(caps.version))
    check("8 hotcue slots", caps.cues.hotcue_slots == 8)
    check("no memory cues on Traktor", caps.cues.memory_cues is False)
    check("loops are a cue type, not a separate bank", caps.cues.loops == "cue_type")
    check("flexible beatgrids supported", caps.grid.flexible is True)
    check("grid is lockable", caps.grid.lockable is True)
    check("save facts present for the UI to compose copy from",
          caps.save.app_name == "Traktor" and caps.save.overwrite_risk == "on_exit")

# ---- commands refresh the projection ------------------------------------
print("== every command refreshes the read projection ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    adapter = TraktorAdapter(work)

    # metadata
    tid = next(t.id for t in adapter.tracks if t.title)
    adapter.set_track_metadata(tid, {"genre": "Konduktor Test Genre"})
    check("metadata edit is visible in the projection",
          adapter.track(tid).genre == "Konduktor Test Genre")

    # cues — a free slot on a track with a grid, so the companion guard is clear
    tid2 = next(
        t.id for t in adapter.tracks
        if t.grid_marker_count and (cues := adapter.track_cues(t.id))
        and len({c.slot for c in cues.cues if c.slot is not None}) < 7
    )
    before = adapter.track(tid2).hotcue_count
    used = {c.slot for c in adapter.track_cues(tid2).cues if c.slot is not None}
    free = next(s for s in range(8) if s not in used)
    fresh = adapter.set_cue(tid2, slot=free, start_sec=12.5, cue_type="cue")
    check("set_cue returns fresh cues", any(c.slot == free for c in fresh.cues))
    check("hotcue count in the projection incremented",
          adapter.track(tid2).hotcue_count == before + 1)

    # cue type translation, both directions
    adapter.set_cue_type(tid2, free, "load")
    check("cue type round-trips through the generic vocabulary",
          next(c.type for c in adapter.track_cues(tid2).cues if c.slot == free) == "load")
    try:
        adapter.set_cue(tid2, slot=free, start_sec=1.0, cue_type="cue", role="memory")
        check("memory cues are refused on Traktor", False, "no error raised")
    except Unsupported:
        check("memory cues are refused on Traktor", True)

    # grid
    grid_before = len(adapter.track_cues(tid2).grid_markers)
    adapter.add_grid_marker(tid2, adapter.track_cues(tid2).grid_markers[0].start + 40.0)
    check("grid marker added and projected",
          len(adapter.track_cues(tid2).grid_markers) == grid_before + 1)
    check("marker count reached the track projection",
          adapter.track(tid2).grid_marker_count == grid_before + 1)

# ---- analysed grid vs plain replace --------------------------------------
print("== set_analysed_grid writes Traktor's own shape, replace_grid does not ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    adapter = TraktorAdapter(work)
    tid = next(t.id for t in adapter.tracks if t.grid_marker_count)

    cues = adapter.replace_grid(tid, [(1.0, 128.0)])
    check("replace_grid sets exactly the markers given", len(cues.grid_markers) == 1)
    check("replace_grid creates no companion cue", cues.grid_markers[0].companion is None)

    cues = adapter.set_analysed_grid(tid, [(1.0, 128.0)])
    check("set_analysed_grid pairs the marker with a companion",
          cues.grid_markers[0].companion is not None)

# ---- remap rebuilds the index (track ids change) --------------------------
print("== a path remap rebuilds the projection, because track ids change ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    adapter = TraktorAdapter(work)

    # Use the adapter's own suggestion: the remap matches RESOLVED OS paths,
    # which is not the same string as Track.filepath (a display path).
    prefix = adapter.path_prefix_suggestions()["primary"]
    matched = {
        t.id
        for t in adapter.tracks
        if (ap := adapter.audio_path(t.id)) and str(ap).startswith(prefix)
    }
    old_id = next(iter(matched))
    moved = adapter.remap_locations(PathMapping.make(prefix, "/Volumes/KonduktorTest"))
    n = len(moved)
    check("remap rewrote some locations", n > 0, str(n))
    check("the old track id is gone from the index", adapter.track(old_id) is None)
    # Export sets follow ids through a remap with exactly this mapping.
    check("it reports where each track id went",
          old_id in moved and adapter.track(moved[old_id]) is not None)
    check("the index still holds every track", len(adapter.tracks) == 8485)

# ---- place_cues takes the generic spec ------------------------------------
print("== batch cue placement takes a typed spec ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    adapter = TraktorAdapter(work)
    tid = next(
        t.id for t in adapter.tracks
        if len({c.slot for c in adapter.track_cues(t.id).cues if c.slot is not None}) < 6
    )
    used = {c.slot for c in adapter.track_cues(tid).cues if c.slot is not None}
    free = [s for s in range(8) if s not in used][:2]
    adapter.place_cues(tid, [AutoHotcue(slot=free[0], start=30.0, name="Drop")])
    placed = next((c for c in adapter.track_cues(tid).cues if c.slot == free[0]), None)
    check("batch-placed cue exists with its name", placed is not None and placed.name == "Drop")

print("== the counts in the table match the deck ==")
# "Is this a cue?" is asked in two places — `to_track` for the library table and
# `to_track_cues` for the deck — and they must give the same answer. They did
# not: `to_track` counted the raw CUE_V2 list, which includes GRID MARKERS, so
# every gridded track reported one cue too many and the "has cues: no" filter
# could never match one. Checked across many tracks, not one, because the bug
# only shows on a track that has a grid.
mismatched = []
for t in adapter.tracks[:150]:
    tc = adapter.track_cues(t.id)
    if tc is None:
        continue
    if t.cue_count != len(tc.cues) or t.grid_marker_count != len(tc.grid_markers):
        mismatched.append(
            f"{t.title!r}: table {t.cue_count}/{t.grid_marker_count} "
            f"vs deck {len(tc.cues)}/{len(tc.grid_markers)}"
        )
check("cue and marker counts agree with the projected cues",
      not mismatched, "; ".join(mismatched[:3]))
# The specific confusion, stated directly: a grid marker is not a cue.
gridded = next((t for t in adapter.tracks if t.grid_marker_count > 0), None)
check("a track with a grid exists to test against", gridded is not None)
if gridded is not None:
    cues = adapter.track_cues(gridded.id)
    check("no grid marker is projected as a cue",
          all(c.type != "grid" for c in cues.cues) and
          gridded.cue_count == len(cues.cues))

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
