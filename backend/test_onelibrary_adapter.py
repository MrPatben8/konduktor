"""Tests for the OneLibrary adapter (read-only).

The third platform against the same contract, and the first whose library is a
REMOVABLE DRIVE rather than a file — so the layout resolution gets as much
attention here as the projection does.

Unlike `test_rekordbox_adapter.py`, this needs nothing installed and nothing
plugged in: it runs against `fixtures/onelibrary/`, a real rekordbox 7 export
trimmed to fixture size (see its README). That makes it deterministic on any
machine, and it also means the expected values below are checked against bytes
rekordbox wrote rather than against bytes Konduktor wrote — which is the only
way a "we read the format correctly" claim means anything.

Where a value was cross-checked against the SAME cues in the desktop
`master.db`, the test says so, because that cross-check is what pins the slot
numbering (the one thing most likely to be silently wrong).
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-ol-appdata-")

from konduktor.adapters.onelibrary import discovery  # noqa: E402
from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.adapters.onelibrary.beatgrid import markers_from_beats  # noqa: E402
from konduktor.adapters.onelibrary.cues import (  # noqa: E402
    MEMORY_SLOT,
    NO_LOOP,
    cues_from_anlz,
)
from konduktor.adapters.onelibrary.driver import OneLibraryDriver  # noqa: E402
from konduktor.adapters.onelibrary.layout import DriveLayout  # noqa: E402
from konduktor.core import registry  # noqa: E402
from konduktor.core.adapter import LibraryAdapter, NotFound, Unsupported  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"

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
    except Exception:  # noqa: BLE001 — the wrong error is still a failure
        return False
    return False


# ---- pure units: no drive required --------------------------------------
print("== drive layout ==")
tmp = Path(tempfile.mkdtemp(prefix="konduktor-ol-layout-"))
(tmp / "PIONEER" / "rekordbox").mkdir(parents=True)
db = tmp / "PIONEER" / "rekordbox" / "exportLibrary.db"
db.write_bytes(b"not really a database")

layout = DriveLayout.locate(tmp)
check("a drive root is recognised", layout is not None and layout.database == db)
check("and so is the database file itself",
      (l2 := DriveLayout.locate(db)) is not None and l2.root == tmp)
check("a directory with no PIONEER folder is not a drive",
      DriveLayout.locate(Path(tempfile.mkdtemp())) is None)
check("a stray file called exportLibrary.db is not a drive",
      DriveLayout.locate(Path(tempfile.mkdtemp()) / "exportLibrary.db") is None)

# The stored path leads with a slash but is NOT absolute. Joining it naively
# discards the mount point entirely and silently yields /Contents/... — a bug
# that would look like "the drive is empty" rather than like a path error.
resolved = layout.resolve("/Contents/Music/a.mp3")
check("a drive-relative path resolves under the mount point",
      resolved == tmp / "Contents" / "Music" / "a.mp3", str(resolved))
check("the leading slash is not treated as a host root",
      not str(resolved).startswith("/Contents"))
check("backslashes resolve too", layout.resolve("\\Contents\\a.mp3") == tmp / "Contents" / "a.mp3")
check("an empty path resolves to nothing", layout.resolve("") is None and layout.resolve(None) is None)
check("the .EXT sits beside the .DAT",
      layout.extended_anlz(Path("/x/ANLZ0000.DAT")) == Path("/x/ANLZ0000.EXT"))
shutil.rmtree(tmp, ignore_errors=True)

print("== beatgrid collapse ==")
# Shared with the Rekordbox adapter on purpose — two copies of this rule would be
# free to drift apart by a rounding tolerance, and a drifting grid is silent.
check("a constant tempo is one marker",
      len(markers_from_beats([0.0, 0.5, 1.0], [120.0, 120.0, 120.0])) == 1)
check("a tempo change adds a marker",
      [m.bpm for m in markers_from_beats([0.0, 0.5, 1.0], [120.0, 120.0, 90.0])] == [120.0, 90.0])
check("no beats is no grid", markers_from_beats([], []) == [])


class _Entry(dict):
    """A stand-in for a parsed ANLZ cue entry (construct Containers are dicts)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as ex:
            raise AttributeError(name) from ex


class _Tag:
    def __init__(self, tag_type, entries):
        self.type = tag_type
        self.struct = type("S", (), {"content": type("C", (), {"entries": entries})()})()


class _Anlz:
    def __init__(self, tags):
        self.tags = tags


def _entry(**kw):
    base = dict(hot_cue=1, time=1000, loop_time=NO_LOOP, type=1,
                color_red=0, color_green=0, color_blue=0, comment="")
    base.update(kw)
    return _Entry(base)


