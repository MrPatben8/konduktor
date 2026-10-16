"""Cue and beatgrid edits on a OneLibrary stick: the ANLZ files.

Two kinds of check, on temp copies of two real rekordbox-written drives:

  * **against rekordbox's own edit** (`fixtures/onelibrary-goober/after/`): the
    same edit made in rekordbox 7 and in Konduktor must produce the same bytes —
    a recolour changes only the colour bytes, a grid edit blanks `PQT2` and
    clears the loop beat lengths exactly as rekordbox did;
  * **fidelity**: nothing reaches the drive before Save, only the tags an edit
    touches change (in place, order kept), a pad keeps its colour and name
    through a move, memory cues stay uneditable, and an `.EXT` with no `PCO2`
    keeps every pad when the first edit creates one.
"""
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
APPDATA = Path(tempfile.mkdtemp(prefix="konduktor-olanlz-appdata-"))
os.environ["KONDUKTOR_DATA_DIR"] = str(APPDATA)

from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.adapters.rekordbox import anlz_file as A  # noqa: E402
from konduktor.adapters.rekordbox import anlz_writer as W  # noqa: E402
from konduktor.core.adapter import InvalidCommand, NotFound, Unsupported  # noqa: E402
from konduktor.core.model import CuePoint, GridMarker  # noqa: E402

HERE = Path(__file__).resolve().parent
GOOBER = HERE / "fixtures" / "onelibrary-goober"
DEMO = HERE / "fixtures" / "onelibrary"
TRACK_1 = "/Contents/Loopmasters/UnknownAlbum/Demo Track 1.mp3"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def copy_of(fixture: Path, name: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="konduktor-olanlz-")) / name
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns("after", "rekordbox-edited", "README.md"))
    return root


def tag(path: Path, kind: str, list_kind: int | None = None) -> bytes | None:
    f = A.parse_file(path)
    return next((t.data for t in f.tags
                 if t.type == kind and (list_kind is None or t.list_kind == list_kind)), None)


def tags_by_key(source) -> dict:
    """Tags keyed by (type, list kind), from a path or from bytes."""
    out = {}
    parsed = A.parse(source) if isinstance(source, bytes) else A.parse_file(source)
    for t in parsed.tags:
        key = (t.type, t.list_kind)
        out[key if key not in out else (*key, len(out))] = t.data
    return out


def dat_of(adapter, track_id: str) -> Path:
    return adapter._store.layout.resolve(adapter._store.content(track_id).analysisDataFilePath)


# ---- against rekordbox's own edits -----------------------------------------------
print("== the same edit as rekordbox 7 made, the same bytes ==")
drive = copy_of(GOOBER, "Goober")
a = OneLibraryAdapter(drive)
by = lambda prefix: next(t for t in a.tracks if t.title.startswith(prefix))  # noqa: E731
black_bloc, wtf, demo2 = by("Justin Hawkes"), by("Result"), by("Demo Track 2")

before_dat = dat_of(a, black_bloc.id).read_bytes()
a.set_cue_color(black_bloc.id, 2, "#FF0045")
a.set_grid_marker_bpm(wtf.id, 0, 87.0)
a.set_cue(demo2.id, slot=4, start_sec=47.5, cue_type="cue")
a.save()

ext = dat_of(a, black_bloc.id).with_suffix(".EXT")
check("a recolour: the PCO2 hot list is rekordbox's, byte for byte",
      tag(ext, "PCO2", A.LIST_HOT) == tag(GOOBER / "after" / "black-bloc-recoloured.EXT", "PCO2", A.LIST_HOT))
check("and the .DAT, which holds no colour, is untouched", dat_of(a, black_bloc.id).read_bytes() == before_dat)

ext = dat_of(a, wtf.id).with_suffix(".EXT")
check("a grid edit blanks PQT2 exactly as rekordbox does",
      tag(ext, "PQT2") == tag(GOOBER / "after" / "wtf-87bpm.EXT", "PQT2"))
check("and clears the memory loop's beat length exactly as rekordbox does",
      tag(ext, "PCO2", A.LIST_MEMORY) == tag(GOOBER / "after" / "wtf-87bpm.EXT", "PCO2", A.LIST_MEMORY))
ours = [e.time for t in A.parse_file(dat_of(a, wtf.id)).tags if t.type == "PQTZ" for e in t.entries]
theirs = [e.time for t in A.parse_file(GOOBER / "after" / "wtf-87bpm.DAT").tags if t.type == "PQTZ"
          for e in t.entries]
