"""OneLibrary's analogue of `test_rekordbox_fidelity.py`: an edit changes only
what it was supposed to change, checked as a FULL TABLE DUMP before and after
(a byte diff means nothing against SQLite), on a temp copy of the fixture drive.

The expected diffs are rekordbox's own, MEASURED on Goober (2026-10-01; see the
editing discussion log): a title edit changes `content.title` and nothing else —
no update counter moves — a new playlist goes on top with its siblings shifted
down one, and a reorder renumbers that playlist's entries from 1.

And the properties a STICK needs that a desktop library does not: nothing
reaches the drive before Save, the drive is replaced in one rename, another
app's write since open is refused rather than undone, an unplugged drive keeps
the edits for a retry, stale `-wal`/`-shm` files are deleted, and a backup of
the drive's database lands in app-data first.
"""
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
APPDATA = Path(tempfile.mkdtemp(prefix="konduktor-olfid-appdata-"))
os.environ["KONDUKTOR_DATA_DIR"] = str(APPDATA)

from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.core.adapter import InvalidCommand  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
TRACK_1 = "/Contents/Loopmasters/UnknownAlbum/Demo Track 1.mp3"
TRACK_2 = "/Contents/Loopmasters/UnknownAlbum/Demo Track 2.mp3"

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


# ---- the row-level dump/diff harness -------------------------------------
LINK_TABLES = {"playlist_content", "history_content", "myTag_content", "hotCueBankList_cue"}


def dump(db: Path) -> dict:
    """Every row of every table; keyed by primary key, or by the whole row for
    the link tables, which have none."""
    import sqlcipher3.dbapi2 as sqlcipher
    from pyrekordbox.devicelib_plus.database import BLOB
    from pyrekordbox.utils import deobfuscate

    con = sqlcipher.connect(str(db))
    con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
    cur = con.cursor()
    names = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    out: dict[str, dict] = {}
    for table in names:
        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
        rows = {}
        for r in cur.execute(f'SELECT * FROM "{table}"').fetchall():
            d = dict(zip(cols, r))
            key = "|".join(map(str, r)) if table in LINK_TABLES else str(r[0])
            rows[key] = d
        out[table] = rows
    con.close()
    return out


def diff(before: dict, after: dict) -> list:
    changes = []
    for table in sorted(set(before) | set(after)):
        a, b = before.get(table, {}), after.get(table, {})
        for pk in sorted(set(a) | set(b)):
            if pk not in a:
                changes.append((table, pk, "INSERT", b[pk]))
            elif pk not in b:
                changes.append((table, pk, "DELETE", a[pk]))
            else:
                fields = {k: (a[pk][k], b[pk].get(k)) for k in a[pk] if a[pk][k] != b[pk].get(k)}
                if fields:
                    changes.append((table, pk, "UPDATE", fields))
    return changes


def describe(changes) -> str:
    return "; ".join(f"{t}/{pk} {kind}" for t, pk, kind, _ in changes) or "(none)"


def fresh_drive() -> Path:
    root = Path(tempfile.mkdtemp(prefix="konduktor-olfid-")) / "Dingus"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("rekordbox-edited"))
    return root


def db_of(root: Path) -> Path:
    return root / "PIONEER" / "rekordbox" / "exportLibrary.db"


# ---- A: nothing reaches the drive until Save --------------------------------
print("== edits stay off the drive until Save ==")
drive = fresh_drive()
before_bytes = db_of(drive).read_bytes()
before = dump(db_of(drive))
a = OneLibraryAdapter(drive)
check("opening writes nothing", db_of(drive).read_bytes() == before_bytes)
check("and leaves no -wal/-shm on the drive",
      not any(db_of(drive).with_name(db_of(drive).name + s).exists() for s in ("-wal", "-shm")))
a.set_track_metadata(TRACK_1, {"title": "Edited"})
check("an edit makes the library dirty", a.dirty is True)
check("and the projection shows it at once", a.track(TRACK_1).title == "Edited")
check("but the drive's database is byte-identical", db_of(drive).read_bytes() == before_bytes)

print("== Discard ==")
a.reload()
check("discarding drops the edit", a.track(TRACK_1).title.startswith("Demo Track 1"), a.track(TRACK_1).title)
check("and leaves nothing dirty", a.dirty is False)
check("and the drive untouched", db_of(drive).read_bytes() == before_bytes)

