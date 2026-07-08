"""Regression guard for save fidelity — keeps playlist diffs minimal.

This is the test that would have caught the lxml reformatting bug. It asserts
two invariants against a COPY of the real collection:

  A. NO-OP SAVE IS BYTE-IDENTICAL.
     Loading and saving with zero edits must reproduce the file exactly. Any
     serialization change (lxml, a different render pipeline, dropped
     float-format/layout post-processing) breaks this immediately.

  B. AN EDIT IS FULLY LOCALIZED.
     Editing one playlist must change ONLY that playlist's block. Everything
     else in the file — other playlists, folders, smart playlists, and the
     entire COLLECTION — must stay byte-for-byte identical.

Run: `python test_save_fidelity.py` in the backend venv (see run_tests.sh).
"""
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"

from konduktor.collection_service import CollectionService  # noqa: E402
from konduktor.playlist_store import PlaylistStore  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def playlist_block_span(data: bytes, uuid: str):
    """Byte span of a playlist's <PLAYLIST …UUID="…">…</PLAYLIST> block."""
    i = data.find(f'UUID="{uuid}"'.encode())
    start = data.rfind(b"<PLAYLIST ", 0, i)
    end = data.find(b"</PLAYLIST>", i) + len(b"</PLAYLIST>")
    return start, end


def tag_lines(b: bytes) -> list[str]:
    return b.replace(b"><", b">\n<").decode("utf-8", "replace").splitlines()


def diff_count(a: bytes, b: bytes) -> int:
    import difflib

    la, lb = tag_lines(a), tag_lines(b)
    return sum(
        1
        for d in difflib.unified_diff(la, lb, n=0)
        if d[:1] in "+-" and not d.startswith(("+++", "---"))
    )


