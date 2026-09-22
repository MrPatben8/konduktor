"""Importing a OneLibrary drive into a Traktor collection.

The first operation that puts tracks into a library that never had them, and
therefore the first that writes an ENTRY Traktor did not write. Two things need
guarding, and they pull in opposite directions:

  * **The new entries must carry the prep.** Cues on the pads they were on, the
    beatgrid including its tempo changes, and nothing silently dropped.
  * **The old entries must not move.** The whole save-fidelity guarantee rests on
    every unedited object rendering the bytes it was parsed from, and appending
    to COLLECTION is the first thing that could disturb that. So this asserts on
    rendered bytes as well as on the projection.

Runs against a temp copy of the real Traktor collection (gotcha 2: it is real,
irreplaceable data) and the checked-in OneLibrary fixture drive.
"""
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import warnings
from difflib import unified_diff
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-import-appdata-")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"

from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402
from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.core.adapter import InvalidCommand, NewTrack, Unsupported  # noqa: E402
from konduktor.core.model import CuePoint, GridMarker, Track, TrackCues  # noqa: E402

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


if not REAL.is_file() or not (FIXTURE / "PIONEER" / "rekordbox" / "exportLibrary.db").is_file():
    print(f"SKIP: need {REAL} and the OneLibrary fixture")
    sys.exit(0)


def fresh_collection() -> tuple[Path, Path, bytes]:
    """A throwaway copy of the real collection, plus a folder for copied audio."""
    work = Path(tempfile.mkdtemp(prefix="konduktor-import-"))
    nml = work / "collection.nml"
    shutil.copy(REAL, nml)
    music = work / "Imported"
    music.mkdir()
    return nml, music, nml.read_bytes()


def stage(src: OneLibraryAdapter, music: Path) -> list[NewTrack]:
    """Copy the drive's audio and describe each track as a NewTrack.

    This is what the import feature will do around the adapter: the copy is the
    caller's job, so a half-finished file copy is never something a library
    write discovers.
    """
    items = []
    for t in src.tracks:
        target = music / Path(t.filepath).name
        shutil.copy(t.filepath, target)
        items.append(NewTrack(track=t, audio_path=target, cues=src.track_cues(t.id)))
    return items


print("== a drive's tracks land in the collection ==")
nml, music, original = fresh_collection()
source = OneLibraryAdapter(FIXTURE)
dest = TraktorAdapter(nml)
before = len(dest.tracks)
items = stage(source, music)
ids = dest.add_tracks(items)

check("every staged track was added", len(ids) == len(items) == 2, str(len(ids)))
check("the collection grew by exactly that many", len(dest.tracks) == before + 2)
check("the new ids are unique", len(set(ids)) == len(ids))
# A track that is not in the projection is invisible to search, playlists and
# the deck — added but unusable.
check("each added track is in the projection", all(dest.track(i) is not None for i in ids))
check("and is findable by its id", all(dest.by_key.get(i) is not None for i in ids))

added = dest.track(ids[0])
origin = source.tracks[0]
check("title survives", added.title == origin.title, f"{added.title!r}")
check("artist survives", added.artist == origin.artist)
check("bpm survives", added.bpm == origin.bpm)
check("label survives", added.label == origin.label)
check("comment survives", added.comment == origin.comment)
check("the id is the Traktor primary key, not the drive's",
      added.id != origin.id and added.id.startswith("Macintosh HD"), added.id)
check("filepath points at the COPY, not the drive",
      str(music) in (added.filepath or ""), str(added.filepath))

print("== prep survives the crossing ==")
src_cues = source.track_cues(origin.id)
got = dest.track_cues(ids[0])

check("the flexible grid crosses intact",
      [(round(m.start, 3), m.bpm) for m in got.grid_markers]
      == [(round(m.start, 3), m.bpm) for m in src_cues.grid_markers],
      str([(m.start, m.bpm) for m in got.grid_markers]))
check("both tempo segments are there", len(got.grid_markers) == 2)

# Hot cues must keep their PAD. Muscle memory is most of what a hotcue layout
# is, so a cue that moves is nearly as bad as one that vanishes.
src_hot = {c.slot: c for c in src_cues.cues if c.role == "hotcue"}
got_by_slot = {c.slot: c for c in got.cues}
same_pad = [
    s for s, c in src_hot.items()
    if s in got_by_slot and abs(got_by_slot[s].start - c.start) < 0.001
]
check("every hot cue keeps the pad it was on",
      sorted(same_pad) == sorted(src_hot), f"kept {sorted(same_pad)} of {sorted(src_hot)}")
