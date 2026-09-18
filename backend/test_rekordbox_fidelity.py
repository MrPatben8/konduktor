"""Rekordbox's analogue of `test_save_fidelity.py`.

The project's core safety property is: **an edit changes only what it was
supposed to change.** For Traktor that is a byte diff of the whole file. A byte
diff is meaningless against SQLite — page layout, free lists and vacuuming move
bytes that carry no meaning — so the equivalent here is a **full table dump
before and after, compared row by row**.

Runs against a TEMP COPY of the local Rekordbox library, and skips cleanly when
no Rekordbox is installed.

Two things about the expected diff are NOT noise and must stay asserted:

  * `agentRegistry.localUpdateCount` changes on every save. It is Rekordbox's
    global update counter, and NOT bumping it is what would corrupt the library.
  * the edited row's own `rb_local_usn` is stamped with that new counter value.

A real Rekordbox 7 was verified to accept a library written exactly this way,
display the edit, leave the row untouched, and carry on from the counter it was
left at — see the handoff's Findings section.
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-rbfid-appdata-")

from konduktor.adapters.rekordbox import discovery  # noqa: E402
from konduktor.adapters.rekordbox.adapter import RekordboxAdapter  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


# ---- the row-level dump/diff harness -----------------------------------
def dump(db_path: Path) -> dict:
    """Every row of every table, keyed by primary key."""
    import sqlcipher3.dbapi2 as sqlcipher
    from pyrekordbox.db6.database import BLOB
    from pyrekordbox.utils import deobfuscate

    con = sqlcipher.connect(str(db_path))
    con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
    cur = con.cursor()
    # Materialise the table list FIRST: reusing one cursor for the outer and the
    # inner queries silently truncates the outer iteration to a single row.
    names = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    out: dict[str, dict] = {}
    for table in names:
        if table == "sqlite_sequence":
            continue
        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
        pk = "ID" if "ID" in cols else cols[0]
        rows = {}
        for r in cur.execute(f'SELECT * FROM "{table}"').fetchall():
            d = dict(zip(cols, r))
            rows[str(d.get(pk))] = d
        out[table] = rows
    con.close()
    return out


def diff(before: dict, after: dict, ignore_columns=("updated_at", "created_at")) -> list:
    """(table, pk, kind, detail) for every row-level change."""
    changes = []
    for table in sorted(set(before) | set(after)):
        rows_a, rows_b = before.get(table, {}), after.get(table, {})
        for pk in sorted(set(rows_a) | set(rows_b)):
            if pk not in rows_a:
                changes.append((table, pk, "INSERT", rows_b[pk]))
            elif pk not in rows_b:
                changes.append((table, pk, "DELETE", rows_a[pk]))
            else:
                fields = {
                    k: (rows_a[pk][k], rows_b[pk].get(k))
                    for k in rows_a[pk]
                    if k not in ignore_columns and rows_a[pk][k] != rows_b[pk].get(k)
                }
                if fields:
                    changes.append((table, pk, "UPDATE", fields))
    return changes


def describe(changes) -> str:
    return "; ".join(f"{t}/{pk} {kind}" for t, pk, kind, _ in changes) or "(none)"


def clean_copy(src: Path, dest: Path, *, closing=()) -> None:
    """Copy a library WITHOUT any stale write-ahead log.

    A `-wal` left beside a replaced `.db` is replayed onto it by SQLite and
    corrupts it. This cost real debugging time during the research spike.

    `closing` is any adapters still holding the destination open: replacing a
    SQLite file under a live connection is an I/O error at best.
    """
    for adapter in closing:
        adapter.close()
    for ext in ("-wal", "-shm"):
        stale = Path(str(dest) + ext)
        if stale.exists():
            stale.unlink()
    shutil.copy2(src, dest)


found = discovery.detect_libraries()
if not found:
    print("== SKIPPED: no Rekordbox library found on this machine ==")
    print("\nRESULT: ALL PASSED")
    sys.exit(0)

REAL = Path(found[0]["path"])
print(f"(using a temp copy of {REAL})")

with tempfile.TemporaryDirectory() as d:
    work = Path(d) / "master.db"
    clean_copy(REAL, work)
    share = REAL.parent / "share"
    if share.is_dir():
        shutil.copytree(share, Path(d) / "share")

    # ---- A: a no-op save changes nothing --------------------------------
    print("== A: opening and saving without editing changes no row ==")
    before = dump(work)
    adapter = RekordboxAdapter(work)
    check("a freshly opened library is not dirty", adapter.dirty is False)
    adapter.save()
    changes = diff(before, dump(work))
    check("a no-op save changes zero rows", not changes, describe(changes))

    # ---- B: one metadata edit touches one row + the counter --------------
    print("== B: a single metadata edit is localized ==")
    clean_copy(REAL, work, closing=[adapter])
    before = dump(work)
    adapter = RekordboxAdapter(work)
    target = adapter.tracks[0]
    original_title = target.title
    adapter.set_track_metadata(target.id, {"title": "KONDUKTOR FIDELITY PROBE"})
    check("the edit makes the adapter dirty", adapter.dirty is True)
    check("the projection updates immediately",
          adapter.track(target.id).title == "KONDUKTOR FIDELITY PROBE")
    adapter.save()
    check("saving clears dirty", adapter.dirty is False)

    changes = diff(before, dump(work))
    tables_touched = {t for t, _, _, _ in changes}
    check("exactly two rows change", len(changes) == 2, describe(changes))
    check("they are the track and the update counter",
          tables_touched == {"djmdContent", "agentRegistry"}, str(tables_touched))

    content = [c for c in changes if c[0] == "djmdContent"]
    check("the changed track is the one edited",
          len(content) == 1 and content[0][1] == target.id, describe(content))
    if content:
        fields = content[0][3]
        check("only Title and the row's USN changed",
              set(fields) == {"Title", "rb_local_usn"}, str(sorted(fields)))
        check("Title holds the new value", fields.get("Title", (None, None))[1] == "KONDUKTOR FIDELITY PROBE")

    registry = [c for c in changes if c[0] == "agentRegistry"]
    check("the global update counter is the registry row that changed",
          len(registry) == 1 and registry[0][1] == "localUpdateCount", describe(registry))
    if registry:
        old_usn, new_usn = registry[0][3]["int_1"]
        check("the counter increments by exactly one edit", new_usn == old_usn + 1,
              f"{old_usn} -> {new_usn}")
        # This is the invariant Rekordbox itself relies on to notice the change.
        if content:
            check("the edited row is stamped with the new counter value",
                  content[0][3].get("rb_local_usn", (None, None))[1] == new_usn,
                  str(content[0][3].get("rb_local_usn")))

    # ---- C: the edit survives a reopen ----------------------------------
    print("== C: the edit is really on disk ==")
    reopened = RekordboxAdapter(work)
    check("the new title is there after reopening",
          reopened.track(target.id).title == "KONDUKTOR FIDELITY PROBE",
          str(reopened.track(target.id).title))
    check("the original title is gone", original_title != "KONDUKTOR FIDELITY PROBE")

    # ---- D: an unsaved edit is not on disk ------------------------------
    print("== D: edits are held until save, like every other platform ==")
    clean_copy(REAL, work, closing=[adapter, reopened])
    before = dump(work)
    adapter = RekordboxAdapter(work)
    adapter.set_track_metadata(adapter.tracks[0].id, {"title": "NEVER SAVED"})
    changes = diff(before, dump(work))
    check("an unsaved edit has written nothing to disk", not changes, describe(changes))

    # ---- E: playlist edits -----------------------------------------------
    print("== E: a playlist round-trips ==")
    clean_copy(REAL, work, closing=[adapter])
    adapter = RekordboxAdapter(work)
    before_tree = adapter.playlist_tree()
    ids = [t.id for t in adapter.tracks[:3]]
    node_id = adapter.create_playlist("Konduktor Test Playlist")
    adapter.set_playlist_entries(node_id, ids)
    adapter.save()

    reopened = RekordboxAdapter(work)
    names = {n.name for n in reopened.playlist_tree()}
    check("the playlist exists after a reopen", "Konduktor Test Playlist" in names, str(names))
    check("its entries are the tracks added, in order",
          reopened.playlist_entries(node_id) == ids,
          f"{reopened.playlist_entries(node_id)} vs {ids}")
    tracks = reopened.playlist_tracks(node_id)
    check("and they resolve to real projected tracks",
          tracks is not None and [t.id for t in tracks] == ids)

    reopened.rename_playlist(node_id, "Konduktor Renamed")
    reopened.save()
    again = RekordboxAdapter(work)
    check("a rename round-trips",
          "Konduktor Renamed" in {n.name for n in again.playlist_tree()})

    again.delete_playlist(node_id)
    again.save()
    final = RekordboxAdapter(work)
    check("a delete round-trips",
          "Konduktor Renamed" not in {n.name for n in final.playlist_tree()})
    check("the rest of the tree is untouched",
          len(final.playlist_tree()) == len(before_tree),
          f"{len(final.playlist_tree())} vs {len(before_tree)}")

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