# ---- Invariant A: no-op save is byte-identical ------------------------
print("== A. no-op save is byte-identical ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    before = work.read_bytes()
    store = PlaylistStore(work)
    store.save()  # no edits
    after = work.read_bytes()
    check("whole file identical after no-op save", before == after)
    if before != after:
        for i, (x, y) in enumerate(zip(before, after)):
            if x != y:
                check.detail = i
                print(f"    first byte diff @ {i}")
                print("    before:", before[max(0, i - 50) : i + 50])
                print("    after :", after[max(0, i - 50) : i + 50])
                break

# ---- Invariant B: an edit is fully localized --------------------------
print("== B. edit touches only the edited playlist ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()

    svc = CollectionService(work)
    store = PlaylistStore(work)

    def walk(n):
        yield n
        if n.subnodes:
            for c in n.subnodes.node:
                yield from walk(c)

    target = next(
        n for n in walk(store._root()) if n.playlist and n.playlist.uuid and len(n.playlist.entry or []) >= 5
    )
    uuid = target.playlist.uuid
    keys = store.entry_keys(uuid)

    store.set_entries(uuid, svc.entries_for(list(reversed(keys))))
    store.save()
    edited = work.read_bytes()

    check("file actually changed", original != edited)

    # Blank out the edited playlist's block in both; the remainder must match.
    os_, oe = playlist_block_span(original, uuid)
    es_, ee = playlist_block_span(edited, uuid)
    rest_original = original[:os_] + b"@@BLOCK@@" + original[oe:]
    rest_edited = edited[:es_] + b"@@BLOCK@@" + edited[ee:]
    check(
        "everything outside the edited playlist is byte-identical",
        rest_original == rest_edited,
    )

    # A pure reorder must preserve the exact set of lines in the block (only
    # their order changes). If the block were reformatted, the multiset differs.
    before_lines = sorted(tag_lines(original[os_:oe]))
    after_lines = sorted(tag_lines(edited[es_:ee]))
    check(
        "edited block is a pure reorder (line multiset unchanged, no reformat)",
        before_lines == after_lines,
    )
    n = diff_count(original[os_:oe], edited[es_:ee])
    print(f"    edited-block changed lines: {n} (reordered {len(keys)} entries)")

# ---- Invariant C: a track-metadata edit is fully localized -------------
print("== C. track metadata edit touches only that track's ENTRY ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()

    store = PlaylistStore(work)
    # pick the first track that has a resolvable primary key
    entry = store._nml.collection.entry[0]
    loc = entry.location
    track_id = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"

    store.set_track_metadata(track_id, {"genre": "KONDUKTOR-FIDELITY-TEST", "rating": 4})
    store.save()
    edited = work.read_bytes()

    check("file actually changed", original != edited)

    # The edited ENTRY's <INFO> block is where the change lands. Blank out the
    # whole entry span in both and require the remainder to be byte-identical.
    def entry_span(data: bytes, file_name: str):
        marker = f'FILE="{file_name}"'.encode()
        i = data.find(marker)
        start = data.rfind(b"<ENTRY ", 0, i)
        end = data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
        return start, end

    os_, oe = entry_span(original, loc.file)
    es_, ee = entry_span(edited, loc.file)
    check(
        "everything outside the edited track's ENTRY is byte-identical",
        original[:os_] + b"@@" + original[oe:] == edited[:es_] + b"@@" + edited[ee:],
    )
    n = diff_count(original[os_:oe], edited[es_:ee])
    check("edited ENTRY diff is tiny (<= 6 tag-lines)", n <= 6, f"got {n}")
    print(f"    edited-ENTRY changed tag-lines: {n}")

    # Re-parses and the edit persisted.
    from traktor_nml_utils import TraktorCollection

    reparsed = TraktorCollection(path=work)
    e0 = reparsed.nml.collection.entry[0]
    check("re-parses + genre persisted", e0.info.genre == "KONDUKTOR-FIDELITY-TEST")
    check("rating persisted (4 stars => RANKING 204)", e0.info.ranking == 204)

# ---- Invariant D: a hotcue edit is fully localized ---------------------
print("== D. hotcue create touches only that track's ENTRY ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()

    store = PlaylistStore(work)
    entry = store._nml.collection.entry[0]
    loc = entry.location
    track_id = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"

    # Use a free slot so we exercise the create path (not overwrite an existing).
    used = {c.hotcue for c in (entry.cue_v2 or []) if c.hotcue is not None}
    slot = next(s for s in range(8) if s not in used)
    store.set_hotcue(track_id, slot, 42.5, 0)
    store.save()
    edited = work.read_bytes()

    check("file actually changed", original != edited)

    def entry_span(data: bytes, file_name: str):
        marker = f'FILE="{file_name}"'.encode()
        i = data.find(marker)
        start = data.rfind(b"<ENTRY ", 0, i)
        end = data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
        return start, end

    os_, oe = entry_span(original, loc.file)
    es_, ee = entry_span(edited, loc.file)
    check(
        "everything outside the edited track's ENTRY is byte-identical",
        original[:os_] + b"@@" + original[oe:] == edited[:es_] + b"@@" + edited[ee:],
    )
    n = diff_count(original[os_:oe], edited[es_:ee])
    check("edited ENTRY diff is tiny (<= 3 tag-lines)", n <= 3, f"got {n}")

    from traktor_nml_utils import TraktorCollection

    reparsed = TraktorCollection(path=work)
    e0 = reparsed.nml.collection.entry[0]
    new_cue = next((c for c in (e0.cue_v2 or []) if c.hotcue == slot), None)
    check("re-parses + hotcue persisted", new_cue is not None)
    check("hotcue START stored in ms", new_cue is not None and abs(new_cue.start - 42500.0) < 1)

    # Create then delete must round-trip back to a byte-identical file.
    store2 = PlaylistStore(work)  # reload the edited file
    store2.delete_hotcue(track_id, slot)
    store2.save()
    check("create+delete round-trips to the original bytes", work.read_bytes() == original)

# ---- Invariant E: a beatgrid edit is fully localized -------------------
print("== E. grid/bpm edit touches only that track's ENTRY ==")
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()

    store = PlaylistStore(work)
    # first entry that has both a tempo and a grid marker
    entry = next(
        e
        for e in store._nml.collection.entry
        if e.tempo and store._grid_marker(e) is not None
    )
    loc = entry.location
    track_id = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"
    orig_bpm = entry.tempo.bpm

    store.set_grid(track_id, bpm=orig_bpm / 2, anchor_sec=1.5)
    store.save()
    edited = work.read_bytes()

    check("file actually changed", original != edited)

    def entry_span(data: bytes, file_name: str):
        marker = f'FILE="{file_name}"'.encode()
        i = data.find(marker)
        start = data.rfind(b"<ENTRY ", 0, i)
        end = data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
        return start, end

    os_, oe = entry_span(original, loc.file)
    es_, ee = entry_span(edited, loc.file)
    check(
        "everything outside the edited track's ENTRY is byte-identical",
        original[:os_] + b"@@" + original[oe:] == edited[:es_] + b"@@" + edited[ee:],
    )

    from traktor_nml_utils import TraktorCollection

    reparsed = TraktorCollection(path=work)
    e0 = next(
        e
        for e in reparsed.nml.collection.entry
        if e.location
        and f"{e.location.volume or ''}{e.location.dir or ''}{e.location.file or ''}" == track_id
    )
    m = store._grid_marker(e0)
    check("tempo BPM halved + persisted", abs(e0.tempo.bpm - orig_bpm / 2) < 1e-3)
    check("grid marker BPM matches tempo", m is not None and abs(m.grid.bpm - orig_bpm / 2) < 1e-3)
    check("grid anchor moved to 1.5s (1500 ms)", m is not None and abs(m.start - 1500.0) < 1)

    # Reverting BPM + anchor to the original values round-trips to the file.
    orig_anchor_ms = store._grid_marker(
        next(
            e
            for e in TraktorCollection(path=REAL).nml.collection.entry
            if e.location
            and f"{e.location.volume or ''}{e.location.dir or ''}{e.location.file or ''}" == track_id
        )
    ).start
    store2 = PlaylistStore(work)
    store2.set_grid(track_id, bpm=orig_bpm, anchor_sec=orig_anchor_ms / 1000.0)
    store2.save()
    check("revert BPM + anchor round-trips to original bytes", work.read_bytes() == original)

# ---- Invariant F: batch place_hotcues (Auto Hotcues) -------------------
print("== F. place_hotcues fills empty slots only, names round-trip, localized ==")
with tempfile.TemporaryDirectory() as d:
    from konduktor.schemas import AutoHotcue
    from traktor_nml_utils import TraktorCollection

    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)

    store = PlaylistStore(work)
    entry = store._nml.collection.entry[0]
    loc = entry.location
    track_id = f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"

    def entry_span(data: bytes, file_name: str):
        marker = f'FILE="{file_name}"'.encode()
        i = data.find(marker)
        start = data.rfind(b"<ENTRY ", 0, i)
        end = data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
        return start, end

    # Establish a hand-placed hotcue as the baseline the batch must NOT overwrite.
    used = {c.hotcue for c in (entry.cue_v2 or []) if c.hotcue is not None and c.hotcue >= 0}
    hand_slot = next(s for s in range(8) if s not in used)
    store.set_hotcue(track_id, hand_slot, 12.0, 0, name="HAND-PLACED")
    store.save()
    original = work.read_bytes()

    # Batch: one spec targets the occupied slot (must be skipped), two fill free slots.
    store2 = PlaylistStore(work)
    e2 = store2._nml.collection.entry[0]
    now_used = {c.hotcue for c in (e2.cue_v2 or []) if c.hotcue is not None and c.hotcue >= 0}
    free = [s for s in range(8) if s not in now_used][:2]
    specs = [AutoHotcue(slot=hand_slot, start=99.0, name="SHOULD-NOT-OVERWRITE")]
    specs += [AutoHotcue(slot=s, start=30.0 + i * 10, name=f"Auto {i}") for i, s in enumerate(free)]
    store2.place_hotcues(track_id, specs)
    store2.save()
    edited = work.read_bytes()

    check("file actually changed", original != edited)

    os_, oe = entry_span(original, loc.file)
    es_, ee = entry_span(edited, loc.file)
    check(
        "everything outside the edited track's ENTRY is byte-identical",
        original[:os_] + b"@@" + original[oe:] == edited[:es_] + b"@@" + edited[ee:],
    )
    n = diff_count(original[os_:oe], edited[es_:ee])
    check(f"edited ENTRY diff is small (<= 6 tag-lines for {len(free)} cues)", n <= 6, f"got {n}")

    reparsed = TraktorCollection(path=work)
    e0 = reparsed.nml.collection.entry[0]
    hand = next((c for c in (e0.cue_v2 or []) if c.hotcue == hand_slot), None)
    check("hand-placed hotcue NOT overwritten (position)", hand is not None and abs(hand.start - 12000.0) < 1)
    check("hand-placed hotcue NOT overwritten (name)", hand is not None and hand.name == "HAND-PLACED")
    for i, s in enumerate(free):
        c = next((c for c in (e0.cue_v2 or []) if c.hotcue == s), None)
        check(f"auto hotcue slot {s} created", c is not None)
        check(f"auto hotcue slot {s} name round-trips", c is not None and c.name == f"Auto {i}")
        check(
            f"auto hotcue slot {s} START in ms",
            c is not None and abs(c.start - (30000.0 + i * 10000)) < 1,
        )

# ---- Invariant G: select_hotcues placement logic (pure, no audio) ------
print("== G. select_hotcues: phrase-snap, empty-slots, names, existing-cue avoidance ==")
from konduktor import auto_hotcues as ah  # noqa: E402

# 128 BPM => beat 0.46875s, 16-bar phrase = 30.0s exactly; anchor 0.0.
BPM, ANCHOR, DUR = 128.0, 0.0, 300.0
# Raw boundaries near (but not exactly on) phrase multiples + a fragmented pair.
raw = [(0.3, 0.2), (29.4, 0.5), (30.6, 0.55), (61.0, 0.9), (150.0, 0.3), (270.2, 0.25)]
specs = ah.select_hotcues(
    raw, bpm=BPM, anchor=ANCHOR, duration=DUR,
    free_slots=[2, 3, 4, 5, 6, 7], existing_times=[], max_cues=8,
)
starts = [s["start"] for s in specs]
check("snaps to 30s phrase grid", all(abs(round(t / 30.0) * 30.0 - t) < 0.01 for t in starts), str(starts))
check("fragmented 29.4/30.6 merge to one boundary (30.0)", starts.count(30.0) == 1, str(starts))
check("only free slots used, in ascending order", [s["slot"] for s in specs] == sorted(s["slot"] for s in specs) and set(s["slot"] for s in specs) <= {2, 3, 4, 5, 6, 7})
check("first cue named Intro", specs and specs[0]["name"] == "Intro")
check("last cue named Outro", specs and specs[-1]["name"] == "Outro")
check("names are unique (ordinal de-dupe)", len({s["name"] for s in specs}) == len(specs), str([s["name"] for s in specs]))

# A hotcue already sitting on the 60s phrase must not get a duplicate.
specs2 = ah.select_hotcues(
    raw, bpm=BPM, anchor=ANCHOR, duration=DUR,
    free_slots=[0, 1, 2, 3, 4, 5, 6, 7], existing_times=[60.0], max_cues=8,
)
check("boundary colliding with an existing hotcue is dropped", 60.0 not in [s["start"] for s in specs2], str([s["start"] for s in specs2]))

# No free slots => nothing placed.
check("no free slots => empty result", ah.select_hotcues(raw, bpm=BPM, anchor=ANCHOR, duration=DUR, free_slots=[], existing_times=[], max_cues=8) == [])

# ---- Invariant H: path write-back is localized + round-trips -----------
print("== H. remap_locations rewrites LOCATIONs (+ playlist keys), localized, round-trips ==")
from collections import Counter  # noqa: E402

from konduktor.file_tags import resolve_path  # noqa: E402
from konduktor.path_mapping import PathMapping, os_path_to_location  # noqa: E402
from traktor_nml_utils import TraktorCollection  # noqa: E402


def _key(loc):
    return f"{loc.volume or ''}{loc.dir or ''}{loc.file or ''}"


def _entry_span_by_file(data: bytes, file_name: str):
    marker = f'FILE="{file_name}"'.encode()
    i = data.find(marker)
    start = data.rfind(b"<ENTRY ", 0, i)
    end = data.find(b"</ENTRY>", i) + len(b"</ENTRY>")
    return start, end


# H1: a track NOT in any playlist -> only its own <ENTRY> changes; revert round-trips.
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)
    original = work.read_bytes()

    store = PlaylistStore(work)
    pl_keys = {
        k
        for n in store._iter_nodes(store._root())
        if n.playlist
        for k in store._entry_keys_of(n.playlist)
    }
    file_counts = Counter(
        e.location.file for e in store._nml.collection.entry if e.location and e.location.file
    )
    target = next(
        e
        for e in store._nml.collection.entry
        if e.location
        and _key(e.location) not in pl_keys
        and file_counts[e.location.file] == 1  # unique filename → clean span masking
    )
    loc = target.location
    old_file = loc.file
    from_prefix = str(resolve_path(loc.volume, loc.dir, loc.file))
    to_prefix = "/Volumes/KONDUKTOR_TEST_H1/remapped__unique__one.mp3"

    n = store.remap_locations(PathMapping.make(from_prefix, to_prefix))
    store.save()
    edited = work.read_bytes()

    check("H1: exactly one track rewritten", n == 1, f"got {n}")
    check("H1: file actually changed", original != edited)

    os_, oe = _entry_span_by_file(original, old_file)
    es_, ee = _entry_span_by_file(edited, "remapped__unique__one.mp3")
    check(
        "H1: everything outside the moved ENTRY is byte-identical",
        original[:os_] + b"@@" + original[oe:] == edited[:es_] + b"@@" + edited[ee:],
    )

    reparsed = TraktorCollection(path=work)
    moved = next(
        e
        for e in reparsed.nml.collection.entry
        if e.location and e.location.file == "remapped__unique__one.mp3"
    )
    check("H1: moved ENTRY has the new VOLUME", moved.location.volume == "KONDUKTOR_TEST_H1")

    # Reverting (to -> from) round-trips to the original bytes exactly.
    store2 = PlaylistStore(work)
    store2.remap_locations(PathMapping.make(to_prefix, from_prefix))
    store2.save()
    check("H1: revert round-trips to original bytes", work.read_bytes() == original)

# H2: a track that IS in a playlist -> its PRIMARYKEY is rewritten too.
with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "collection.nml"
    shutil.copy2(REAL, work)

    store = PlaylistStore(work)
    # A collection entry whose key appears in at least one playlist.
    pl_keys = {
        k
        for node in store._iter_nodes(store._root())
        if node.playlist
        for k in store._entry_keys_of(node.playlist)
    }
    target = next(
        e for e in store._nml.collection.entry if e.location and _key(e.location) in pl_keys
    )
    loc = target.location
    old_key = _key(loc)
    from_prefix = str(resolve_path(loc.volume, loc.dir, loc.file))
    to_prefix = "/Volumes/KONDUKTOR_TEST_H2/remapped__playlist__one.mp3"
    new_key = "".join(os_path_to_location(Path(to_prefix)))

    n = store.remap_locations(PathMapping.make(from_prefix, to_prefix))
    store.save()

    check("H2: at least one track rewritten", n >= 1)

    reparsed = TraktorCollection(path=work)
    moved = next(
        (
            e
            for e in reparsed.nml.collection.entry
            if e.location and e.location.file == "remapped__playlist__one.mp3"
        ),
        None,
    )
    check("H2: collection ENTRY location rewritten", moved is not None)

    store3 = PlaylistStore(work)
    all_pl_keys = {
        k
        for node in store3._iter_nodes(store3._root())
        if node.playlist
        for k in store3._entry_keys_of(node.playlist)
    }
    check("H2: old playlist PRIMARYKEY gone", old_key not in all_pl_keys)
    check("H2: new playlist PRIMARYKEY present", new_key in all_pl_keys)


print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
