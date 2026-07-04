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

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