check("PQTZ: the same number of beats as rekordbox's", len(ours) == len(theirs), f"{len(ours)} vs {len(theirs)}")
# rekordbox re-derives its anchor and works at its own tempo precision (its first
# beat moved 83 -> 89 ms, steps alternate 689/690); Konduktor keeps the marker.
check("each within 10 ms of rekordbox's (its anchor moves; ours stays put)",
      max(abs(x - y) for x, y in zip(ours, theirs)) <= 10)
check("from the marker where it was, 60/87 s apart",
      ours[0] == 83 and all(abs(ours[k] - 83 - k * 60000 / 87) <= 0.5 for k in range(len(ours))))
check("bpmx100 follows the first marker, as rekordbox's did", a._store.content(wtf.id).bpmx100 == 8700)

pads = {c.slot: c for c in a.track_cues(demo2.id).cues if c.role == "hotcue"}
theirs = {e.hot_cue - 1: e for t in A.parse_file(GOOBER / "after" / "demo2-pad-e-added.EXT").tags
          if t.type == "PCO2" and t.list_kind == A.LIST_HOT for e in t.entries}
check("a new pad lands where rekordbox's did, the other pads as they were",
      sorted(pads) == sorted(theirs) and all(abs(pads[s].start * 1000 - theirs[s].time) < 30
                                             or s == 4 for s in pads), str(sorted(pads)))
a.close()

# ---- fidelity on the demo drive --------------------------------------------------
print("== nothing reaches the drive before Save; only touched tags change ==")
drive = copy_of(DEMO, "Dingus")
a = OneLibraryAdapter(drive)
dat = dat_of(a, TRACK_1)
ext = dat.with_suffix(".EXT")
original = {p: p.read_bytes() for p in (dat, ext)}
cues = {c.slot: c for c in a.track_cues(TRACK_1).cues if c.role == "hotcue"}
check("hot cues and memory cues are editable on a drive opened for editing",
      all(c.editable for c in a.track_cues(TRACK_1).cues))

a.set_cue(TRACK_1, slot=5, start_sec=10.0, cue_type="cue", name="Drop")
a.set_cue_color(TRACK_1, 5, "#FF0045")
a.set_cue(TRACK_1, slot=5, start_sec=11.0, cue_type="cue")
pad = next(c for c in a.track_cues(TRACK_1).cues if c.slot == 5)
check("a moved pad keeps its colour and its name",
      (round(pad.start, 3), pad.color, pad.name) == (11.0, "#FF0045", "Drop"), str(pad))
check("the edit shows at once", a.dirty and round(pad.start, 3) == 11.0)
check("but the drive's files are untouched", all(p.read_bytes() == b for p, b in original.items()))

a.set_cue_type(TRACK_1, 0, "cue")  # pad A was a loop
check("a loop retyped to a cue loses its length",
      next(c for c in a.track_cues(TRACK_1).cues if c.slot == 0).length == 0.0)
check("a cue cannot become a loop without a length",
      _raises(lambda: a.set_cue_type(TRACK_1, 5, "loop"), InvalidCommand))
a.delete_cue(TRACK_1, 3)
check("a pad can be deleted (pad D, which lives in the .EXT's PCOB)",
      3 not in {c.slot for c in a.track_cues(TRACK_1).cues if c.role == "hotcue"})
check("deleting an empty pad is NotFound", _raises(lambda: a.delete_cue(TRACK_1, 7), NotFound))
check("a pad beyond the bank is refused",
      _raises(lambda: a.set_cue(TRACK_1, slot=8, start_sec=1.0, cue_type="cue"), InvalidCommand))
check("set_cue refuses role=memory (memory cues have their own commands)",
      _raises(lambda: a.set_cue(TRACK_1, slot=0, start_sec=1.0, cue_type="cue", role="memory"), InvalidCommand))
check("a colour must be one of rekordbox's swatches",
      _raises(lambda: a.set_cue_color(TRACK_1, 5, "#123456"), InvalidCommand))
a.place_cues(TRACK_1, [CuePoint(slot=1, start=30.0, length=0.0, type="cue"),
                       CuePoint(slot=6, start=31.0, length=0.0, type="cue")])
starts = {c.slot: round(c.start, 2) for c in a.track_cues(TRACK_1).cues if c.role == "hotcue"}
check("place_cues fills empty pads and leaves occupied ones", starts.get(6) == 31.0 and starts.get(1) != 30.0)

