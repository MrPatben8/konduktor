"""The wire contract between a Konduktor server and the app (`remote_protocol.py`).

Pure: no library, no network. Pins what would go wrong SILENTLY:

  * every member of `LibraryAdapter` is either an RPC or excluded with a reason
    — a command added to the protocol later cannot be quietly missing from
    remote libraries (the remote adapter would raise AttributeError mid-edit);
  * every command is MUTATING — one that was not would skip the session check,
    letting a displaced computer keep editing;
  * arguments and results survive JSON by the protocol's own types (tuples,
    dataclasses, pydantic models, `None`), and a misspelt argument is refused;
  * a prepared analysis crosses intact;
  * the version rule: same major, server minor ≥ client's.
"""
from __future__ import annotations

import json
import sys
import warnings

warnings.filterwarnings("ignore")

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


import numpy as np

from konduktor import remote_protocol as rp
from konduktor.core import waveform
from konduktor.core.adapter import AudioFacts, PreparedAnalysis
from konduktor.core.model import AutoHotcue, GridMarker, Track, TrackCues

print("== coverage ==")
members = rp.protocol_members()
missing = members - set(rp.RPC) - set(rp.EXCLUDED)
check("every protocol member is an RPC or excluded", not missing, sorted(missing))
stray = (set(rp.RPC) | set(rp.EXCLUDED)) - members
check("…and nothing is listed that the protocol lacks", not stray, sorted(stray))
check("no member is both", not (set(rp.RPC) & set(rp.EXCLUDED)))
check("every exclusion says why", all(len(r) > 10 for r in rp.EXCLUDED.values()))
VERBS = ("set_", "create_", "delete_", "rename_", "add_", "remove_", "move_", "replace_", "place_", "apply_")
commands = {m for m in members if m.startswith(VERBS)}
unguarded = {m for m in commands if m in rp.RPC and not rp.RPC[m].mutating}
check("every command that travels needs the session", not unguarded, sorted(unguarded))
reads = {"tracks", "track", "track_cues", "query_tracks", "capabilities", "audio_facts", "dirty"}
check("reads do not", all(not rp.RPC[m].mutating for m in reads))
for name in rp.RPC:
    try:
        rp._params(name)
        rp._adapter(rp._result_hint(name))
    except Exception as ex:  # noqa: BLE001
        check(f"{name}: types resolve", False, repr(ex))

print("== round trips ==")


def through_json(name, args):
    wire = json.loads(json.dumps({"args": rp.dump_args(name, args)}))
    return rp.load_args(name, wire["args"])


got = through_json("set_cue", {"track_id": "a", "slot": 2, "start_sec": 1.5, "cue_type": "loop",
                               "length_sec": 4.0, "name": None})
check("set_cue's arguments survive", got == {"track_id": "a", "slot": 2, "start_sec": 1.5,
                                              "cue_type": "loop", "length_sec": 4.0, "name": None}, got)
got = through_json("replace_grid", {"track_id": "a", "markers": [(0.1, 128.0), (60.0, 130.0)]})
check("a grid's (start, bpm) tuples survive", got["markers"] == [(0.1, 128.0), (60.0, 130.0)], got)
cue = AutoHotcue(slot=1, start=12.0, name="Drop")
got = through_json("place_cues", {"track_id": "a", "cues": [cue], "overwrite": True})
check("place_cues gets AutoHotcue objects back", isinstance(got["cues"][0], AutoHotcue)
      and got["cues"][0].start == 12.0 and got["overwrite"] is True, got)
got = through_json("query_tracks", {"q": "acid", "bpm_min": 120.0, "limit": 5})
check("query_tracks' keywords survive", got == {"q": "acid", "bpm_min": 120.0, "limit": 5}, got)
try:
    rp.load_args("track", {"track_id": "a", "trak_id": "b"})
    check("a misspelt argument is refused", False)
except ValueError:
    check("a misspelt argument is refused", True)
try:
    rp.load_args("set_cue", {"track_id": "a"})
    check("a missing argument is refused", False)
except ValueError:
    check("a missing argument is refused", True)

facts = {"t1": AudioFacts(key="/music/a.mp3", size=10, mtime_ns=123)}
back = rp.load_result("audio_facts", json.loads(json.dumps(rp.dump_result("audio_facts", facts))))
check("audio facts survive as dataclasses", back == facts, back)
cues = TrackCues(track_id="t1", grid_markers=[GridMarker(start=0.1, bpm=128)])
back = rp.load_result("track_cues", json.loads(json.dumps(rp.dump_result("track_cues", cues))))
check("TrackCues survive", back == cues)
check("None survives", rp.load_result("track", rp.dump_result("track", None)) is None)
t = Track(id="x", title="T")
check("a Track survives", rp.load_result("track", rp.dump_result("track", t)) == t)

print("== a prepared analysis ==")
rng = np.random.default_rng(1)
measured = waveform.Analysis(
    columns=waveform.Columns(rms=rng.random(400).astype(np.float32),
                             brightness=rng.random(400).astype(np.float32), duration=181.5),
    frames=waveform.Frames(bands=rng.random((27225, 3)).astype(np.float32), duration=181.5),
)
packed = rp.pack_analysis(PreparedAnalysis(measured=measured, markers=[GridMarker(start=0.25, bpm=124.0)]))
back = rp.unpack_analysis(packed)
check("columns, frames and durations cross intact",
      np.array_equal(back.measured.columns.rms, measured.columns.rms)
      and np.array_equal(back.measured.frames.bands, measured.frames.bands)
      and back.measured.duration == 181.5)
check("…and the detected grid", [(m.start, m.bpm) for m in back.markers] == [(0.25, 124.0)])
check("no analysis is None", rp.pack_analysis(None) is None and rp.unpack_analysis(None) is None)

print("== versions ==")
check("same version talks", rp.compatible((1, 0), (1, 0)) is None)
check("a newer server minor talks", rp.compatible((1, 0), (1, 3)) is None)
check("an older server minor: update the server", rp.compatible((1, 2), (1, 1)) == "server")
check("a newer server major: update the app", rp.compatible((1, 0), (2, 0)) == "app")
check("an older server major: update the server", rp.compatible((2, 0), (1, 9)) == "server")

print()
print("RESULT:", "FAILURES" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