check("a loop stays a loop, with its length",
      got_by_slot[0].type == "loop" and abs(got_by_slot[0].length - 1.875) < 0.001)

# Traktor has no memory cues, so they degrade into spare pads rather than being
# dropped — the settled lossiness rule.
n_memory = sum(1 for c in src_cues.cues if c.role != "hotcue")
check("memory cues are preserved in spare slots, not dropped",
      len(got.cues) == len(src_hot) + n_memory, f"{len(got.cues)} cues")
check("and they do not overwrite a hot cue",
      len({c.slot for c in got.cues}) == len(got.cues))
check("every imported cue occupies a real pad",
      all(c.role == "hotcue" and c.slot is not None for c in got.cues))

# `set_analysed_grid` would place Traktor's beat-1 companion on pad A. That both
# invents a cue the source never had and displaces the imported pad A cue — it
# silently ate one during development, which is why this is asserted.
check("no beatgrid companion cue is invented",
      all(c.grid_marker is None for c in got.cues),
      str([(c.slot, c.grid_marker) for c in got.cues]))

print("== the existing 8,485 entries do not move ==")
outcome = dest.save()
after = nml.read_bytes()
diff = list(unified_diff(original.decode("utf-8", "replace").splitlines(),
                         after.decode("utf-8", "replace").splitlines(), lineterm="", n=0))
removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
# Exactly one line is rewritten: the one carrying <COLLECTION ENTRIES="N">, whose
# count changed. Everything else is pure addition.
check("only the COLLECTION header line is rewritten",
      len(removed) == 1 and "<COLLECTION ENTRIES=" in removed[0],
      f"{len(removed)} removed: {removed[:1]}")
check("nothing else is removed or reformatted",
      all("<COLLECTION ENTRIES=" in l for l in removed))

# ENTRIES is a real count Traktor reads, not decoration. Nothing else in the app
# changes how many entries exist, so nothing had ever needed to maintain it.
import re  # noqa: E402

declared = int(re.search(rb'<COLLECTION ENTRIES="(\d+)"', after).group(1))
check("the ENTRIES count is updated to match",
      declared == after.count(b"<ENTRY ") == before + 2,
      f"declared {declared}, actual {after.count(b'<ENTRY ')}")
# The version-history message is what a user reads when deciding which version
# to roll back to, so an import must not read as an edit. Asserted on the
# SaveOutcome because save() clears the journal.
check("the version-history message says the tracks were ADDED",
      "Added 2 tracks" in outcome.summary, outcome.summary)
check("and does not also call them edited",
      "edited 2 tracks" not in outcome.summary.lower(), outcome.summary)

print("== reopening proves it is really there ==")
reopened = TraktorAdapter(nml)
check("the added tracks survive a round trip", len(reopened.tracks) == before + 2)
round_tripped = reopened.track(ids[0])
check("with their metadata", round_tripped is not None and round_tripped.title == origin.title)
rt_cues = reopened.track_cues(ids[0])
check("with their grid", len(rt_cues.grid_markers) == 2)
check("and with their cues", len(rt_cues.cues) == len(got.cues))

print("== refusals ==")
check("a second import of the same file is refused, not silently duplicated",
      _raises(lambda: dest.add_tracks(items), InvalidCommand))
missing = NewTrack(track=items[0].track, audio_path=music / "nope.mp3", cues=None)
check("a missing audio file is refused before anything is written",
      _raises(lambda: dest.add_tracks([missing]), InvalidCommand))
check("read-only OneLibrary refuses to receive tracks",
      _raises(lambda: source.add_tracks(items), Unsupported))

print("== a track with no prep at all ==")
nml2, music2, _ = fresh_collection()
dest2 = TraktorAdapter(nml2)
bare_audio = music2 / "bare.mp3"
shutil.copy(items[0].audio_path, bare_audio)
bare = NewTrack(
    track=Track(id="ignored", title="Bare", artist="Nobody"),
    audio_path=bare_audio,
    cues=None,
)
bare_ids = dest2.add_tracks([bare])
check("a track with no cues and no grid still imports", len(bare_ids) == 1)
bare_cues = dest2.track_cues(bare_ids[0])
check("and has no grid", bare_cues.grid_markers == [])
check("and no cues", bare_cues.cues == [])
check("and no BPM was invented", dest2.track(bare_ids[0]).bpm is None)