markers = a.track_cues(TRACK_1).grid_markers
# A per-beat grid has no markers: one at the governing tempo, on its beats, is
# indistinguishable from none and collapses on read (as on the Rekordbox side).
a.add_grid_marker(TRACK_1, markers[-1].start + 10 * 60 / markers[-1].bpm)
check("a marker at the governing tempo collapses into it",
      len(a.track_cues(TRACK_1).grid_markers) == len(markers))
a.add_grid_marker(TRACK_1, 100.0, 100.0)
check("a marker with a new tempo is a new segment",
      [m.bpm for m in a.track_cues(TRACK_1).grid_markers] == [128.0, 90.0, 100.0])
a.move_grid_marker(TRACK_1, 1, 200.0)
moved = a.track_cues(TRACK_1).grid_markers
check("a moved marker is clamped before its neighbour", moved[1].start < moved[2].start)
a.delete_grid_marker(TRACK_1, 2)
check("and deleted", len(a.track_cues(TRACK_1).grid_markers) == len(markers))

a.save()
after = {p: tags_by_key(p) for p in (dat, ext)}
before = {p: tags_by_key(b) for p, b in original.items()}
changed = {p.suffix: sorted(f"{k[0]}/{k[1]}" for k in before[p] if before[p][k] != after[p].get(k))
           for p in original}
check("the .DAT: only the pad 1-3 list and the grid changed",
      changed[".DAT"] == ["PCOB/1", "PQTZ/None"], str(changed[".DAT"]))
check("the .EXT: only its cue lists changed (memory: the loop beats a grid edit clears)",
      set(changed[".EXT"]) <= {"PCOB/1", "PCO2/1", "PCO2/0"}, str(changed[".EXT"]))
check("tag order is kept in both files",
      all([t.type for t in A.parse(original[p]).tags] == [t.type for t in A.parse_file(p).tags]
          for p in original))
check("the .DAT's memory list (no beat field there) is byte-identical",
      tag(dat, "PCOB", A.LIST_MEMORY) == before[dat][("PCOB", A.LIST_MEMORY)])
backups = sorted((APPDATA / "onelibrary" / "backups" / "Dingus").iterdir())
check("both analysis files were backed up first",
      all((backups[-1] / p.relative_to(drive)).read_bytes() == b for p, b in original.items()))
a.close()
b = OneLibraryAdapter(drive)
check("everything reads back after a reopen",
      {c.slot: round(c.start, 2) for c in b.track_cues(TRACK_1).cues if c.role == "hotcue"} == starts
      and next(c for c in b.track_cues(TRACK_1).cues if c.slot == 5).name == "Drop")
b.close()

print("== another app wrote an analysis file since Konduktor read it ==")
drive = copy_of(DEMO, "Dingus")
a = OneLibraryAdapter(drive)
a.set_cue(TRACK_1, slot=6, start_sec=5.0, cue_type="cue")
time.sleep(0.01)
os.utime(dat_of(a, TRACK_1).with_suffix(".EXT"))
check("Save refuses rather than undo it", _raises(a.save, InvalidCommand) and a.dirty)
a.close()

print("== an .EXT with no PCO2 keeps every pad on the first edit ==")
drive = copy_of(DEMO, "Dingus")
probe = OneLibraryAdapter(drive)
ext = dat_of(probe, TRACK_1).with_suffix(".EXT")
want = sorted(c.slot for c in probe.track_cues(TRACK_1).cues if c.role == "hotcue")
memory = sum(c.role == "memory" for c in probe.track_cues(TRACK_1).cues)
probe.close()
f = A.parse_file(ext)
f.tags = [t for t in f.tags if t.type != "PCO2"]
ext.write_bytes(f.to_bytes())
a = OneLibraryAdapter(drive)
check("read from PCOB alone, the pads are all there", sorted(
    c.slot for c in a.track_cues(TRACK_1).cues if c.role == "hotcue") == want)
a.set_cue(TRACK_1, slot=6, start_sec=5.0, cue_type="cue")
got = a.track_cues(TRACK_1).cues
check("after the edit creates PCO2, every old pad AND the new one read",
      sorted(c.slot for c in got if c.role == "hotcue") == sorted(want + [6]), str([c.slot for c in got]))
check("and the memory cue survives", sum(c.role == "memory" for c in got) == memory)
a.close()

print("== a track without analysis files ==")
drive = copy_of(DEMO, "Dingus")
probe = OneLibraryAdapter(drive)
dat = dat_of(probe, TRACK_1)
probe.close()
for p in (dat, dat.with_suffix(".EXT")):
    p.unlink()