# ---- B: a no-op save ----------------------------------------------------------
print("== a no-op save ==")
a.save()
check("changes zero rows", diff(before, dump(db_of(drive))) == [], describe(diff(before, dump(db_of(drive)))))
a.close()

# ---- C: one field, one column ---------------------------------------------------
print("== a one-field edit changes exactly that column ==")
drive = fresh_drive()
before = dump(db_of(drive))
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"title": "Edited"})
outcome = a.save()
changes = diff(before, dump(db_of(drive)))
check("exactly one row changed", len(changes) == 1, describe(changes))
check("content.title, and no update counter (measured: rekordbox moves none)",
      changes and changes[0][0] == "content" and set(changes[0][3]) == {"title"}
      and changes[0][3]["title"][1] == "Edited", str(changes))
check("the save is not dirty afterwards", a.dirty is False)
check("and the adapter reads the saved drive", a.track(TRACK_1).title == "Edited")
check("the save reports a summary of the edit", bool(outcome.summary), outcome.summary)
check("no version-history snapshot (a drive is many files)", outcome.snapshot is None)

backups = sorted((APPDATA / "onelibrary" / "backups" / "Dingus").iterdir())
check("a backup was written to app-data, not the drive", len(backups) >= 1)
check("holding the database as it was BEFORE the save",
      dump(backups[-1] / "exportLibrary.db") == before)
check("no partial file is left beside the database",
      not list(db_of(drive).parent.glob("*.konduktor-partial")))
a.close()

print("== rating, comment, release date ==")
drive = fresh_drive()
before = dump(db_of(drive))
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"rating": 3})
a.set_track_metadata(TRACK_2, {"comment": "", "release_date": "2021-03-04"})
check("an out-of-range rating is refused", _raises(lambda: a.set_track_metadata(TRACK_1, {"rating": 6}),
                                                    InvalidCommand))
check("a malformed date is refused", _raises(lambda: a.set_track_metadata(TRACK_1, {"release_date": "soon"}),
                                              InvalidCommand))
a.save()
changes = {(t, pk): f for t, pk, kind, f in diff(before, dump(db_of(drive)))}
row1 = next(f for (t, pk), f in changes.items() if t == "content" and pk == "1")
row2 = next(f for (t, pk), f in changes.items() if t == "content" and pk == "2")
check("rating is stored 0-5 directly", row1 == {"rating": (0, 3)}, str(row1))
check("a cleared comment is '' as rekordbox stores it, not NULL",
      row2.get("djComment", (None, None))[1] == "", str(row2))
check("a full date sets releaseYear AND releaseDate as YYYY-MM-DD text",
      row2.get("releaseYear", (0, 0))[1] == 2021 and row2.get("releaseDate", ("", ""))[1] == "2021-03-04",
      str(row2))
check("and reads back", a.track(TRACK_2).release_date == "2021-03-04", str(a.track(TRACK_2).release_date))
check("nothing else changed", len(changes) == 2, str(list(changes)))
a.set_track_metadata(TRACK_2, {"release_date": "2019"})
check("a bare year is releaseYear with an empty releaseDate",
      a._store.content(TRACK_2).releaseYear == 2019 and a.track(TRACK_2).release_date == "2019",
      str(a.track(TRACK_2).release_date))
a.close()

print("== lookups are found or created ==")
drive = fresh_drive()
before = dump(db_of(drive))
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"artist": "Loopmasters"})  # already there
check("an existing name is reused, not duplicated", a.dirty and a._store.content(TRACK_1).artist_id_artist == 1)
a.set_track_metadata(TRACK_1, {"artist": "New Artist", "genre": "Techno"})
check("the projection reads the NEW names (not the stale relationship)",
      a.track(TRACK_1).artist == "New Artist" and a.track(TRACK_1).genre == "Techno",
      f"{a.track(TRACK_1).artist} / {a.track(TRACK_1).genre}")
