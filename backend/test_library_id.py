"""Stable library identity: the thing export sets are keyed by.

The property under test is one no other suite covers: **a library that moves
keeps its identity**. Everything Konduktor persists about a library keys off its
OS path today, so moving a collection silently orphans its history repo and its
path mapping. That is tolerable for those; it is not tolerable for export sets,
which are curated user work.

Runs entirely on temp directories and a temp app-data dir — it never touches a
real library, and `KONDUKTOR_DATA_DIR` keeps the registry out of the user's own.
"""
import json
import os
import tempfile
from pathlib import Path

# Isolate the app-data dir BEFORE anything reads it.
_TMP = tempfile.TemporaryDirectory()
os.environ["KONDUKTOR_DATA_DIR"] = _TMP.name

from konduktor import library_id, paths  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def a_library(folder: Path, name: str = "collection.nml") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lib = folder / name
    lib.write_text("<NML/>")
    return lib


root = Path(tempfile.mkdtemp())

print("== an id is stable, and written beside the library ==")
lib = a_library(root / "Traktor 4.5.0")
first = library_id.for_path(lib, platform="traktor", label="collection.nml")
check("an id is issued", bool(first))
check("asking again gives the same id", library_id.for_path(lib) == first)
check("a sidecar is written", (lib.parent / library_id.SIDECAR_NAME).is_file())
# The app's own file browser skips dotfiles, so the sidecar must be one or it
# shows up in the picker as a mystery file next to the user's collection.
check("the sidecar is hidden", library_id.SIDECAR_NAME.startswith("."))
check("the id is durable", library_id.is_durable(first))
# Used as a filename by the export-set store, so it must not need escaping.
check("the id is filesystem-safe", first.replace("-", "").isalnum(), first)

print("== the whole point: it survives a move ==")
moved = root / "Somewhere Else"
lib.parent.rename(moved)
after_move = library_id.for_path(moved / "collection.nml")
check("a moved library keeps its id", after_move == first, f"{after_move} vs {first}")
check("the registry follows it to the new path",
      library_id.describe(first)["path"] == str(moved / "collection.nml"))
check("and it is still one library, not two", len(library_id.known()) == 1,
      list(library_id.known()))

print("== two libraries in ONE folder are two libraries ==")
# The flaw this found: one sidecar per folder holding a single id would hand a
# `collection.nml` and an older one kept beside it the SAME identity — and then
# show one library's export sets against the other.
second = a_library(moved, "collection-2023.nml")
other = library_id.for_path(second, platform="traktor")
check("a sibling library gets its own id", other != first, f"{other} vs {first}")
check("one sidecar holds both", len(json.loads(
    (moved / library_id.SIDECAR_NAME).read_text())["libraries"]) == 2)
check("each keeps its own on a re-read",
      library_id.for_path(moved / "collection.nml") == first
      and library_id.for_path(second) == other)

print("== separate folders are separate libraries ==")
elsewhere = a_library(root / "Traktor 3.11.0")
third = library_id.for_path(elsewhere, platform="traktor")
check("a different folder gets a different id", len({first, other, third}) == 3)

print("== a library Konduktor must not write beside ==")
# A removable drive: writing to a user's USB stick for Konduktor's own
# bookkeeping is not a trade worth making, so those get a path-derived id.
stick = root / "GIG"
stick_lib = a_library(stick)
volatile = library_id.for_path(stick_lib, platform="onelibrary", sidecar=False)
check("no sidecar is written", not (stick / library_id.SIDECAR_NAME).exists())
check("the id is reported as NOT durable", not library_id.is_durable(volatile))
check("it is still stable for a given path", library_id.for_path(stick_lib, sidecar=False) == volatile)
check("it is still registered", library_id.describe(volatile) is not None)

print("== a directory library keys on the folder itself ==")
drive = root / "DriveLibrary"
drive.mkdir()
drive_id = library_id.for_path(drive, platform="onelibrary")
check("a directory library gets an id", bool(drive_id))
check("keyed as '.', not by a filename",
      "." in json.loads((drive / library_id.SIDECAR_NAME).read_text())["libraries"])
check("and is stable", library_id.for_path(drive) == drive_id)

print("== an unwritable location falls back rather than failing ==")
locked = root / "ReadOnly"
locked_lib = a_library(locked)
os.chmod(locked, 0o500)  # r-x: cannot create the sidecar
try:
    fallback = library_id.for_path(locked_lib, platform="traktor")
    check("an id is still issued", bool(fallback))
    check("and it says it is not durable", not library_id.is_durable(fallback))
finally:
    os.chmod(locked, 0o700)

print("== the registry is an index, and disposable ==")
before = len(library_id.known())
library_id.forget(third)
check("forget removes it", library_id.describe(third) is None)
check("and only it", len(library_id.known()) == before - 1)
# The sidecar is the source of truth, so re-opening reissues the SAME id rather
# than a new one. A registry that could be lost and silently renumber libraries
# would be worse than no registry.
check("re-opening restores it from the sidecar",
      library_id.for_path(elsewhere) == third)

print("== atomic writes: a reader never sees half a file ==")
target = Path(_TMP.name) / "atomic.json"
paths.write_json(target, {"a": 1})
paths.write_json(target, {"a": 2, "b": 3})
check("the second write replaced the first whole", paths.read_json(target) == {"a": 2, "b": 3})
check("no temp files left behind",
      not [p for p in target.parent.iterdir() if p.name.startswith(".atomic.json.tmp")],
      [p.name for p in target.parent.iterdir()])
check("a missing file reads as the default", paths.read_json(target.with_name("nope.json"), "x") == "x")
check("so does a corrupt one",
      (target.write_text("{not json"), paths.read_json(target, "x"))[1] == "x")

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
_TMP.cleanup()
raise SystemExit(1 if failed else 0)