a = OneLibraryAdapter(drive)
check("refuses a cue edit (it has nowhere to store one)",
      _raises(lambda: a.set_cue(TRACK_1, slot=0, start_sec=1.0, cue_type="cue"), InvalidCommand))
check("and a grid edit", _raises(lambda: a.replace_grid(TRACK_1, [GridMarker(start=0, bpm=120)]),
                                 InvalidCommand))
a.close()

print("== the entry helpers ==")
e = W.pcp2_entry(hot_cue=2, kind=1, time_ms=5, loop_ms=None, rgb=(1, 2, 3), beats=None, code=9,
                 comment="A name longer than forty bytes in UTF-16")
d = A.decode_entry(e)
check("a long name grows the entry past 88 bytes and round-trips",
      len(e) > 88 and d.comment == "A name longer than forty bytes in UTF-16" and d.color_code == 9)
check("a 44-byte compact entry has no colour bytes to patch",
      A.entry_with_colour(A.raw_entries(A.Tag("PCO2", tag(HERE / "fixtures" / "onelibrary" / "rekordbox-edited"
                                                       / "nemean.EXT", "PCO2", A.LIST_MEMORY)))[-1],
                          9, (1, 2, 3)) is None)

# ---- memory cues: rekordbox's own edits, byte for byte --------------------------
# `fixtures/onelibrary-memory-edits`: six memory-cue edits rekordbox 7 made on a stick
# (kit M2, 2026-10-16), one per track. Each is replayed with `memory_cues` — the
# code the store runs — on `before/`, and the memory lists must equal `after/`.
print("== memory cues: the same edit as rekordbox 7 made, the same bytes ==")
from konduktor.adapters.onelibrary import memory_cues as M  # noqa: E402
from konduktor.adapters.onelibrary.cues import memory_cue_id  # noqa: E402

MEMORY_EDITS = HERE / "fixtures" / "onelibrary-memory-edits"


def memory_edit(name: str):
    return (A.parse_file(MEMORY_EDITS / "before" / f"{name}.DAT"),
            A.parse_file(MEMORY_EDITS / "before" / f"{name}.EXT"))


def same_as_rekordbox(name: str, dat, ext) -> bool:
    """Both files equal rekordbox's, tag for tag — except that when rekordbox
    rewrites a `.DAT` it also re-sorts the UNTOUCHED hot-cue `PCOB` into
    ascending pad order (measured: same entries, pads 3-2-1 -> 1-2-3).
    Konduktor changes only the list it edits, so that one tag must hold the
    same entries in either order."""
    want_dat = A.parse_file(MEMORY_EDITS / "after" / f"{name}.DAT")
    want_ext = A.parse_file(MEMORY_EDITS / "after" / f"{name}.EXT")
    if ext.to_bytes() != want_ext.to_bytes() or len(dat.tags) != len(want_dat.tags):
        return False
    for got, want in zip(dat.tags, want_dat.tags):
        if (got.type, got.list_kind) == ("PCOB", A.LIST_HOT):
            if sorted(A.raw_entries(got)) != sorted(A.raw_entries(want)):
                return False
        elif got.data != want.data:
            return False
    return dat.header[:8] == want_dat.header[:8] and len(dat.to_bytes()) == len(want_dat.to_bytes())


def id_at(ext, time_ms: int) -> str:
    return next(memory_cue_id(A.decode_entry(e)) for e in M.entries(ext, "PCO2")
                if A.decode_entry(e).time == time_ms)


dat, ext = memory_edit("black-bloc")
M.add(dat, ext, kind=1, time_ms=131206, loop_ms=None, beats=None)
check("add a memory cue: .DAT prepended + relinked, .EXT appended compact — both FILES "
      "equal rekordbox's", same_as_rekordbox("black-bloc", dat, ext))

dat, ext = memory_edit("formula")
M.add(dat, ext, kind=2, time_ms=80378, loop_ms=81773, beats=4)
check("add a memory loop (4 beats): rekordbox's bytes", same_as_rekordbox("formula", dat, ext))

dat, ext = memory_edit("lift-it")
for t in (3353, 4733, 6112, 7491, 8871, 10250, 11629, 13009):
    M.add(dat, ext, kind=1, time_ms=t, loop_ms=None, beats=None)
check("add eight, up to rekordbox's cap of 10: rekordbox's bytes",
      same_as_rekordbox("lift-it", dat, ext) and M.count(ext) == 10)