print("== cue decoding ==")
# The generic slot is 0-based (slot 0 is the pad labelled A). ANLZ's hot_cue is
# a DENSE 1-based index, unlike master.db's Kind which skips 4 — so the two
# platforms need different arithmetic even though they are the same vendor.
one = cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(hot_cue=1, time=5000)])]), None)
check("ANLZ hot_cue 1 is generic slot 0 — the pad labelled A",
      len(one) == 1 and one[0].slot == 0 and one[0].role == "hotcue")
four = cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(hot_cue=4)])]), None)
check("hot_cue 4 is slot 3 (pad D) — NOT shifted past a reserved value",
      four[0].slot == 3)
mem = cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(hot_cue=MEMORY_SLOT)])]), None)
check("hot_cue 0 is a memory cue with no slot",
      mem[0].role == "memory" and mem[0].slot is None)

# loop_time is an unsigned -1 when there is no loop, so a falsiness test would
# read 0xFFFFFFFF as a loop ending at zero.
point = cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(type=1, loop_time=NO_LOOP)])]), None)
check("a point cue has no length", point[0].length == 0.0 and point[0].type == "cue")
loop = cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(type=2, time=40000, loop_time=41875)])]), None)
check("a loop's length is its out minus its in",
      loop[0].type == "loop" and abs(loop[0].length - 1.875) < 1e-9)

check("a colour comes through as #RRGGBB",
      cues_from_anlz(_Anlz([_Tag("PCO2", [_entry(color_red=255, color_green=140, color_blue=0)])]),
                     None)[0].color == "#FF8C00")
check("all-zero RGB means 'unset', not black",
      cues_from_anlz(_Anlz([_Tag("PCO2", [_entry()])]), None)[0].color is None)

# PCOB is split across the two files — .DAT holds pads 1-3, .EXT holds 4 and up —
# so a reader that looks at one file has an incomplete bank.
dat = _Anlz([_Tag("PCOB", [_entry(hot_cue=1), _entry(hot_cue=2, time=2000)])])
ext = _Anlz([_Tag("PCOB", [_entry(hot_cue=4, time=4000)])])
merged = cues_from_anlz(dat, ext)
check("PCOB is merged across the .DAT and the .EXT",
      sorted(c.slot for c in merged) == [0, 1, 3], str([c.slot for c in merged]))
check("PCO2 wins over PCOB when both are present",
      cues_from_anlz(dat, _Anlz([_Tag("PCO2", [_entry(hot_cue=8, time=9000)])]))[0].slot == 7)
check("the same cue in two tags is reported once",
      len(cues_from_anlz(_Anlz([_Tag("PCO2", [_entry()]), _Tag("PCO2", [_entry()])]), None)) == 1)
check("no cue tags is no cues", cues_from_anlz(_Anlz([]), None) == [])
check("no analysis file at all is no cues", cues_from_anlz(None, None) == [])
check("nothing read from a drive claims to be editable",
      all(not c.editable for c in merged))


# ---- the real drive ------------------------------------------------------
if not (FIXTURE / "PIONEER" / "rekordbox" / "exportLibrary.db").is_file():
    print(f"\nSKIP: no fixture drive at {FIXTURE}")
    sys.exit(1 if failed else 0)

print("\n== the driver recognises a drive by layout AND schema ==")
driver = OneLibraryDriver()
check("can_open accepts the drive root", driver.can_open(FIXTURE))
check("can_open accepts the database directly",
      driver.can_open(FIXTURE / "PIONEER" / "rekordbox" / "exportLibrary.db"))
check("can_open rejects an unrelated directory", not driver.can_open(Path(tempfile.mkdtemp())))
check("the registry picks OneLibrary for it", registry.driver_for(FIXTURE).platform == "onelibrary")

# The two encrypted databases must not be confused for one another. They are both
# SQLCipher with no readable header, and only the KEY tells them apart.
fake = Path(tempfile.mkdtemp()) / "PIONEER" / "rekordbox" / "exportLibrary.db"
fake.parent.mkdir(parents=True)
shutil.copy(Path(__file__), fake)  # a plain text file wearing the right name
check("can_open rejects a non-database in the right place", not driver.can_open(fake.parent.parent.parent))

adapter = OneLibraryAdapter(FIXTURE)
check("implements the LibraryAdapter protocol", isinstance(adapter, LibraryAdapter))

print("== the projection ==")
tracks = adapter.tracks
check("both tracks project", len(tracks) == 2, str(len(tracks)))
by_title = {t.title: t for t in tracks}
one = next(t for t in tracks if t.title and t.title.startswith("Demo Track 1"))
check("title comes through", one.title.startswith("Demo Track 1"))
check("artist comes through the lookup table", one.artist == "Loopmasters")
check("label comes through the lookup table", one.label == "Loopmasters")
check("bpmx100 becomes a real BPM", one.bpm == 128.0, str(one.bpm))
check("length is whole seconds", one.length == 172, str(one.length))
check("rating is 0-5, not Traktor's /51", 0 <= one.rating <= 5)
check("comment comes through", (one.comment or "").startswith("Tracks by"))

