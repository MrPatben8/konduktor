"""MIDI controller mappings moved to and from FILES (`/api/midi/export`,
`/api/midi/import`) — the only backend part of controller support; the
mapping itself lives in userprefs and is validated by the UI.

Pins what would hurt silently: an export never overwrites a file (a shared
mapping saved twice must not replace the first), the name cannot steer the
file outside the chosen folder, and import refuses anything that is not a
small version-1 JSON object rather than handing the UI garbage. The file
browser's `suffix` filter lists the mapping files.
"""
import json
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

from konduktor import prefs

_TMP = tempfile.TemporaryDirectory()
prefs._PREFS_PATH = Path(_TMP.name) / "userprefs.json"
prefs._LEGACY_PREFS_PATH = Path(_TMP.name) / "absent.json"

from konduktor.main import fs_list, midi_export, midi_import  # noqa: E402
from konduktor.schemas import MidiExportRequest  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def refused(fn, status):
    try:
        fn()
    except HTTPException as e:
        return e.status_code == status
    return False


folder = Path(_TMP.name) / "maps"
folder.mkdir()
mapping = {"version": 1, "shift": "note:1:63", "bindings": {"note:1:11": {"encoding": "button", "normal": "deck.A.play"}}}

print("export")
first = Path(midi_export(MidiExportRequest(dir=str(folder), name="DDJ-400", mapping=mapping))["path"])
check("written as <name>.konduktor-midi.json", first.name == "DDJ-400.konduktor-midi.json")
check("holds the mapping", json.loads(first.read_text()) == mapping)
second = Path(midi_export(MidiExportRequest(dir=str(folder), name="DDJ-400", mapping={"version": 1}))["path"])
check("never overwrites: a second export is -2", second.name == "DDJ-400-2.konduktor-midi.json")
check("the first file is untouched", json.loads(first.read_text()) == mapping)
sneaky = Path(midi_export(MidiExportRequest(dir=str(folder), name="../../escape", mapping=mapping))["path"])
check("a name cannot leave the folder", sneaky.parent == folder, str(sneaky))
check("a missing folder is refused", refused(lambda: midi_export(MidiExportRequest(dir=str(folder / "nope"), name="x", mapping=mapping)), 400))
check("a non-mapping is refused", refused(lambda: midi_export(MidiExportRequest(dir=str(folder), name="x", mapping={"version": 9})), 400))

print("import")
check("reads back what was exported", midi_import(str(first))["mapping"] == mapping)
bad = folder / "bad.konduktor-midi.json"
bad.write_text("{not json")
check("invalid JSON refused", refused(lambda: midi_import(str(bad)), 400))
bad.write_text(json.dumps([1, 2]))
check("not an object refused", refused(lambda: midi_import(str(bad)), 400))
bad.write_text(json.dumps({"version": 2}))
check("wrong version refused", refused(lambda: midi_import(str(bad)), 400))
big = folder / "big.konduktor-midi.json"
big.write_text(json.dumps({"version": 1, "pad": "x" * 1_100_000}))
check("an oversized file refused", refused(lambda: midi_import(str(big)), 400))
check("a missing file is 404", refused(lambda: midi_import(str(folder / "absent.json")), 404))

print("file browser")
(folder / "notes.json").write_text("{}")
listed = {f.name for f in fs_list(path=str(folder), suffix=".konduktor-midi.json").files}
check("the suffix lists mapping files only", "notes.json" not in listed and first.name in listed, str(listed))

sys.exit(1 if failed else 0)
