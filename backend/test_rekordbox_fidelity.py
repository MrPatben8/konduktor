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

    # ---- D2: the same for a playlist that already has entries -------------
    # pyrekordbox's `remove_from_playlist` COMMITS, so replacing a non-empty
    # list once wrote to disk at once — and raised while Rekordbox was running.
    print("== D2: replacing a playlist's entries is held until save too ==")
    from pyrekordbox.masterdb import database as rb_database  # noqa: E402

    clean_copy(REAL, work, closing=[adapter])
    before = dump(work)
    adapter = RekordboxAdapter(work)
    filled = next((p for p in adapter.playlist_tree()
                   if p.kind == "playlist" and len(adapter.playlist_entries(p.id) or []) > 1),
                  None)
    if filled is None:
        print("  (skipped: no playlist with two entries)")
    else:
        original = adapter.playlist_entries(filled.id)
        edited = list(reversed(original))[:-1]  # reorder AND drop one
        adapter.set_playlist_entries(filled.id, edited)
        check("the projection shows the new order",
              adapter.playlist_entries(filled.id) == edited)
        changes = diff(before, dump(work))
        check("nothing is on disk before save", not changes, describe(changes))
        adapter.reload()
        check("discarding restores the original entries",
              adapter.playlist_entries(filled.id) == original)
        real_probe = rb_database.get_rekordbox_pid
        rb_database.get_rekordbox_pid = lambda: 4242  # "Rekordbox is running"
        try:
            adapter.set_playlist_entries(filled.id, edited)
            check("an edit while Rekordbox runs does not fail", True)
        except Exception as ex:  # noqa: BLE001
            check("an edit while Rekordbox runs does not fail", False, repr(ex))
        finally:
            rb_database.get_rekordbox_pid = real_probe
        adapter.reload()
        adapter.set_playlist_entries(filled.id, edited)
        adapter.save()
        adapter.close()
        adapter = RekordboxAdapter(work)
        check("after save the new entries are on disk",
              adapter.playlist_entries(filled.id) == edited)
        changes = diff(before, dump(work))
        check("and nothing outside the playlist's rows and the counter changed",
              {t for t, _, _, _ in changes} <= {"djmdSongPlaylist", "agentRegistry"},
              describe(changes))

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

    # A folder, a playlist inside it, and deleting the folder takes both.
    folder_id = final.create_folder("Konduktor Test Folder")
    inner_id = final.create_playlist("Konduktor Inner", folder_id)
    final.save()
    folder_reopened = RekordboxAdapter(work)
    folder = next((n for n in folder_reopened.playlist_tree() if n.id == folder_id), None)
    check("a folder round-trips as a folder",
          folder is not None and folder.kind == "folder")
    check("a playlist created inside it is its child",
          folder is not None and [c.id for c in folder.children] == [inner_id])
    folder_reopened.delete_playlist(folder_id)
    folder_reopened.save()
    after_folder = RekordboxAdapter(work)
    flat_ids = lambda ns: [x for n in ns for x in [n.id] + flat_ids(n.children)]
    check("deleting the folder removes it AND its playlist",
          not {folder_id, inner_id} & set(flat_ids(after_folder.playlist_tree())))
    folder_reopened.close()
    after_folder.close()

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
        # On rekordbox's clock: 30.000 s of DECODED audio is stored 1105 samples
        # later on an MP3/AAC (`timebase`), and exactly at 30.000 s lossless.
        from konduktor.adapters.rekordbox import timebase
        want_ms = int(round((30.0 + adapter._store.time_offset(cue_track.id)) * 1000))
        check("positions are stored in ms AND frames at 150fps, on rekordbox's clock",
              row["InMsec"] == want_ms and row["InFrame"] == int(want_ms * 150 / 1000),
              f"{row['InMsec']}/{row['InFrame']} (want {want_ms})")
        check("and read back at exactly the position that was set",
              abs(next(c.start for c in adapter.track_cues(cue_track.id).cues if c.slot == 5)
                  - 30.0) < 0.0005)
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

    # ---- I: removing a track does what Rekordbox 7 itself does -----------
    # Measured 2026-10-01 by removing a track in Rekordbox and diffing: its
    # rows are DELETED (not flagged) from djmdContent, djmdCue, contentCue,
    # contentFile, djmdMixerParam and djmdSongPlaylist, the playlist row is
    # left alone, and the files contentFile lists go with their folders.
    print("== I: removing a track deletes exactly what Rekordbox deletes ==")
    from konduktor.core.adapter import Unsupported  # noqa: E402

    clean_copy(REAL, work)
    before = dump(work)
    measured = ("djmdCue", "contentCue", "contentFile", "djmdMixerParam", "djmdSongPlaylist")
    sampler = {str(r["ContentID"]) for r in before.get("djmdSongSampler", {}).values()}
    songs = before["djmdSongPlaylist"].values()
    # The FIRST entry of the longest playlist, so the entries after it must
    # be renumbered.
    by_list: dict[str, list] = {}
    for r in songs:
        by_list.setdefault(str(r["PlaylistID"]), []).append(r)
    longest = max(by_list.values(), key=len, default=[])
    first = min(longest, key=lambda r: r["TrackNo"]) if len(longest) > 1 else None
    if first is None or str(first["ContentID"]) in sampler:
        print("  (skipped: no playlist with two entries to remove from)")
    else:
        victim = str(first["ContentID"])
        playlist_id = str(first["PlaylistID"])
        listed = [r["Path"] for r in before["contentFile"].values()
                  if str(r["ContentID"]) == victim]
        files = [p for p in listed if p.startswith("/PIONEER/USBANLZ/")]
        art = [p for p in listed if p.startswith("/PIONEER/Artwork/")]
        adapter = RekordboxAdapter(work)
        n_tracks = len(adapter.tracks)
        check("remove_tracks reports one track removed", adapter.remove_tracks([victim]) == 1)
        check("it leaves the projection at once",
              adapter.track(victim) is None and len(adapter.tracks) == n_tracks - 1)
        check("and its playlist", victim not in adapter.playlist_entries(playlist_id))
        check("the adapter is dirty", adapter.dirty is True)
        check("nothing is on disk before save", not diff(before, dump(work)))
        check("its analysis files are still there before save",
              all((Path(d) / "share" / p.lstrip("/")).is_file() for p in files))
        adapter.save()

        changes = diff(before, dump(work))
        expected_deletes = {("djmdContent", victim)} | {
            (t, pk) for t in measured for pk, r in before[t].items()
            if str(r.get("ContentID")) == victim
        }
        deletes = {(t, pk) for t, pk, kind, _ in changes if kind == "DELETE"}
        check("exactly the measured rows are deleted", deletes == expected_deletes,
              f"extra {sorted(deletes - expected_deletes)} missing "
              f"{sorted(expected_deletes - deletes)}")
        check("nothing is inserted", not [c for c in changes if c[2] == "INSERT"])
        updates = {(t, pk): f for t, pk, kind, f in changes if kind == "UPDATE"}
        later = {pk for pk, r in before["djmdSongPlaylist"].items()
                 if str(r["PlaylistID"]) == playlist_id and r["TrackNo"] > first["TrackNo"]}
        check("only the counter and the later entries' TrackNo move",
              set(updates) == {("agentRegistry", "localUpdateCount")}
              | {("djmdSongPlaylist", pk) for pk in later}, describe(changes))
        check("each later entry moves up by one",
              all(updates[("djmdSongPlaylist", pk)]["TrackNo"][1]
                  == before["djmdSongPlaylist"][pk]["TrackNo"] - 1 for pk in later))
        check("the playlist row itself is left alone",
              ("djmdPlaylist", playlist_id) not in updates)
        check("the track's analysis files are deleted",
              not any((Path(d) / "share" / p.lstrip("/")).exists() for p in files), str(files))
        check("and their emptied folder",
              not any((Path(d) / "share" / p.lstrip("/")).parent.exists() for p in files))
        # Unmeasured (the measured track had none), so left in place.
        check("its artwork is left alone",
              all((Path(d) / "share" / p.lstrip("/")).is_file() for p in art))
        check("the analysis root is kept",
              (Path(d) / "share" / "PIONEER" / "USBANLZ").is_dir())
        adapter.close()
        reopened = RekordboxAdapter(work)
        check("the track is gone after a reopen", reopened.track(victim) is None)
        reopened.close()

    # A table that was EMPTY in the measured library is not guessed at.
    if sampler:
        clean_copy(REAL, work)
        adapter = RekordboxAdapter(work)
        target = next(iter(sampler))
        try:
            adapter.remove_tracks([adapter.tracks[0].id, target])
            check("a track in the Sampler is refused", False)
        except Unsupported as ex:
            check("a track in the Sampler is refused", "Sampler" in str(ex), str(ex))
        check("and the whole batch changed nothing",
              adapter.dirty is False and adapter.track(target) is not None)
        adapter.close()

    # ---- J: adding tracks writes what Rekordbox 7 itself writes ------------
    # Measured 2026-10-01 by dragging two files into Rekordbox and diffing: a
    # djmdContent row filled from the FILE, analysis files + artwork under
    # share/, and one contentFile row per file whose Hash is its MD5. A plain
    # file gets Konduktor's own grid (decided 2026-10-01); one that brings a
    # grid keeps it, and its cues cross as themselves.
    print("== J: adding tracks writes rows and files the way Rekordbox does ==")
    import hashlib  # noqa: E402

    import av  # noqa: E402
    import numpy as np  # noqa: E402
    from konduktor.core.adapter import InvalidCommand, NewTrack  # noqa: E402
    from konduktor.core.model import CuePoint, GridMarker, Track, TrackCues  # noqa: E402

    BPM, FIRST, SR = 128.0, 0.5, 44100

    def kick_mp3(path: Path, seconds: float = 30.0) -> Path:
        """A kick every beat at BPM from FIRST — a grid with a known answer."""
        n = int(seconds * SR)
        y = np.zeros(n, dtype=np.float32)
        t = np.arange(int(0.12 * SR)) / SR
        kick = (np.sin(2 * np.pi * 55 * t) * np.exp(-t * 30)).astype(np.float32)
        beat = FIRST
        while beat < seconds - 0.2:
            i = int(beat * SR)
            y[i:i + len(kick)] += kick[: n - i]
            beat += 60.0 / BPM
        x = np.vstack([y, y]) * 0.8
        with av.open(str(path), "w", format="mp3") as c:
            s = c.add_stream("libmp3lame", rate=SR, layout="stereo")
            s.bit_rate = 320_000
            step = s.codec_context.frame_size or 1152
            for i in range(0, n, step):
                f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, i:i + step]),
                                               format="fltp", layout="stereo")
                f.sample_rate, f.pts = SR, i
                for p in s.encode(f):
                    c.mux(p)
            for p in s.encode(None):
                c.mux(p)
        return path

    def jpeg() -> bytes:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (300, 200), (200, 40, 90)).save(buf, "JPEG")
        return buf.getvalue()

    audio_dir = Path(d) / "added"
    audio_dir.mkdir()
    plain = kick_mp3(audio_dir / "plain.mp3")
    prepped = kick_mp3(audio_dir / "prepped.mp3")
    from mutagen.id3 import APIC, ID3  # noqa: E402
    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=jpeg()))
    tags.save(str(plain))   # embedded art: read off the file

    cues = TrackCues(
        grid_markers=[GridMarker(start=0.75, bpm=125.0)],  # the source's own grid
        cues=[
            CuePoint(role="hotcue", slot=0, start=2.0, length=0.0, color="#FF0000"),
            CuePoint(role="hotcue", slot=2, start=6.0, length=4.0, name="Loop"),
            CuePoint(role="memory", slot=None, start=9.0, length=0.0),
        ],
    )
    items = [
        NewTrack(track=Track(id="src:1", title="Konduktor Add Plain",
                             artist="Konduktor Add Artist", release_date="2021-03-04"),
                 audio_path=plain),
        NewTrack(track=Track(id="src:2", title="Konduktor Add Prepped",
                             key_wheel=10, key_mode="minor"),
                 audio_path=prepped, cues=cues, art=(jpeg(), "image/jpeg")),
    ]

    clean_copy(REAL, work)
    before = dump(work)
    share_before = {p for p in (Path(d) / "share").rglob("*") if p.is_file()}

    adapter = RekordboxAdapter(work)
    calls = []

    def cancel(message, *, step, of):
        calls.append((step, of))
        if step == 2:
            raise RuntimeError("cancelled")

    try:
        adapter.add_tracks(items, checkpoint=cancel)
        check("a raising checkpoint cancels", False)
    except RuntimeError:
        check("a raising checkpoint cancels", True)
    check("before touching the library", adapter.dirty is False
          and len(adapter.tracks) == len(before["djmdContent"]), str(calls))

    steps = []
    ids = adapter.add_tracks(items, checkpoint=lambda m, *, step, of: steps.append((step, of)))
    check("each file is a checkpoint", steps == [(1, 2), (2, 2)], str(steps))
    check("two ids come back, in order", len(ids) == 2 and all(adapter.track(i) for i in ids))
    check("the projection has them at once",
          [adapter.track(i).title for i in ids] == ["Konduktor Add Plain", "Konduktor Add Prepped"])
    check("nothing is on disk before save", not diff(before, dump(work)))
    check("no file is written before save",
          {p for p in (Path(d) / "share").rglob("*") if p.is_file()} == share_before)
    try:
        adapter.add_tracks([items[0]])
        check("adding a file the library holds is refused", False)
    except InvalidCommand:
        check("adding a file the library holds is refused", True)
    adapter.save()

    changes = diff(before, dump(work))
    new_rows = {t for t, _, kind, _ in changes if kind == "INSERT"}
    check("only new rows, plus the counter",
          {(t, kind) for t, _, kind, _ in changes} - {(t, "INSERT") for t in new_rows}
          == {("agentRegistry", "UPDATE")}, describe(changes))
    check("into the tables Rekordbox writes for a new track",
          new_rows <= {"djmdContent", "djmdCue", "contentCue", "contentFile",
                       "djmdArtist", "djmdKey"}, str(sorted(new_rows)))
    after = dump(work)
    rows = {i: after["djmdContent"][i] for i in ids}
    plain_row, prepped_row = rows[ids[0]], rows[ids[1]]
    check("the row is filled from the file",
          plain_row["SampleRate"] == SR and plain_row["BitRate"] == 320
          and plain_row["Length"] == 30 and plain_row["FileSize"] == plain.stat().st_size,
          str({k: plain_row[k] for k in ("SampleRate", "BitRate", "Length", "FileSize")}))
    check("stamped with this library's device",
          plain_row["DeviceID"] == next(iter(before["djmdContent"].values()))["DeviceID"])
    check("the release year is kept", plain_row["ReleaseYear"] == 2021)
    check("the key is rendered, not copied", prepped_row["KeyID"] is not None)
    files: dict[str, list] = {}
    for r in after["contentFile"].values():
        files.setdefault(str(r["ContentID"]), []).append(r)
    for tid, label in ((ids[0], "plain"), (ids[1], "prepped")):
        listed = files.get(tid, [])
        paths = sorted(Path(r["Path"]).name for r in listed)
        check(f"{label}: contentFile lists its analysis and artwork",
              paths == ["ANLZ0000.2EX", "ANLZ0000.DAT", "ANLZ0000.EXT", "artwork.jpg"], str(paths))
        on_disk = [Path(d) / "share" / r["Path"].lstrip("/") for r in listed]
        check(f"{label}: each Hash is the file's MD5 and Size its size",
              all(p.is_file() and r["Hash"] == hashlib.md5(p.read_bytes()).hexdigest()
                  and r["Size"] == p.stat().st_size for p, r in zip(on_disk, listed)))
        art_dir = (Path(d) / "share" / rows[tid]["ImagePath"].lstrip("/")).parent
        check(f"{label}: artwork in three sizes",
              sorted(p.name for p in art_dir.iterdir())
              == ["artwork.jpg", "artwork_m.jpg", "artwork_s.jpg"])
    adapter.close()

    reopened = RekordboxAdapter(work)
    plain_cues = reopened.track_cues(ids[0])
    markers = plain_cues.grid_markers if plain_cues else []
    check("a plain file gets Konduktor's grid", len(markers) == 1
          and abs(markers[0].bpm - BPM) < 0.05, str(markers))
    if markers:
        period = 60.0 / BPM
        off = (markers[0].start - FIRST) % period
        check("on the kick, in the decoded time base",
              min(off, period - off) < 0.015, f"{markers[0].start:.4f}")
    check("and its BPM column", rows[ids[0]]["BPM"] == 12800, str(rows[ids[0]]["BPM"]))
    got = reopened.track_cues(ids[1])
    check("a source's grid is kept as it was",
          got and [(round(m.start, 3), m.bpm) for m in got.grid_markers] == [(0.75, 125.0)],
          str(got.grid_markers if got else None))
    by_role = {(c.role, c.slot): c for c in (got.cues if got else [])}
    hot = by_role.get(("hotcue", 0))
    check("a hot cue keeps its pad and position",
          hot is not None and abs(hot.start - 2.0) < 0.002, str(hot))
    check("and its colour, as a palette swatch", hot is not None and hot.color is not None)
    loop = by_role.get(("hotcue", 2))
    check("a loop stays a loop on its pad",
          loop is not None and loop.type == "loop" and abs(loop.length - 4.0) < 0.002, str(loop))
    memory = [c for c in (got.cues if got else []) if c.role == "memory"]
    check("a memory cue stays a memory cue",
          len(memory) == 1 and abs(memory[0].start - 9.0) < 0.002, str(memory))

    # Removing an added track takes its analysis files with it.
    dat = Path(d) / "share" / rows[ids[0]]["AnalysisDataPath"].lstrip("/")
    reopened.remove_tracks([ids[0]])
    reopened.save()
    check("removing it again deletes its analysis files", not dat.exists())
    reopened.close()

    # ---- K: setting the key ------------------------------------------------
    # Rekordbox fills djmdKey lazily, one row per key it has met, named "Abm" —
    # and an imported tag can add "12A". So the key is found by MEANING, a row
    # is only ever added for a key the library lacks, and that row looks like
    # rekordbox's own: Seq NULL, its own USN.
    print("== K: set_key reuses the library's key rows and creates missing ones as rekordbox does ==")
    from konduktor.adapters.rekordbox.projection import parse_key as rb_parse_key, render_key

    clean_copy(REAL, work)
    before = dump(work)
    by_meaning = {rb_parse_key(r["ScaleName"]): pk for pk, r in before["djmdKey"].items()}
    adapter = RekordboxAdapter(work)
    have = next(((w, m) for (w, m) in by_meaning if w), None)
    lack = next(((w, m) for w in range(1, 13) for m in ("major", "minor") if (w, m) not in by_meaning))
    t1, t2 = adapter.tracks[0], adapter.tracks[1]
    if have is None:
        print("  (skipped the reuse half: this library has no key rows)")
    else:
        got = adapter.set_key(t1.id, *have)
        check("the projection updates immediately", (got.key_wheel, got.key_mode) == have, str(got))
        adapter.save()
        changes = diff(before, dump(work))
        check("an existing key: exactly the track row and the counter change",
              {t for t, _, _, _ in changes} == {"djmdContent", "agentRegistry"} and len(changes) == 2,
              describe(changes))
        content = [c for c in changes if c[0] == "djmdContent"]
        check("…pointing at the library's own row for that key",
              content and content[0][3].get("KeyID", (None, None))[1] == by_meaning[have],
              str(content and content[0][3]))
        before = dump(work)
    got = adapter.set_key(t2.id, *lack)
    adapter.save()
    after = dump(work)
    changes = diff(before, after)
    new_keys = [c for c in changes if c[0] == "djmdKey"]
    check("a missing key adds exactly one key row", len(new_keys) == 1 and new_keys[0][2] == "INSERT",
          describe(changes))
    if new_keys:
        row = new_keys[0][3]
        check("named in rekordbox's spelling, Seq NULL, stamped with a USN",
              rb_parse_key(row["ScaleName"]) == lack and row["ScaleName"] == render_key(*lack)
              and row["Seq"] is None and row["rb_local_usn"] is not None, str(row))
    adapter.set_key(t1.id, *lack)
    adapter.save()
    check("setting that key again reuses the new row",
          len(dump(work)["djmdKey"]) == len(after["djmdKey"]))
    adapter.close()

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