# The id is the drive-relative path, NOT content_id: rekordbox renumbers rows
# from 1 on every re-export, so a row number would break every remembered
# reference the moment the user re-wrote the stick.
check("the track id is its drive-relative path",
      one.id == "/Contents/Loopmasters/UnknownAlbum/Demo Track 1.mp3", one.id)
check("filepath resolves under the mount point",
      one.filepath == str(FIXTURE / "Contents/Loopmasters/UnknownAlbum/Demo Track 1.mp3"))
check("and the resolved file is really there", Path(one.filepath).is_file())
check("audio_path agrees with the projection", str(adapter.audio_path(one.id)) == one.filepath)

print("== counts are approximate until the cues are read ==")
check("no cues are claimed before the analysis file is parsed", one.cue_count == 0)
check("a tempo plus an analysis file is guessed as one marker", one.grid_marker_count == 1)
cues = adapter.track_cues(one.id)
check("reading cues corrects cue_count", one.cue_count == len(cues.cues) == 5, str(one.cue_count))
check("reading cues corrects hotcue_count", one.hotcue_count == 4, str(one.hotcue_count))
check("reading cues corrects grid_marker_count", one.grid_marker_count == 2, str(one.grid_marker_count))
check("track_cues on an unknown track is None", adapter.track_cues("no-such-track") is None)

print("== the beatgrid survives an export ==")
# This grid was written into master.db by Konduktor, exported by rekordbox, and
# confirmed on screen in Rekordbox 7 before it ever reached this fixture.
markers = cues.grid_markers
check("a flexible grid stays flexible", len(markers) == 2, str(len(markers)))
check("the first segment is 128 BPM", markers[0].bpm == 128.0)
check("the second segment is 90 BPM", markers[1].bpm == 90.0)
check("and it starts at 60 s", abs(markers[1].start - 60.0) < 0.001, str(markers[1].start))
check("markers are ordered by position", [m.start for m in markers] == sorted(m.start for m in markers))

print("== cues, cross-checked against the same cues in master.db ==")
# master.db held djmdCue Kind 1/2/3/5 at 40.000 s / 18.773 s / 90.667 s /
# 120.000 s plus a reserved-Kind memory loop at 40.000 s. Those same five cues
# must come back here as slots 0/1/2/3 and one memory cue — which is what pins
# ANLZ's dense numbering against master.db's sparse bank.
hot = {c.slot: c for c in cues.cues if c.role == "hotcue"}
check("four hot cues, on pads A-D", sorted(hot) == [0, 1, 2, 3], str(sorted(hot)))
check("pad A is the loop at 40.000 s",
      hot[0].type == "loop" and abs(hot[0].start - 40.0) < 0.001)
check("pad A's loop is 4 beats at 128 BPM = 1.875 s", abs(hot[0].length - 1.875) < 0.001)
check("pad B is at 18.775 s", abs(hot[1].start - 18.775) < 0.001, str(hot[1].start))
check("pad C is at 90.667 s", abs(hot[2].start - 90.667) < 0.001, str(hot[2].start))
# Pad D exists ONLY in the .EXT. A reader that stopped at the .DAT would lose it,
# and would do so silently.
check("pad D is at 120.000 s — the cue that lives only in the .EXT",
      abs(hot[3].start - 120.0) < 0.001, str(hot[3].start))
check("hot cues carry their RGB colour", all(c.color and c.color.startswith("#") for c in hot.values()))

memory = [c for c in cues.cues if c.role == "memory"]
check("the memory cue is preserved", len(memory) == 1)
check("it has no slot", memory[0].slot is None)
check("it is the same loop, at the same place", abs(memory[0].start - 40.0) < 0.001)
check("no cue on a read-only drive is editable", all(not c.editable for c in cues.cues))

print("== playlists ==")
tree = adapter.playlist_tree()
check("the playlist is at the root", len(tree) == 1 and tree[0].name == "demos", str(tree))
node = tree[0]
check("it is a playlist, not a folder", node.kind == "playlist" and node.selectable)
check("its count is right", node.count == 2, str(node.count))
check("playlist_count agrees", adapter.playlist_count() == 1)
entries = adapter.playlist_entries(node.id)
check("its entries are track ids, in order",
      entries == [t.id for t in (adapter.playlist_tracks(node.id) or [])])
check("and they resolve to the two tracks", len(adapter.playlist_tracks(node.id)) == 2)
check("a root playlist's parent 0 is not treated as a real node id", len(tree) == 1)
check("nothing on the drive claims to be editable",
      not any(n.can_rename or n.can_delete or n.can_add_tracks or n.can_reorder for n in tree))
check("entries for an unknown node are None", adapter.playlist_entries("999999") is None)