a.set_track_metadata(TRACK_2, {"label": ""})
check("clearing a lookup unsets it", a.track(TRACK_2).label is None)
a.save()
changes = diff(before, dump(db_of(drive)))
inserts = [(t, f) for t, pk, kind, f in changes if kind == "INSERT"]
check("one new artist and one new genre row",
      sorted(t for t, _ in inserts) == ["artist", "genre"], describe(changes))
check("with nameForSearch NULL, as rekordbox leaves it",
      all(f.get("nameForSearch") is None for t, f in inserts if t == "artist"))
a.close()

# ---- D: playlists, as rekordbox writes them ---------------------------------------
print("== playlists ==")
drive = fresh_drive()
before = dump(db_of(drive))
a = OneLibraryAdapter(drive)
demos = a.playlist_tree()[0]
new_id = a.create_playlist("Probe")
tree = a.playlist_tree()
check("a new playlist is listed FIRST (rekordbox puts it on top)",
      [n.name for n in tree] == ["Probe", "demos"], str([n.name for n in tree]))
a.set_playlist_entries(new_id, [TRACK_2])
a.set_playlist_entries(demos.id, [TRACK_2, TRACK_1])
check("a reorder reads back in the new order", a.playlist_entries(demos.id) == [TRACK_2, TRACK_1])
a.save()
changes = diff(before, dump(db_of(drive)))
pl = {pk: (kind, f) for t, pk, kind, f in changes if t == "playlist"}
check("the new playlist row has sequenceNo 0, attribute 0, parent 0",
      pl.get(new_id, ("", {}))[0] == "INSERT"
      and {k: pl[new_id][1][k] for k in ("sequenceNo", "attribute", "playlist_id_parent")}
      == {"sequenceNo": 0, "attribute": 0, "playlist_id_parent": 0}, str(pl.get(new_id)))
check("and its sibling moved down one", pl.get(demos.id) == ("UPDATE", {"sequenceNo": (0, 1)})
      or pl.get(demos.id, ("", {}))[1].get("sequenceNo", (None, None))[1]
      == before["playlist"][demos.id]["sequenceNo"] + 1, str(pl.get(demos.id)))
entries = sorted((f["playlist_id"], f["content_id"], f["sequenceNo"])
                 for t, pk, kind, f in changes if t == "playlist_content" and kind == "INSERT")
check("entries are numbered from 1 (measured)",
      entries == sorted([(int(new_id), 2, 1), (int(demos.id), 2, 1), (int(demos.id), 1, 2)]), str(entries))
check("no other table changed", {t for t, *_ in changes} == {"playlist", "playlist_content"},
      describe(changes))

folder = a.create_folder("Gigs")
inner = a.create_playlist("Friday", folder)
a.set_playlist_entries(inner, [TRACK_1])
check("a playlist can only be created inside a FOLDER",
      _raises(lambda: a.create_playlist("x", demos.id), InvalidCommand))
check("a nameless playlist is refused", _raises(lambda: a.create_playlist("  "), InvalidCommand))
a.rename_playlist(inner, "Saturday")
gigs = next(n for n in a.playlist_tree() if n.id == folder)
check("a folder nests its playlist", [c.name for c in gigs.children] == ["Saturday"])
a.save()
mid = dump(db_of(drive))
a.delete_playlist(folder)
a.save()
after = dump(db_of(drive))
changes = diff(mid, after)
check("deleting a folder deletes what is in it, entries included",
      sorted((t, kind) for t, pk, kind, _ in changes if kind == "DELETE")
      == [("playlist", "DELETE"), ("playlist", "DELETE"), ("playlist_content", "DELETE")],
      describe(changes))
seqs = sorted(r["sequenceNo"] for r in after["playlist"].values() if r["playlist_id_parent"] == 0)
check("and closes the gap among its siblings", seqs == list(range(len(seqs))), str(seqs))
a.close()

print("== moving playlists (sidebar drag and drop) ==")
drive = fresh_drive()
a = OneLibraryAdapter(drive)
folder = a.create_folder("Gigs")
probe = a.create_playlist("Probe")
a.save()
before = dump(db_of(drive))
check("every node offers can_move", all(n.can_move for n in a.playlist_tree()))
demos = next(n for n in a.playlist_tree() if n.name == "demos")
a.move_playlist(demos.id, folder, 0)
a.move_playlist(probe, None, 1)
tree = a.playlist_tree()
check("a playlist moved into a folder is its child",
      [c.id for c in next(n for n in tree if n.id == folder).children] == [demos.id])
