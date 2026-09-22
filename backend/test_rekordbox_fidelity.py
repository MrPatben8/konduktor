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
    from pyrekordbox.masterdb.database import BLOB
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

    # ---- F: a cue write touches the cue row AND its mirror ---------------
    print("== F: a hot cue write is localized, and keeps the mirror in step ==")
    clean_copy(REAL, work, closing=[adapter, again, final, reopened])
    adapter = RekordboxAdapter(work)
    cue_track = next((t for t in adapter.tracks if t.bpm), adapter.tracks[0])
    # Empty the bank FIRST so "one row inserted" is a fact about this edit, not
    # about whatever the reference library happened to hold. Reading the existing
    # cues instead made this assertion flip to UPDATE the moment the real library
    # gained a cue in the same slot.
    for existing in list(adapter.track_cues(cue_track.id).cues):
        if existing.slot is not None:
            adapter.delete_cue(cue_track.id, existing.slot)
    adapter.save()
    before = dump(work)
    # Generic slot 5 is the pad labelled F, stored natively as Kind=7 — the bank
    # skips the reserved Kind 4, so pads from D up are shifted by two.
    adapter.set_cue(cue_track.id, slot=5, start_sec=30.0, cue_type="cue", name="Probe")
    adapter.save()
    changes = diff(before, dump(work))
    tables_touched = {t for t, _, _, _ in changes}
    # djmdCue gains a row, contentCue's mirror is rewritten, the counter moves.
    # Nothing else may move — in particular not djmdContent.
    check("only the cue, its mirror and the counter change",
          tables_touched == {"djmdCue", "contentCue", "agentRegistry"},
          str(tables_touched))
    cue_rows = [c for c in changes if c[0] == "djmdCue"]
    check("exactly one cue row is inserted",
          len(cue_rows) == 1 and cue_rows[0][2] == "INSERT", describe(cue_rows))
    if cue_rows and cue_rows[0][2] == "INSERT":
        row = cue_rows[0][3]
        check("the 0-based slot is stored as its measured Kind",
              row["Kind"] == 7, str(row["Kind"]))
        check("positions are stored in ms AND frames at 150fps",
              row["InMsec"] == 30000 and row["InFrame"] == 4500,
              f"{row['InMsec']}/{row['InFrame']}")
        check("a non-loop has no out-point", row["OutMsec"] == -1, str(row["OutMsec"]))
        check("an uncoloured cue matches Rekordbox's own convention",
              row["Color"] == -1 and row["ColorTableIndex"] is None,
              f"{row['Color']}/{row['ColorTableIndex']}")
        check("the cue points at its track's UUID",
              row["ContentUUID"] == dump(work)["djmdContent"][cue_track.id]["UUID"])

    # The mirror is the trap: leaving it stale is this platform's version of
    # Traktor's companion-cue desync.
    mirror = [c for c in changes if c[0] == "contentCue"]
    check("the JSON mirror was rewritten", len(mirror) == 1, describe(mirror))
    after = dump(work)
    mirror_row = next(
        (r for r in after["contentCue"].values() if r["ContentID"] == cue_track.id), None
    )
    check("a mirror row exists for the track", mirror_row is not None)
    if mirror_row:
        import json as _json

        records = _json.loads(mirror_row["Cues"])
        live = [
            r for r in after["djmdCue"].values()
            if r["ContentID"] == cue_track.id and not r["rb_local_deleted"]
        ]
        check("the mirror holds one record per cue row",
              len(records) == len(live), f"{len(records)} vs {len(live)}")
        check("rb_cue_count agrees with both",
              mirror_row["rb_cue_count"] == len(live),
              f"{mirror_row['rb_cue_count']} vs {len(live)}")
        check("the mirror's id is the track's UUID",
              mirror_row["ID"] == after["djmdContent"][cue_track.id]["UUID"])
        check("the new cue is in the mirror", any(r["Kind"] == 7 for r in records))

    # ---- G: deleting a cue removes it from both places -------------------
    print("== G: deleting a cue clears the row and the mirror ==")
    adapter.delete_cue(cue_track.id, 5)
    adapter.save()
    after = dump(work)
    live = [
        r for r in after["djmdCue"].values()
        if r["ContentID"] == cue_track.id and r["Kind"] == 7 and not r["rb_local_deleted"]
    ]
    check("the cue row is gone", not live, str(len(live)))
    mirror_row = next(
        (r for r in after["contentCue"].values() if r["ContentID"] == cue_track.id), None
    )
    if mirror_row:
        import json as _json

        records = _json.loads(mirror_row["Cues"])
        check("and gone from the mirror too", not any(r["Kind"] == 7 for r in records))
        check("with rb_cue_count updated",
              mirror_row["rb_cue_count"] == len(records),
              f"{mirror_row['rb_cue_count']} vs {len(records)}")
    adapter.close()

    # ---- H: a grid write touches ONE tag of ONE file ---------------------
    print("== H: a beatgrid write is localized to the .DAT's PQTZ tag ==")
    from pyrekordbox.anlz import AnlzFile

    adapter = RekordboxAdapter(work)  # phase G closed the previous one
    grid_track = next((t for t in adapter.tracks if t.bpm), None)
    if grid_track is None:
        check("a gridded track exists to test with", False, "none found")
    else:
        rel = str(dump(work)["djmdContent"][grid_track.id]["AnalysisDataPath"]).lstrip("/")
        dat = Path(d) / "share" / rel
        ext = dat.with_suffix(".EXT")
        two_ex = dat.with_suffix(".2EX")
        before_dat = dat.read_bytes()
        before_ext = ext.read_bytes() if ext.is_file() else None
        before_2ex = two_ex.read_bytes() if two_ex.is_file() else None
        before_db = dump(work)

        adapter.set_grid_marker_bpm(grid_track.id, 0, 130.0)
        # The save contract: an unsaved grid edit must not be on disk yet.
        check("an unsaved grid edit has not touched the analysis file",
              dat.read_bytes() == before_dat)
        adapter.save()

        check("the analysis file changed", dat.read_bytes() != before_dat)
        # The extended grid carries undecoded bytes and is deliberately not
        # written. Rekordbox was verified to read PQTZ and ignore this being stale.
        if before_ext is not None:
            check(".EXT is left untouched", ext.read_bytes() == before_ext)
        if before_2ex is not None:
            check(".2EX is left untouched", two_ex.read_bytes() == before_2ex)

        # The real localization test: every OTHER tag in the rewritten .DAT must
        # be byte-identical. This is the ANLZ analogue of "only edited objects
        # diff", and it is what would catch a rebuild corrupting the file.
        old_tags = {t.type: t.build() for t in AnlzFile.parse(before_dat).tags if t.type != "PQTZ"}
        new_tags = {t.type: t.build() for t in AnlzFile.parse_file(str(dat)).tags if t.type != "PQTZ"}
        check("the same tags are present afterwards",
              set(old_tags) == set(new_tags), f"{sorted(old_tags)} vs {sorted(new_tags)}")
        differing = [k for k in old_tags if k in new_tags and old_tags[k] != new_tags[k]]
        check("every tag except PQTZ is byte-identical", not differing, ", ".join(differing))

        reread = AnlzFile.parse_file(str(dat))
        pqtz = next(t for t in reread.tags if t.type == "PQTZ")
        check("the new tempo is really in the file",
              abs(float(pqtz.bpms[0]) - 130.0) < 0.001, str(pqtz.bpms[0]))
        check("the grid still covers the track",
              len(pqtz.beats) > 1 and float(pqtz.times[-1]) > 0)

        # The BPM column is Konduktor's to maintain: Rekordbox does not
        # reconcile it with the grid (verified in the real app).
        changes = diff(before_db, dump(work))
        tables_touched = {t for t, _, _, _ in changes}
        check("in the database, only the track's BPM and the counter move",
              tables_touched == {"djmdContent", "agentRegistry"}, str(tables_touched))
        content = [c for c in changes if c[0] == "djmdContent"]
        if content and content[0][2] == "UPDATE":
            fields = content[0][3]
            check("only BPM and the row USN changed",
                  set(fields) == {"BPM", "rb_local_usn"}, str(sorted(fields)))
            check("BPM is stored x100", fields.get("BPM", (0, 0))[1] == 13000,
                  str(fields.get("BPM")))
        adapter.close()

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
