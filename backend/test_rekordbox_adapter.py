"""Tests for the Rekordbox adapter (read-only milestone).

The counterpart to `test_traktor_adapter.py`: it exercises the GENERIC layer for
the second platform — the projection, the cue/grid translation, the playlist
tree, capabilities, and the refusal of every command — so that "a second adapter
satisfies the same contract" is a claim the suite actually checks rather than a
design intention.

Runs against a TEMP COPY of the local Rekordbox library, and skips cleanly when
there is no Rekordbox installed (CI, or any machine but the developer's).
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-rb-appdata-")

from konduktor.adapters.rekordbox import discovery  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402
from konduktor.adapters.rekordbox.beatgrid import (  # noqa: E402
    beats_from_markers,
    markers_from_beats,
)
from konduktor.adapters.rekordbox.cue_types import (  # noqa: E402
    beat_loop_size,
    beats_in_loop,
    kind_for,
    role_and_slot,
)
from konduktor.adapters.rekordbox.driver import RekordboxDriver  # noqa: E402
from konduktor.adapters.rekordbox.projection import parse_key  # noqa: E402
from konduktor.core import registry  # noqa: E402
from konduktor.core.adapter import (  # noqa: E402
    InvalidCommand,
    LibraryAdapter,
    NotFound,
    Unsupported,
)
from konduktor.core.model import GridMarker, TrackCues  # noqa: E402

failed = False


def _raises(fn, exc) -> bool:
    """True when `fn` raises exactly the expected adapter error."""
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001 — the wrong error is still a failure
        return False
    return False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


# ---- pure units: no library required -----------------------------------
print("== cue encoding ==")
check("Kind 0 is a memory cue with no slot", role_and_slot(0) == ("memory", None))
check("Kind N is hot cue slot N", role_and_slot(5) == ("hotcue", 5))
check("round-trips back to Kind", kind_for(*role_and_slot(5)) == 5 and kind_for("memory", None) == 0)
# Verified against two real loops: 1 beat @125 BPM and 4 beats @120 BPM.
check("BeatLoopSize encodes (beats << 16) | 1", beat_loop_size(4) == 262145 and beat_loop_size(1) == 65537)
check("and decodes back", beats_in_loop(262145) == 4 and beats_in_loop(None) is None)

print("== key parsing (Rekordbox names keys musically, not in Camelot) ==")
check("Fm -> 4A", parse_key("Fm") == (4, "minor"))
check("Am -> 8A", parse_key("Am") == (8, "minor"))
check("C -> 8B", parse_key("C") == (8, "major"))
check("Ebm -> 2A", parse_key("Ebm") == (2, "minor"))
check("unparseable key is None, not a guess", parse_key("nonsense") == (None, None))
check("absent key is None", parse_key(None) == (None, None))

print("== beatgrid: per-beat <-> marker list ==")
# Rekordbox stores EVERY beat; the generic model is a marker list.
times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.4, 2.8]
bpms = [120.0, 120.0, 120.0, 120.0, 120.0, 150.0, 150.0]
markers = markers_from_beats(times, bpms)
check("a tempo change makes a second marker", len(markers) == 2, str(len(markers)))
check("markers carry their tempo and start", markers[0].bpm == 120.0 and markers[1].start == 2.4)
const = markers_from_beats([0.0, 0.5, 1.0], [120.0, 120.0, 120.0])
check("a constant-tempo grid is ONE marker", len(const) == 1)
check("an empty grid projects to no markers", markers_from_beats([], []) == [])
# Float noise must not manufacture markers.
noisy = markers_from_beats([0.0, 0.5, 1.0], [120.0, 120.000001, 119.999999])
check("float noise does not split a marker", len(noisy) == 1, str(len(noisy)))
# The inverse.
beat_nums, out_bpms, out_times = beats_from_markers([GridMarker(start=0.0, bpm=120.0)], 2.0)
check("expanding one marker gives a beat every 0.5s at 120 BPM", out_times == [0.0, 0.5, 1.0, 1.5], str(out_times))
check("beats are numbered 1-4 in bars", beat_nums == [1, 2, 3, 4], str(beat_nums))
check("expanding no markers gives no beats", beats_from_markers([], 10.0) == ([], [], []))

# ---- the rest needs a real library --------------------------------------
found = discovery.detect_libraries()
if not found:
    print("\n== library tests SKIPPED: no Rekordbox library found on this machine ==")
    print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
    sys.exit(1 if failed else 0)

REAL = Path(found[0]["path"])
print(f"\n(using a temp copy of {REAL})")

with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "master.db"
    shutil.copy2(REAL, work)
    # The beatgrid lives in the ANLZ files under share/, not in the database.
    share = REAL.parent / "share"
    if share.is_dir():
        shutil.copytree(share, Path(d) / "share")

    print("== the driver recognises the library by probing, not by extension ==")
    driver = RekordboxDriver()
    check("can_open accepts a real master.db", driver.can_open(work))
    plain = Path(d) / "plain.db"
    import sqlite3

    sqlite3.connect(plain).execute("CREATE TABLE t (a)")
    check("can_open rejects an unencrypted .db", not driver.can_open(plain))
    check("can_open rejects a non-database", not driver.can_open(Path(d) / "missing.db"))
    check("the registry picks Rekordbox for it", registry.driver_for(work).platform == "rekordbox")

    adapter = RekordboxAdapter(work)

    print("== the projection ==")
    check("implements the LibraryAdapter protocol", isinstance(adapter, LibraryAdapter))
    check("tracks are projected", len(adapter.tracks) > 0, str(len(adapter.tracks)))
    check("the primary-key index agrees with the list", len(adapter.by_key) == len(adapter.tracks))
    check("platform is reported generically", adapter.platform == "rekordbox")
    sample = adapter.tracks[0]
    check("a track id is a plain string", isinstance(sample.id, str) and sample.id != "")
    check("track() resolves by id", adapter.track(sample.id) is sample)
    check("an unknown id resolves to None", adapter.track("no-such-track") is None)
    rated = [t for t in adapter.tracks if t.rating]
    check("ratings are already 0-5 (no /51 conversion)", all(0 <= t.rating <= 5 for t in adapter.tracks))
    if rated:
        check("at least one rating survives the projection", max(t.rating for t in rated) >= 1)

    print("== cues project into the generic vocabulary ==")
    with_cues = [t for t in adapter.tracks if t.cue_count]
    check("some track has cues", bool(with_cues), "none found")
    if with_cues:
        tc = adapter.track_cues(with_cues[0].id)
        check("track_cues returns a projection", tc is not None)
        check("every cue type is in the generic vocabulary",
              all(c.type in ("cue", "loop") for c in tc.cues),
              str({c.type for c in tc.cues}))
        check("every role is generic", all(c.role in ("hotcue", "memory") for c in tc.cues))
        check("a memory cue has no slot",
              all(c.slot is None for c in tc.cues if c.role == "memory"))
        check("a hot cue has a slot", all(c.slot is not None for c in tc.cues if c.role == "hotcue"))
        check("no cue claims to be editable in the read-only milestone",
              all(not c.editable for c in tc.cues))
        check("grid markers carry no companion (a Traktor convention)",
              all(m.companion is None for m in tc.grid_markers))
        loops = [c for c in tc.cues if c.type == "loop"]
        if loops:
            check("a loop has a positive length", all(c.length > 0 for c in loops))

    print("== the beatgrid is read lazily and corrects the projection ==")
    # Rekordbox analyses one-shot samples too, and gives them an ANLZ file with
    # no beatgrid — so "has an analysis file" does NOT mean "has a grid".
    gridded = [t for t in adapter.tracks if t.bpm]
    if gridded:
        t = gridded[0]
        tc = adapter.track_cues(t.id)
        check("a track with a tempo projects at least one grid marker", len(tc.grid_markers) >= 1)
        check("markers are ordered by start",
              all(a.start <= b.start for a, b in zip(tc.grid_markers, tc.grid_markers[1:])))
        check("every marker has a positive tempo", all(m.bpm > 0 for m in tc.grid_markers))
    # The approximation must be CORRECTED by a read, for every track — including
    # the sample one-shots, whose true count is zero.
    mismatched = []
    for t in adapter.tracks[:40]:
        markers = adapter.track_cues(t.id).grid_markers
        if t.grid_marker_count != len(markers):
            mismatched.append(f"{t.title}: {t.grid_marker_count} vs {len(markers)}")
    check("reading a grid corrects grid_marker_count on every track",
          not mismatched, "; ".join(mismatched[:3]))
    check("track_cues on an unknown track is None", adapter.track_cues("no-such-track") is None)

    print("== playlists ==")
    tree = adapter.playlist_tree()
    check("the tree is a list of generic nodes", isinstance(tree, list))
    ids = []

    def walk(nodes):
        for n in nodes:
            ids.append(n.id)
            check_kind = n.kind in ("folder", "playlist", "smart")
            if not check_kind:
                check(f"node {n.name!r} has a generic kind", False, n.kind)
            walk(n.children)

    walk(tree)
    check("no internal Rekordbox playlist is exposed",
          all(i not in ("100000", "200000") for i in ids), str(ids))
    check("nothing claims to be editable", not any(
        n.can_rename or n.can_delete or n.can_add_tracks or n.can_reorder
        for n in tree
    ))

    print("== capabilities gate the UI ==")
    caps = adapter.capabilities()
    check("platform is rekordbox", caps.platform == "rekordbox")
    # Milestone 2: the library IS writable, but only for what is implemented.
    # The library-level flag and the per-feature flags are separate gates on
    # purpose — see the cue/grid refusals below.
    check("a local library is writable", caps.writable is True)
    check("with no read-only cause", caps.readonly_cause is None, str(caps.readonly_cause))
    check("per-feature flags still describe what Rekordbox CAN do",
          caps.cues.memory_cues and caps.grid.flexible)
    check("hot cues are editable", caps.cues.editable is True)
    check("the grid is editable", caps.grid.editable is True)
    check("but has no lock — Rekordbox has no such concept", caps.grid.lockable is False)
    check("8 hot cue slots, labelled by letter",
          caps.cues.hotcue_slots == 8 and caps.cues.slot_labels == "letter")
    check("memory cues are supported", caps.cues.memory_cues)
    # Loops are READ but not WRITTEN — a cue row with an out-point does not land
    # in the slot its Kind names (verified in Rekordbox), so offering it would
    # silently create an uneditable memory cue.
    check("only writable cue types are advertised", caps.cues.types == ["cue"],
          str(caps.cues.types))
    # A loop is a cue with an out-point, NOT a separate bank — verified on a real
    # library, and the opposite of what the original plan assumed.
    check("a loop is a cue type, not a separate bank", caps.cues.loops == "cue_type")
    check("cue colour is a palette, not free RGB", caps.cues.color == "palette")
    check("the editable field set is advertised", bool(caps.tracks.editable_fields))
    # Rekordbox has no column for these; Traktor does. Mapping them onto its
    # Composer field would silently write the wrong thing.
    check("producer/mix are absent rather than approximated",
          "producer" not in caps.tracks.editable_fields
          and "mix" not in caps.tracks.editable_fields)
    check("version history is off (multi-artefact library)", caps.save.history is False)
    check("the save warning says Rekordbox holds the library open",
          caps.save.overwrite_risk == "while_running")
    check("library label is master.db", caps.save.library_label == "master.db")

    print("== what Rekordbox genuinely cannot do still refuses, with a reason ==")
    commands = {
        "set_cover_art": lambda: adapter.set_cover_art(sample.id, b"", "image/jpeg"),
        # Rekordbox genuinely has no per-track grid lock, unlike Traktor's LOCK.
        "set_grid_lock": lambda: adapter.set_grid_lock(sample.id, True),
        "remap_locations": lambda: adapter.remap_locations(None),
        # save() works now; snapshot() must not, because a Rekordbox library is
        # more than one file and there is nothing to version.
        "snapshot": lambda: adapter.snapshot(),
    }
    refused = []
    for name, fn in commands.items():
        try:
            fn()
            refused.append(name)
        except Unsupported:
            pass
        except Exception as ex:  # noqa: BLE001 — anything else is the wrong error
            refused.append(f"{name} ({type(ex).__name__})")
    check("each raises Unsupported rather than failing oddly", not refused, ", ".join(refused))
    check("refusing a command leaves the adapter clean", adapter.dirty is False)

    print("== metadata writes, including the foreign-key fields ==")
    # artist/album/genre/label are FKs into lookup tables, not columns. Setting
    # one must (a) find-or-create the lookup row — pyrekordbox's add_* RAISES on
    # an existing name — and (b) return a projection that reflects the new value,
    # which needs a flush+expire or the ORM reads the stale relationship back.
    writable_target = adapter.tracks[0]
    updated = adapter.set_track_metadata(
        writable_target.id,
        {"genre": "Konduktor Probe Genre", "artist": "Konduktor Probe Artist", "rating": 4},
    )
    check("the command returns the refreshed projection", updated is not None)
    if updated:
        check("a NEW lookup value is reflected immediately",
              updated.genre == "Konduktor Probe Genre" and updated.artist == "Konduktor Probe Artist",
              f"{updated.genre!r} / {updated.artist!r}")
        check("a direct column is reflected immediately", updated.rating == 4, str(updated.rating))
    check("the index holds the same refreshed track",
          adapter.track(writable_target.id).genre == "Konduktor Probe Genre")
    # Re-using an EXISTING lookup value must not try to create it again.
    existing_genre = next(
        (t.genre for t in adapter.tracks if t.genre and t.id != writable_target.id), None
    )
    if existing_genre:
        again = adapter.set_track_metadata(writable_target.id, {"genre": existing_genre})
        check("an existing lookup value is reused, not re-created",
              again is not None and again.genre == existing_genre,
              str(again.genre if again else None))
    check("an out-of-range rating is rejected",
          _raises(lambda: adapter.set_track_metadata(writable_target.id, {"rating": 9}),
                  InvalidCommand))
    check("an unknown track is a NotFound",
          _raises(lambda: adapter.set_track_metadata("nope", {"title": "x"}), NotFound))
    check("unknown fields are ignored, never guessed at",
          adapter.set_track_metadata(writable_target.id, {"not_a_field": "x"}) is not None)

    print("== hot cue writes ==")
    cue_track = next((t for t in adapter.tracks if t.bpm), adapter.tracks[0])
    tc = adapter.set_cue(cue_track.id, slot=6, start_sec=30.0, cue_type="cue", name="Probe")
    placed = [c for c in tc.cues if c.slot == 6]
    check("the command returns the refreshed cues", len(placed) == 1, str(len(placed)))
    if placed:
        check("the cue is where it was put", abs(placed[0].start - 30.0) < 0.001)
        check("it is a hot cue with a slot", placed[0].role == "hotcue" and placed[0].slot == 6)
        check("its name round-trips", placed[0].name == "Probe", str(placed[0].name))
    check("writing a loop is refused until its slot encoding is known",
          _raises(lambda: adapter.set_cue(cue_track.id, slot=7, start_sec=60.0,
                                          cue_type="loop", length_sec=1.92), Unsupported))
    # Reading one must still work: Rekordbox libraries contain loops and losing
    # them from the projection would be worse than not being able to edit them.
    looped = [
        c
        for t in adapter.tracks[:40]
        for c in (adapter.track_cues(t.id) or TrackCues()).cues
        if c.type == "loop"
    ]
    if looped:
        check("existing loops are still projected, with their length",
              all(c.length > 0 for c in looped), str([c.length for c in looped]))
    # Replacing an occupied slot must not leave two cues in it.
    tc = adapter.set_cue(cue_track.id, slot=6, start_sec=45.0, cue_type="cue")
    in_six = [c for c in tc.cues if c.slot == 6]
    check("re-setting a slot replaces rather than duplicates", len(in_six) == 1, str(len(in_six)))
    if in_six:
        check("and moves it", abs(in_six[0].start - 45.0) < 0.001)
    tc = adapter.delete_cue(cue_track.id, 6)
    check("a deleted slot is empty", not [c for c in tc.cues if c.slot == 6])
    check("deleting an empty slot is NotFound",
          _raises(lambda: adapter.delete_cue(cue_track.id, 6), NotFound))
    check("a Traktor-only cue type is Unsupported",
          _raises(lambda: adapter.set_cue(cue_track.id, slot=2, start_sec=1.0,
                                          cue_type="fade_in"), Unsupported))
    # Rekordbox is the ONLY platform with memory cues, so they stay
    # preserved-but-uneditable under the two-platform promotion rule.
    check("memory cues are shown but not writable",
          _raises(lambda: adapter.set_cue(cue_track.id, slot=1, start_sec=1.0,
                                          cue_type="cue", role="memory"), Unsupported))
    check("slot 0 is rejected — it is the memory-cue encoding, not a bank slot",
          _raises(lambda: adapter.set_cue(cue_track.id, slot=0, start_sec=1.0,
                                          cue_type="cue"), InvalidCommand))
    check("a negative position is rejected",
          _raises(lambda: adapter.set_cue(cue_track.id, slot=2, start_sec=-1.0,
                                          cue_type="cue"), InvalidCommand))

    print("== beatgrid edits ==")
    grid_track = next((t for t in adapter.tracks if t.bpm), None)
    if grid_track:
        check("a gridded track starts with at least one marker",
              len(adapter.track_cues(grid_track.id).grid_markers) >= 1)
        # Put the grid into a KNOWN state first. Reading whatever the library
        # happens to hold made this section depend on the user's own edits — it
        # broke the moment the reference library gained a marker at 60s.
        tc = adapter.replace_grid(grid_track.id, [(0.05, 120.0)])
        check("the grid can be reset to a single known marker",
              len(tc.grid_markers) == 1 and tc.grid_markers[0].bpm == 120.0,
              str([(m.start, m.bpm) for m in tc.grid_markers]))
        tc = adapter.set_grid_marker_bpm(grid_track.id, 0, 130.0)
        check("retempo changes the marker", tc.grid_markers[0].bpm == 130.0,
              str(tc.grid_markers[0].bpm))
        # Rekordbox does NOT reconcile the BPM column with the grid — verified in
        # the real app, where the deck showed the new tempo and the list the old.
        check("the BPM column follows the first marker",
              adapter.track(grid_track.id).bpm == 130.0,
              str(adapter.track(grid_track.id).bpm))
        tc = adapter.add_grid_marker(grid_track.id, 60.0, 140.0)
        check("a second marker makes the grid flexible", len(tc.grid_markers) == 2,
              str(len(tc.grid_markers)))
        check("markers stay ordered by position",
              tc.grid_markers[0].start < tc.grid_markers[1].start)
        check("Rekordbox markers never carry a companion cue",
              all(m.companion is None for m in tc.grid_markers))
        # A move is clamped between neighbours so the list cannot reorder.
        tc = adapter.move_grid_marker(grid_track.id, 1, 0.0)
        check("moving a marker past its neighbour is clamped",
              tc.grid_markers[1].start > tc.grid_markers[0].start,
              f"{tc.grid_markers[0].start} / {tc.grid_markers[1].start}")
        tc = adapter.delete_grid_marker(grid_track.id, 1)
        check("a marker can be deleted", len(tc.grid_markers) == 1, str(len(tc.grid_markers)))
        check("grid_marker_count follows the real grid",
              adapter.track(grid_track.id).grid_marker_count == 1)
        tc = adapter.delete_grid(grid_track.id)
        check("the whole grid can be deleted", tc.grid_markers == [])
        check("and the BPM column is cleared with it",
              adapter.track(grid_track.id).bpm is None,
              str(adapter.track(grid_track.id).bpm))
        tc = adapter.replace_grid(grid_track.id, [(0.05, 128.0)])
        check("replace_grid accepts (start, bpm) tuples from the route layer",
              len(tc.grid_markers) == 1 and tc.grid_markers[0].bpm == 128.0)
        check("a non-positive tempo is rejected",
              _raises(lambda: adapter.set_grid_marker_bpm(grid_track.id, 0, 0), InvalidCommand))
        check("an unknown marker index is NotFound",
              _raises(lambda: adapter.set_grid_marker_bpm(grid_track.id, 9, 120.0), NotFound))
        check("Rekordbox has no grid lock",
              _raises(lambda: adapter.set_grid_lock(grid_track.id, True), Unsupported))

    print("== saving warns but proceeds while Rekordbox is running ==")
    # pyrekordbox's own commit() refuses outright if it sees a Rekordbox process.
    # Konduktor's settled behaviour is to warn, not block — matching Traktor —
    # and the library's check is process-wide, so it would otherwise refuse to
    # write a temp copy while the user had a different library open.
    from pyrekordbox.db6 import database as _rb_database

    _real_pid = _rb_database.get_rekordbox_pid
    _rb_database.get_rekordbox_pid = lambda *a, **kw: 4242  # pretend it is running
    try:
        adapter.set_track_metadata(sample.id, {"title": "Saved While Running"})
        adapter.save()
        check("a save completes with Rekordbox running", True)
    except Exception as ex:  # noqa: BLE001
        check("a save completes with Rekordbox running", False, f"{type(ex).__name__}: {ex}")
    finally:
        _rb_database.get_rekordbox_pid = _real_pid
    check("and the edit really landed",
          adapter.track(sample.id).title == "Saved While Running",
          str(adapter.track(sample.id).title))

    print("== a cloud-synced library is refused permanently, not pending a milestone ==")
    # The one failure mode version history cannot undo: a local edit the sync
    # server did not issue could corrupt the user's OTHER machines. Detection is
    # a server-issued `usn`, so forge one on a throwaway copy and check it flips.
    synced = Path(d) / "synced.db"
    shutil.copy2(work, synced)
    import sqlcipher3.dbapi2 as sqlcipher
    from pyrekordbox.db6.database import BLOB
    from pyrekordbox.utils import deobfuscate

    con = sqlcipher.connect(str(synced))
    con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
    con.execute("UPDATE djmdContent SET usn = 42 WHERE ID = (SELECT ID FROM djmdContent LIMIT 1)")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for ext in ("-wal", "-shm"):
        stale = Path(str(synced) + ext)
        if stale.exists():
            stale.unlink()  # a stale WAL beside a copied .db corrupts it

    cloud = RekordboxAdapter(synced)
    ccaps = cloud.capabilities()
    check("a server-issued usn marks the library cloud-synced", cloud.cloud_synced)
    check("the read-only cause distinguishes it from a missing feature",
          ccaps.readonly_cause == "cloud_synced", str(ccaps.readonly_cause))
    try:
        cloud.set_track_metadata(cloud.tracks[0].id, {"title": "x"})
        check("a cloud-synced library refuses edits", False, "no refusal")
    except Unsupported as ex:
        check("a cloud-synced library refuses edits", True)
        check("and the refusal names the real reason, not the milestone",
              "Cloud" in str(ex), str(ex))

    print("== reads that must still work ==")
    page = adapter.query_tracks(limit=5)
    check("query_tracks paginates", len(page.items) <= 5 and page.total == len(adapter.tracks))
    facets = adapter.facets()
    check("facets come from the generic index", facets.total_tracks == len(adapter.tracks))
    stats = adapter.stats(playlist_count=adapter.playlist_count())
    check("stats come from the generic index", stats.total_tracks == len(adapter.tracks))
    check("audio_path resolves to an OS path",
          adapter.audio_path(sample.id) is None or isinstance(adapter.audio_path(sample.id), Path))
    check("cover_art is absent rather than an error", adapter.cover_art(sample.id) is None)

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