check("a playlist moves to the index asked for", [n.id for n in tree] == [folder, probe],
      str([n.name for n in tree]))
check("a folder cannot be moved into itself",
      _raises(lambda: a.move_playlist(folder, folder, 0), InvalidCommand))
check("nor into a playlist", _raises(lambda: a.move_playlist(probe, demos.id, 0), InvalidCommand))
a.save()
after = dump(db_of(drive))
changes = diff(before, after)
check("only playlist rows change, by UPDATE",
      {(t, kind) for t, _, kind, _ in changes} == {("playlist", "UPDATE")}, describe(changes))
check("only parent and sequenceNo columns change",
      {c for *_, f in changes for c in f} <= {"playlist_id_parent", "sequenceNo"},
      str([f for *_, f in changes]))
for parent in (0, int(folder)):
    seqs = sorted(r["sequenceNo"] for r in after["playlist"].values() if r["playlist_id_parent"] == parent)
    check(f"sequenceNo stays 0..n-1 under {parent}", seqs == list(range(len(seqs))), str(seqs))
a.close()
reopened = OneLibraryAdapter(drive)
check("the move round-trips through a reopen",
      [c.id for c in next(n for n in reopened.playlist_tree() if n.id == folder).children] == [demos.id])
reopened.move_playlist(probe, None, 1)
check("moving a node onto its own place is not an edit", reopened.dirty is False)
reopened.close()

# ---- E: a stick's hazards ------------------------------------------------------------
print("== another app wrote the drive since it was opened ==")
drive = fresh_drive()
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"title": "Mine"})
theirs = db_of(drive).read_bytes()
time.sleep(0.01)
os.utime(db_of(drive))  # e.g. rekordbox saving the stick meanwhile
check("Save refuses rather than undo it", _raises(a.save, InvalidCommand))
check("the drive is exactly as the other app left it", db_of(drive).read_bytes() == theirs)
check("and the edit is still pending", a.dirty and a.track(TRACK_1).title == "Mine")
a.close()

print("== an unplugged drive ==")
drive = fresh_drive()
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"title": "Kept"})
parked = drive.parent / "unplugged"
(drive / "PIONEER").rename(parked)
check("Save refuses cleanly", _raises(a.save, InvalidCommand))
check("keeping the edit", a.dirty and a.track(TRACK_1).title == "Kept")
parked.rename(drive / "PIONEER")
a.save()
check("and saves it once the drive is back",
      dump(db_of(drive))["content"]["1"]["title"] == "Kept" and not a.dirty)
a.close()

print("== stale SQLite sidecars ==")
drive = fresh_drive()
shm = db_of(drive).with_name("exportLibrary.db-shm")
shm.write_bytes(b"\0" * 32768)  # rekordbox leaves one on a stick it mounted
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"title": "x"})
a.save()
check("a stale -shm is deleted on save (SQLite would trust it)", not shm.exists())
check("and the saved database opens", dump(db_of(drive))["content"]["1"]["title"] == "x")
a.close()

print("== the drive is never held open ==")
drive = fresh_drive()
a = OneLibraryAdapter(drive)
try:
    import subprocess

    mine = subprocess.run(["lsof", "-Fn", "-p", str(os.getpid())], capture_output=True,
                          text=True).stdout
    check("no file on the drive is open in this process",
          str(db_of(drive)) not in mine, "exportLibrary.db is open")
except FileNotFoundError:
    print("  (lsof unavailable — skipped)")
a.close()

print("== a read-only (browsing) drive ==")
drive = fresh_drive()
b = OneLibraryAdapter(drive, read_only=True)
check("is not writable", b.capabilities().writable is False)
b.close()
check("closing removes its working copy",
      not any((APPDATA / "onelibrary" / "work").iterdir()))

print("== file tags (decision 5: as rekordbox does) ==")
drive = fresh_drive()
from stem_test_support import make_mp3  # noqa: E402

audio = drive / TRACK_1.lstrip("/")
make_mp3(audio)  # the fixture's MP3s are 12-byte placeholders
a = OneLibraryAdapter(drive)
a.set_track_metadata(TRACK_1, {"title": "Tagged", "artist": "Somebody", "rating": 4})
outcome = a.save()
import mutagen  # noqa: E402