print("== a full hotcue bank ==")
# Traktor has 8 pads. Nine cues cannot all fit, so the rule has to be explicit
# about WHICH survive rather than leaving it to iteration order.
crowded_audio = music2 / "crowded.mp3"
shutil.copy(items[0].audio_path, crowded_audio)
many = TrackCues(
    grid_markers=[GridMarker(start=0.0, bpm=120.0)],
    cues=[CuePoint(type="cue", role="hotcue", start=float(i), length=0.0, slot=i)
          for i in range(8)]
    + [CuePoint(type="cue", role="memory", start=99.0, length=0.0, slot=None)],
)
crowded_id = dest2.add_tracks([NewTrack(
    track=Track(id="ignored", title="Crowded", bpm=120.0),
    audio_path=crowded_audio,
    cues=many,
)])[0]
crowded = dest2.track_cues(crowded_id)
check("the bank is filled but not overflowed", len(crowded.cues) == 8, str(len(crowded.cues)))
check("the hot cues are the ones that survived",
      sorted(c.slot for c in crowded.cues) == list(range(8)))
check("the memory cue that would not fit is dropped, not misplaced",
      all(c.start < 99.0 for c in crowded.cues))

print("== the source slot ==")
# A source is a library read FROM, open alongside the loaded one. The whole slot
# rests on it being read-only, so that is asserted where the source is opened
# rather than assumed by everything downstream.
from konduktor.app_state import AppState  # noqa: E402

state = AppState()
nml3, _, _ = fresh_collection()
state.open(nml3)
check("a collection loads as the destination", state.loaded)
check("and no source is open yet", not state.source_loaded)

state.open_source(FIXTURE)
check("a read-only drive opens as a source", state.source_loaded)
check("the destination is untouched by opening a source",
      state.loaded and state.path == nml3)
check("the source projects its own tracks", len(state.source.tracks) == 2)
check("the two libraries are genuinely different objects",
      state.source is not state.adapter)

# Opening your own collection as a source of itself is the mistake this guards.
before_source = state.source
check("a WRITABLE library is refused as a source",
      _raises(lambda: state.open_source(nml3), ValueError))
check("and the refusal leaves the existing source in place",
      state.source is before_source)

state.close_source()
check("closing releases the source", not state.source_loaded)
check("but not the destination", state.loaded)
check("closing twice is safe", state.close_source() is None)

print("== folders ==")
# Imported playlists land in a folder named after the drive, and the protocol had
# no folder verb until this feature needed one.
nml4, _, _ = fresh_collection()
dest4 = TraktorAdapter(nml4)
fid = dest4.create_folder("Hardy")
check("a folder can be created", fid.startswith("fld:"), fid)
# Re-importing the same stick must land BESIDE the first import, not next to an
# identically-named twin.
check("creating it again returns the same folder", dest4.create_folder("Hardy") == fid)
pid = dest4.create_playlist("demos", fid)
node = next((n for n in dest4.playlist_tree() if n.id == fid), None)
check("the folder is in the tree", node is not None and node.kind == "folder")
check("and the playlist is inside it",
      node is not None and [c.name for c in node.children] == ["demos"])
check("read-only sources refuse to make folders",
      _raises(lambda: source.create_folder("x"), Unsupported))

print("== the job registry ==")
from konduktor.jobs import JobCancelled, JobRegistry  # noqa: E402

reg = JobRegistry()
job = reg.submit("test", lambda h: 41 + 1)
for _ in range(200):
    if job.finished:
        break
    time.sleep(0.01)
check("a job runs and reports its result", job.state == "done" and job.result == 42)

def boom(h):
    raise RuntimeError("nope")


# The registry logs a traceback for a failed job, which is right in production
# and alarming in test output — a deliberate failure should not look like a
# real one to someone scanning the suite.
logging.getLogger("konduktor.jobs").setLevel(logging.CRITICAL)
failing = reg.submit("test", boom)
for _ in range(200):
    if failing.finished:
        break
    time.sleep(0.01)
check("a failing job is reported, not swallowed",
      failing.state == "failed" and "nope" in (failing.error or ""))
logging.getLogger("konduktor.jobs").setLevel(logging.NOTSET)

# Cancel is a REQUEST: the job stops at its next checkpoint, so the state must
# not flip until the work has actually unwound and cleaned up.
started = threading.Event()

def slow(h):
    started.set()
    while True:
        h.raise_if_cancelled()
        time.sleep(0.01)

running = reg.submit("test", slow)
started.wait(2)
check("cancel is accepted while running", reg.cancel(running.id))
for _ in range(400):
    if running.finished:
        break
    time.sleep(0.01)
check("and the job ends up cancelled", running.state == "cancelled")
check("cancelling a finished job does nothing", not reg.cancel(running.id))
check("cancelling an unknown job does nothing", not reg.cancel("nope"))

print("== import: plan and run ==")
from konduktor import importer  # noqa: E402

