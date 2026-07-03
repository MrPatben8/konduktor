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

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