tags = mutagen.File(str(audio)).tags
check("the edited title reaches the file's TIT2", str(tags.get("TIT2")) == "Tagged", str(tags.get("TIT2")))
check("and the artist its TPE1", str(tags.get("TPE1")) == "Somebody")
check("the rating does NOT (rekordbox keeps it library-only)", not tags.getall("POPM"))
check("the write is reported", any(r.ok and r.track_id == TRACK_1 for r in outcome.tag_results),
      str(outcome.tag_results))
(drive / TRACK_2.lstrip("/")).unlink()  # its audio gone from the stick
a.set_track_metadata(TRACK_2, {"title": "Missing"})
outcome = a.save()
check("a missing audio file is reported, not fatal",
      [r.status for r in outcome.tag_results] == ["file-not-found"] and not a.dirty
      and dump(db_of(drive))["content"]["2"]["title"] == "Missing", str(outcome.tag_results))
a.close()

# ---- the key: found by meaning, created in rekordbox's spelling, tagged -------
print("== set_key ==")
from konduktor.adapters.rekordbox.projection import parse_key, render_key  # noqa: E402

drive = fresh_drive()
audio = drive / TRACK_1.lstrip("/")
make_mp3(audio)
before = dump(db_of(drive))
keys = {parse_key(r["name"]): pk for pk, r in before["key"].items()}
a = OneLibraryAdapter(drive)
check("the key can be written on an editable stick", a.capabilities().tracks.key_writable is True)
have = next(iter(keys))
t = a.set_key(TRACK_1, *have)
check("the projection shows it at once", (t.key_wheel, t.key_mode) == have, str(t))
a.save()
changes = diff(before, dump(db_of(drive)))
check("an existing key: exactly content.key_id changes, pointing at the stick's own row",
      len(changes) == 1 and set(changes[0][3]) == {"key_id"}
      and str(changes[0][3]["key_id"][1]) == keys[have], str(changes))
check("and reaches the file's TKEY as the stick names it",
      str(mutagen.File(str(audio)).tags.get("TKEY")) == before["key"][keys[have]]["name"],
      str(mutagen.File(str(audio)).tags.get("TKEY")))
before = dump(db_of(drive))
lack = next((w, m) for w in range(1, 13) for m in ("major", "minor") if (w, m) not in keys)
a.set_key(TRACK_2, *lack)
a.save()
changes = diff(before, dump(db_of(drive)))
added = [c for c in changes if c[0] == "key"]
check("a missing key adds one key row, in rekordbox's spelling",
      len(added) == 1 and added[0][2] == "INSERT" and added[0][3]["name"] == render_key(*lack),
      describe(changes))
check("a read-only stick may not", OneLibraryAdapter(drive, read_only=True).capabilities().tracks.key_writable is False)
a.close()

print("== imported key names (a rekordbox stick holds '12A' beside 'Abm') ==")
goober = Path(tempfile.mkdtemp(prefix="konduktor-olfid-goober-")) / "Goober"
shutil.copytree(FIXTURE.parent / "onelibrary-goober", goober, ignore=shutil.ignore_patterns("after"))
before = dump(db_of(goober))
names = {r["name"]: pk for pk, r in before["key"].items()}
g = OneLibraryAdapter(goober)
camelot_keyed = [t for t in g.tracks if t.key in ("12A", "11A", "10A")]
check("a track keyed '12A' projects its wheel (it read as no key before)",
      camelot_keyed and all(t.key_wheel is not None for t in camelot_keyed),
      str([(t.key, t.key_wheel) for t in camelot_keyed]))
target = next(t for t in g.tracks if t.key != "12A")
g.set_key(target.id, 12, "minor")
g.save()
after = dump(db_of(goober))
check("setting 12A reuses the stick's '12A' row instead of adding 'Dbm'",
      len(after["key"]) == len(before["key"])
      and str(next(r for r in after["content"].values()
                   if r["content_id"] == next(r2["content_id"] for r2 in before["content"].values()
                                              if r2["title"] == target.title))["key_id"]) == names["12A"])
g.close()

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