nml5, music5, _ = fresh_collection()
dest5 = TraktorAdapter(nml5)
plan5 = importer.plan(source, dest5, music5)
check("the plan covers the whole drive when nothing is selected", len(plan5.tracks) == 2)
check("it finds the playlist", [n for _, n in plan5.playlists] == ["demos"])
check("it sizes the copy", plan5.total_bytes > 0)
check("nothing is missing", plan5.importable == plan5.tracks)

# Selecting a playlist AUTO-INCLUDES its tracks, deduplicated.
pl_id = importer._all_playlists(source)[0][0]
plan_pl = importer.plan(source, dest5, music5, playlist_ids=[pl_id])
check("selecting a playlist pulls in its tracks", len(plan_pl.tracks) == 2)
plan_both = importer.plan(
    source, dest5, music5, playlist_ids=[pl_id],
    track_ids=[t.id for t in source.tracks],
)
check("a track in both a selection and a playlist is copied once",
      len(plan_both.tracks) == 2, str(len(plan_both.tracks)))


class _Handle:
    """A JobHandle stand-in, so the import can be driven without a thread."""

    def __init__(self, cancel_after=None):
        self.messages = []
        self.done = 0
        self.total = 0
        self._cancel_after = cancel_after
        self.cancelled = False

    def progress(self, done=None, total=None, message=None):
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if message is not None:
            self.messages.append(message)
        if self._cancel_after is not None and self.done >= self._cancel_after:
            self.cancelled = True

    def raise_if_cancelled(self):
        if self.cancelled:
            raise JobCancelled()


handle = _Handle()
result = importer.run(source, dest5, plan5, handle, folder_name="Hardy")
check("the import reports what it did", result["tracks"] == 2 and result["playlists"] == 1)
check("progress reached the total", handle.total > 0 and handle.done == handle.total,
      f"{handle.done}/{handle.total}")
check("it said what it was doing", any("Copying" in m for m in handle.messages))
check("the audio really was copied",
      sorted(p.name for p in music5.iterdir()) == ["Demo Track 1.mp3", "Demo Track 2.mp3"])
check("the tracks are in the collection", len(dest5.tracks) == 8485 + 2)
tree5 = dest5.playlist_tree()
folder = next((n for n in tree5 if n.name == "Hardy"), None)
check("the playlist landed in a folder named after the drive",
      folder is not None and [c.name for c in folder.children] == ["demos"])
check("and the playlist has its tracks",
      folder is not None and folder.children[0].count == 2)

print("== import: cancelling rolls back ==")
nml6, music6, original6 = fresh_collection()
dest6 = TraktorAdapter(nml6)
plan6 = importer.plan(source, dest6, music6)
# Cancel mid-copy — which is the case that used to leave a half-written file
# behind, because cleanup only covered COMPLETED copies.
cancelling = _Handle(cancel_after=1)
cancelled_ok = _raises(
    lambda: importer.run(source, dest6, plan6, cancelling, folder_name="Hardy"),
    JobCancelled,
)
check("a cancelled import raises rather than half-finishing", cancelled_ok)
left = sorted(p.name for p in music6.iterdir()) if music6.exists() else []
check("no audio is left orphaned, including the file being written", left == [], str(left))
check("the collection on disk is untouched", nml6.read_bytes() == original6)
check("and nothing was added in memory either", len(dest6.tracks) == 8485)
# The retry must not have to suffix around debris from the cancelled run.
retry = _Handle()
importer.run(source, dest6, plan6, retry, folder_name="Hardy")
check("a retry after a cancel copies under the original names",
      sorted(p.name for p in music6.iterdir()) == ["Demo Track 1.mp3", "Demo Track 2.mp3"],
      str(sorted(p.name for p in music6.iterdir())))

print("== import: missing audio ==")
nml7, music7, _ = fresh_collection()
dest7 = TraktorAdapter(nml7)
plan7 = importer.plan(source, dest7, music7)
plan7.tracks[0].missing = True
check("a missing file is excluded from the copy", len(plan7.importable) == 1)
check("but still reported", len(plan7.as_dict()["missing"]) == 1)
r7 = importer.run(source, dest7, plan7, _Handle(), folder_name="Hardy")
check("the import proceeds without it", r7["tracks"] == 1 and r7["skipped_missing"] == 1)

print("== import: duplicate detection ==")
plan8 = importer.plan(source, dest5, music5)
check("re-importing flags the tracks as already present",
      all(t.duplicate for t in plan8.tracks), str([t.duplicate for t in plan8.tracks]))
check("but does not refuse them — the user chose to add everything",
      len(plan8.importable) == 2)

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