dat, ext = memory_edit("wtf")
check("recolour Blue: only PCP2 byte 28 (= 7), rekordbox's bytes",
      M.recolour(ext, id_at(ext, 113186), 7) and same_as_rekordbox("wtf", dat, ext))

dat, ext = memory_edit("sell-my-soul")
check("a comment grows the PCP2 entry by its length (88 -> 100): rekordbox's bytes",
      M.rename(ext, id_at(ext, 78645), "Loopy") and same_as_rekordbox("sell-my-soul", dat, ext))

dat, ext = memory_edit("doomed")
check("delete: out of both lists, the survivor relinked: rekordbox's bytes",
      M.delete(dat, ext, id_at(ext, 121402)) and same_as_rekordbox("doomed", dat, ext))
check("an unknown id finds nothing", not M.delete(dat, ext, "m:1:1:4294967295"))

print("== memory cues through the adapter, on the demo drive ==")
from konduktor.adapters.rekordbox import memory_palette  # noqa: E402

drive = copy_of(DEMO, "Memo")
a = OneLibraryAdapter(drive)
dat = dat_of(a, TRACK_1)
ext = dat.with_suffix(".EXT")
original = {p: p.read_bytes() for p in (dat, ext)}
caps = a.capabilities().cues
check("capabilities: memory cues, cap 10, the 8-colour palette",
      caps.memory_cues and caps.max_memory_cues == 10 and caps.memory_color == "palette"
      and caps.memory_palette == memory_palette.SWATCHES)
before_mem = [c for c in a.track_cues(TRACK_1).cues if c.role == "memory"]
tc = a.add_memory_cue(TRACK_1, start_sec=12.0, cue_type="cue", name="Break",
                      color=memory_palette.SWATCHES[1])
new = [c for c in tc.cues if c.role == "memory" and c not in before_mem]
check("add_memory_cue: named, coloured (Red), editable, with an id",
      len(new) == 1 and new[0].name == "Break" and new[0].color == memory_palette.SWATCHES[1]
      and new[0].editable and new[0].id, str(new))
check("nothing reaches the drive before Save", all(p.read_bytes() == b for p, b in original.items()))
tc = a.rename_memory_cue(TRACK_1, new[0].id, "Breakdown")
tc = a.set_memory_cue_color(TRACK_1, new[0].id, None)
got = next(c for c in tc.cues if c.id == new[0].id)
check("rename and clear the colour", got.name == "Breakdown" and got.color is None, str(got))
check("a hot-cue swatch is not a memory colour",
      _raises(lambda: a.set_memory_cue_color(TRACK_1, new[0].id, "#00FF30"), InvalidCommand))
check("an unknown id is NotFound",
      _raises(lambda: a.delete_memory_cue(TRACK_1, "m:1:1:4294967295"), NotFound))
while sum(c.role == "memory" for c in a.track_cues(TRACK_1).cues) < 10:
    n = sum(c.role == "memory" for c in a.track_cues(TRACK_1).cues)
    a.add_memory_cue(TRACK_1, start_sec=100.0 + n, cue_type="cue")
check("the 11th is refused", _raises(lambda: a.add_memory_cue(TRACK_1, start_sec=1.0,
                                                               cue_type="cue"), InvalidCommand))
a.delete_memory_cue(TRACK_1, new[0].id)
check("delete removes it", all(c.id != new[0].id for c in a.track_cues(TRACK_1).cues))
hot_before = {c.slot: (c.start, c.color) for c in a.track_cues(TRACK_1).cues if c.role == "hotcue"}
a.save()
def changed_tags(path: Path) -> set:
    now, was = tags_by_key(path), tags_by_key(original[path])
    return {k for k in set(now) | set(was) if now.get(k) != was.get(k)}


check("Save: the .DAT changes only in its memory PCOB",
      changed_tags(dat) == {("PCOB", A.LIST_MEMORY)}, str(changed_tags(dat)))
check("the .EXT only in its memory PCO2",
      changed_tags(ext) == {("PCO2", A.LIST_MEMORY)}, str(changed_tags(ext)))
b = OneLibraryAdapter(drive)
check("reopened: the hot cues are untouched and 9 memory cues remain",
      {c.slot: (c.start, c.color) for c in b.track_cues(TRACK_1).cues if c.role == "hotcue"}
      == hot_before and sum(c.role == "memory" for c in b.track_cues(TRACK_1).cues) == 9)
b.close()
a.close()

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