print("== capabilities gate the UI ==")
caps = adapter.capabilities()
check("platform is onelibrary", caps.platform == "onelibrary")
# The distinction the whole capability system exists for: this is a roadmap gap,
# not a protective refusal, and the UI must word the two differently.
check("the library is read-only", caps.writable is False)
check("because the platform is incomplete, not because of a sync state",
      caps.readonly_cause == "platform_incomplete")
check("the version is the drive's dbVersion", caps.version == "1000", str(caps.version))
# The per-feature flags still describe the FORMAT. Blanking them to false would
# claim OneLibrary has no hot cues, which is not what read-only means.
check("the format is still reported as having hot cues", caps.cues.hotcue_slots == 8)
check("cue colour is free RGB here, unlike master.db's palette", caps.cues.color == "free")
check("loops are a cue type, not a separate bank", caps.cues.loops == "cue_type")
check("memory cues are acknowledged", caps.cues.memory_cues is True)
check("the grid is flexible even though we do not write it", caps.grid.flexible is True)
check("nothing is editable", not caps.cues.editable and not caps.grid.editable)
check("no metadata field is editable", caps.tracks.editable_fields == [])
check("ratings are 0-5", caps.tracks.rating_max == 5)
check("folders are supported by the format", caps.playlists.folders is True)
check("a drive carries no smart playlists", caps.playlists.smart == "none")
check("drives are not versioned", caps.save.history is False)
check("nothing else can overwrite it, because we never write", caps.save.overwrite_risk == "none")
check("the label is the drive's name", caps.save.library_label == FIXTURE.name)

print("== every command is refused ==")
COMMANDS = [
    ("set_track_metadata", lambda: adapter.set_track_metadata(one.id, {"title": "x"})),
    ("set_cover_art", lambda: adapter.set_cover_art(one.id, b"", "image/png")),
    ("create_playlist", lambda: adapter.create_playlist("x")),
    ("rename_playlist", lambda: adapter.rename_playlist(node.id, "x")),
    ("delete_playlist", lambda: adapter.delete_playlist(node.id)),
    ("set_playlist_entries", lambda: adapter.set_playlist_entries(node.id, [])),
    ("set_cue", lambda: adapter.set_cue(one.id, slot=0, start_sec=1.0, cue_type="cue")),
    ("set_cue_type", lambda: adapter.set_cue_type(one.id, 0, "loop")),
    ("delete_cue", lambda: adapter.delete_cue(one.id, 0)),
    ("place_cues", lambda: adapter.place_cues(one.id, [])),
    ("add_grid_marker", lambda: adapter.add_grid_marker(one.id, 1.0)),
    ("move_grid_marker", lambda: adapter.move_grid_marker(one.id, 0, 1.0)),
    ("set_grid_marker_bpm", lambda: adapter.set_grid_marker_bpm(one.id, 0, 120.0)),
    ("delete_grid_marker", lambda: adapter.delete_grid_marker(one.id, 0)),
    ("replace_grid", lambda: adapter.replace_grid(one.id, [])),
    ("set_analysed_grid", lambda: adapter.set_analysed_grid(one.id, [])),
    ("delete_grid", lambda: adapter.delete_grid(one.id)),
    ("set_grid_lock", lambda: adapter.set_grid_lock(one.id, True)),
    ("remap_locations", lambda: adapter.remap_locations(None)),
    ("save", lambda: adapter.save()),
    ("snapshot", lambda: adapter.snapshot()),
]
refused = [name for name, fn in COMMANDS if not _raises(fn, Unsupported)]
check(f"all {len(COMMANDS)} commands raise Unsupported", not refused, "; ".join(refused))
check("nothing is ever dirty", adapter.dirty is False)
check("an unknown track is a NotFound, not an Unsupported",
      _raises(lambda: adapter.audio_path("no-such-track"), NotFound))

print("== reads that answer rather than refuse ==")
check("cover art is absent, not an error", adapter.cover_art(one.id) is None)
# A drive stores everything relative to its own root, so it cannot have stale
# absolute paths and there is nothing for a remap to fix.
check("a path mapping is accepted and ignored", adapter.set_path_mapping(None) is None)
check("remap_preview matches nothing", adapter.remap_preview(None)["matched"] == 0)
check("path suggestions still describe the drive",
      adapter.path_prefix_suggestions()["primary"] is not None)

print("== discovery ==")
described = discovery.describe(FIXTURE)
check("a drive describes itself by name", FIXTURE.name in described["label"], described["label"])
check("and points at the database", described["path"].endswith("exportLibrary.db"))
check("and reports that it exists", described["exists"] is True)
check("detection of unplugged drives is empty, not an error",
      isinstance(discovery.detect_libraries(), list))

adapter.close()
check("closing twice is safe", adapter.close() is None)

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
