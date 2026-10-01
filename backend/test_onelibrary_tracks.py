"""Adding tracks to a OneLibrary stick and removing them (decisions 3, 6 and 7).

On temp copies of Goober (`fixtures/onelibrary-goober/`, with its Device Library)
and generated MP3s, through `importer.run` — the path every add route takes:

  * placement: the LIBRARY decides — a file already on the stick is used in
    place; anything else goes to `Contents/<Artist>/<Album>/` as rekordbox lays a
    stick out (`UnknownArtist`/`UnknownAlbum`, names cut to 48 characters),
    through `.konduktor-incoming/`;
  * an added track arrives ready to prep: analysis files (grid, cues, memory
    cues), artwork, a row shaped like rekordbox's, the track count, and its row
    in the Device Library;
  * nothing is left behind: a cancel mid-copy, a Discard, a crash (cleared on the
    next EDITABLE open only);
  * removal: every row naming the track goes (playlists renumbered), and its
    audio, analysis files and unshared artwork are deleted AFTER the save that
    stopped naming them — never before.
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-oltracks-appdata-")

from stem_test_support import make_mp3  # noqa: E402
from konduktor import importer  # noqa: E402
from konduktor.adapters.onelibrary.adapter import OneLibraryAdapter  # noqa: E402
from konduktor.adapters.rekordbox import pdb  # noqa: E402
from konduktor.core.adapter import NewTrack  # noqa: E402
from konduktor.core.model import CuePoint, GridMarker, Track, TrackCues  # noqa: E402
from konduktor.jobs import Job, JobCancelled, JobHandle  # noqa: E402

HERE = Path(__file__).resolve().parent
GOOBER = HERE / "fixtures" / "onelibrary-goober"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def copy_of(name: str = "Goober") -> Path:
    root = Path(tempfile.mkdtemp(prefix="konduktor-oltracks-")) / name
    shutil.copytree(GOOBER, root, ignore=shutil.ignore_patterns("after", "README.md"))
    return root


def fingerprint(root: Path) -> str:
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode() + p.read_bytes())
    return h.hexdigest()


def rows(root: Path, sql: str):
    import sqlcipher3.dbapi2 as sqlcipher
    from pyrekordbox.devicelib_plus.database import BLOB
    from pyrekordbox.utils import deobfuscate

    con = sqlcipher.connect(str(root / "PIONEER" / "rekordbox" / "exportLibrary.db"))
    con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
    con.row_factory = lambda cur, row: dict(zip([d[0] for d in cur.description], row))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 40, 90)).save(buf, "JPEG")
    return buf.getvalue()


SRC = Path(tempfile.mkdtemp(prefix="konduktor-oltracks-src-"))
ONE = make_mp3(SRC / "Kick One.mp3", seconds=20)
TWO = make_mp3(SRC / "Kick Two.mp3", seconds=20, seed=5)
COVER = jpeg()


class Source:
    """Two tracks: one fully prepped (grid, hot cue, memory loop, cover), one bare."""

    def __init__(self):
        self.t = {
            str(ONE): Track(id=str(ONE), title="Kick One", artist="Konduktor: Test", album="Probe EP",
                            bpm=120.0, length=20, rating=4, filepath=str(ONE)),
            str(TWO): Track(id=str(TWO), title="Kick Two", length=20, filepath=str(TWO)),
        }

    @property
    def tracks(self):
        return list(self.t.values())

    def track(self, i):
        return self.t.get(i)

    def track_cues(self, i):
        if i != str(ONE):
            return None
        return TrackCues(grid_markers=[GridMarker(start=0.5, bpm=120.0)], cues=[
            CuePoint(role="hotcue", slot=1, start=2.5, length=0.0, type="cue", color="#FF0045", name="In"),
            CuePoint(role="hotcue", slot=1, start=4.5, length=0.0, type="cue"),  # pad taken -> memory
            CuePoint(role="memory", start=8.5, length=2.0, type="loop"),
        ])

    def cover_art(self, i):
        return (COVER, "image/jpeg") if i == str(ONE) else None


class CancelOnFirstBytes(JobHandle):
    def progress(self, done=None, **kw):
        super().progress(done=done, **kw)
        if done:
            self._job._cancel.set()


def run(adapter, handle_cls=JobHandle, track_ids=(str(ONE), str(TWO))):
    plan = importer.plan(Source(), adapter, None, track_ids=list(track_ids))
    return importer.run(Source(), adapter, plan, handle_cls(Job(id="t", kind="import")))


# ---- placement -----------------------------------------------------------------
print("== the library places the audio ==")
drive = copy_of()
a = OneLibraryAdapter(drive)
caps = a.capabilities()
check("an editable stick adds, removes, and places its own audio",
      caps.tracks.addable and caps.tracks.removable and caps.tracks.places_audio)
check("a browsing one does neither", not OneLibraryAdapter(drive, read_only=True).capabilities().tracks.addable)
on_stick = drive / "Contents" / "Here.mp3"
on_stick.parent.mkdir(parents=True, exist_ok=True)
on_stick.write_bytes(b"x")
check("a file already on the stick is used where it is", a.place_audio(on_stick, Track(id="x")) is None)
target = a.place_audio(ONE, Track(id="x", artist="A/B: C", album="Album."))
final = a._store.final_of(target)
check("anything else is copied in through .konduktor-incoming/",
      target.parent == drive / ".konduktor-incoming" and target.suffix == ".mp3")
check("to Contents/<Artist>/<Album>/, with characters FAT refuses replaced",
      final == drive / "Contents" / "A_B_ C" / "Album" / "Kick One.mp3", str(final))
check("no artist or album: UnknownArtist / UnknownAlbum (rekordbox's names)",
      a._store.final_of(a.place_audio(TWO, Track(id="y"))).parent
      == drive / "Contents" / "UnknownArtist" / "UnknownAlbum")
long = SRC / ("L" * 60 + ".mp3")
long.write_bytes(b"x")
check("a long file name is cut to 48 characters, as rekordbox does",
      len(a._store.final_of(a.place_audio(long, Track(id="z"))).name) == 48)
taken = drive / "Contents" / "UnknownArtist" / "UnknownAlbum" / "Kick Two.mp3"
taken.parent.mkdir(parents=True, exist_ok=True)
taken.write_bytes(b"x")
check("a name already taken on the stick gets a suffix",
      a._store.final_of(a.place_audio(TWO, Track(id="y"))).name == "Kick Two-2.mp3")
a.close()

# ---- adding ----------------------------------------------------------------------
print("== an import into the stick ==")
drive = copy_of()
a = OneLibraryAdapter(drive)
before_n = rows(drive, "SELECT numberOfContents AS n FROM property")[0]["n"]
result = run(a)
one_id, two_id = result["track_ids"]
check("both tracks are added", result["tracks"] == 2)
check("the import saved, so the copies are in place and the incoming folder is gone",
      (drive / one_id.lstrip("/")).is_file() and (drive / two_id.lstrip("/")).is_file()
      and not (drive / ".konduktor-incoming").exists())
check("laid out as rekordbox does",
      one_id == "/Contents/Konduktor_ Test/Probe EP/Kick One.mp3"
      and two_id == "/Contents/UnknownArtist/UnknownAlbum/Kick Two.mp3", f"{one_id} {two_id}")
check("the projection plays them from there", a.track(one_id).filepath == str(drive / one_id.lstrip("/")))
cues = a.track_cues(one_id)
hot = [c for c in cues.cues if c.role == "hotcue"]
memory = sorted((c for c in cues.cues if c.role == "memory"), key=lambda c: c.start)
check("a hot cue keeps its pad, colour and name",
      [(c.slot, round(c.start, 2), c.color, c.name) for c in hot] == [(1, 2.5, "#FF0045", "In")], str(hot))
check("a memory loop, and a hot cue whose pad was taken, cross as memory cues",
      [(round(c.start, 2), c.type) for c in memory] == [(4.5, "cue"), (8.5, "loop")], str(memory))
check("its grid crosses", [(round(m.start, 3), m.bpm) for m in cues.grid_markers] == [(0.5, 120.0)],
      str(cues.grid_markers))
check("a file with no grid gets Konduktor's", len(a.track_cues(two_id).grid_markers) == 1)
dat = drive / rows(drive, f"SELECT analysisDataFilePath AS p FROM content WHERE path = '{one_id}'")[0]["p"].lstrip("/")
check("its .DAT, .EXT and .2EX are on the stick",
      all(dat.with_suffix(s).is_file() for s in (".DAT", ".EXT", ".2EX")))

row = rows(drive, f"SELECT * FROM content WHERE path = '{one_id}'")[0]
check("the row is shaped like rekordbox's own (search NULL, no counters, '' texts)",
      row["titleForSearch"] is None and row["cueUpdateCount"] is None and row["hasModified"] == 0
      and row["subtitle"] == "" and row["isrc"] == "" and row["contentLink"] == 788224
      and row["analysedBits"] == 41, str(row))
check("with the file's facts", row["fileName"] == "Kick One.mp3" and row["fileType"] == 1
      and row["samplingRate"] == 44100 and row["bpmx100"] == 12000 and row["rating"] == 4)
check("dates as YYYY-MM-DD text", len(row["dateAdded"]) == 10 and len(row["dateCreated"] or "") == 10)
check("the track count follows",
      rows(drive, "SELECT numberOfContents AS n FROM property")[0]["n"] == before_n + 2)
image = rows(drive, f"SELECT path FROM image WHERE image_id = {row['image_id']}")[0]["path"]
art = drive / "PIONEER" / "Artwork" / "00001"
n = row["image_id"]
check("the cover is written as rekordbox writes it: a/b pairs, 80 and 240 px",
      image == f"/PIONEER/Artwork/00001/b{n}.jpg"
      and all((art / f"{x}{n}{s}.jpg").is_file() for x in "ab" for s in ("", "_m")))
check("and served as the track's art", a.cover_art(one_id)[1] == "image/jpeg")

lib = pdb.read((drive / "PIONEER" / "rekordbox" / "export.pdb").read_bytes())
new = {t["file_path"]: t for t in lib.tracks}
check("the Device Library lists both, with OneLibrary's ids",
      new.get(one_id, {}).get("id") == row["content_id"] and two_id in new)
check("their analysis paths and artwork (the a<n> file) reach it too",
      new[one_id]["analyze_path"] == row["analysisDataFilePath"]
      and lib.artwork.get(new[one_id]["artwork_id"]) == f"/PIONEER/Artwork/00001/a{n}.jpg")
a.close()

print("== nothing is left behind ==")
drive = copy_of()
before = fingerprint(drive)
a = OneLibraryAdapter(drive)
try:
    run(a, CancelOnFirstBytes)
    cancelled = False
except JobCancelled:
    cancelled = True
check("a cancel mid-copy leaves the stick exactly as it was",
      cancelled and fingerprint(drive) == before and not a.dirty)
target = a.place_audio(ONE, Track(id="x", title="Kick"))
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ONE, target)
[pending] = a.add_tracks([NewTrack(track=Track(id="x", title="Kick"), audio_path=target)])
check("an unsaved add plays from its incoming copy", a.track(pending).filepath == str(target))
a.reload()
check("Discard deletes the copy and leaves the stick as it was",
      not target.exists() and fingerprint(drive) == before and a.track(pending) is None)
a.close()
(drive / ".konduktor-incoming").mkdir()
(drive / ".konduktor-incoming" / "crashed.mp3").write_bytes(b"x")
OneLibraryAdapter(drive, read_only=True).close()
check("a crash's leftovers survive a BROWSING open", (drive / ".konduktor-incoming").exists())
OneLibraryAdapter(drive).close()
check("and are cleared by the next editable one", not (drive / ".konduktor-incoming").exists())

# ---- removing ----------------------------------------------------------------------
print("== removing tracks ==")
drive = copy_of()
a = OneLibraryAdapter(drive)
by = lambda prefix: next(t for t in a.tracks if t.title.startswith(prefix))  # noqa: E731
victim = by("Demo Track 1")
audio = drive / victim.id.lstrip("/")
audio.parent.mkdir(parents=True, exist_ok=True)
audio.write_bytes(b"audio")                    # the fixture carries no audio
dat = a._store.layout.resolve(a._store.content(victim.id).analysisDataFilePath)
tian = by("Tian")                              # holds image 3, shared with nobody
n = a._store.content(tian.id).image_id
art = drive / "PIONEER" / "Artwork" / "00001"
art.mkdir(parents=True, exist_ok=True)
for x in "ab":
    for s in ("", "_m"):
        (art / f"{x}{n}{s}.jpg").write_bytes(b"jpg")
demos = next(p for p in a.playlist_tree() if p.name == "demos")
count = rows(drive, "SELECT numberOfContents AS n FROM property")[0]["n"]

check("removing two tracks reports two", a.remove_tracks([victim.id, tian.id]) == 2)
check("they leave the library and its playlists at once",
      a.track(victim.id) is None and victim.id not in (a.playlist_entries(demos.id) or []))
check("but nothing is deleted before Save", audio.exists() and dat.exists() and (art / f"b{n}.jpg").exists())
a.save()
check("Save deletes the audio, the analysis files and the unshared artwork",
      not audio.exists() and not dat.exists() and not dat.with_suffix(".EXT").exists()
      and not any((art / f"{x}{n}{s}.jpg").exists() for x in "ab" for s in ("", "_m")))
check("and prunes the folders that left empty", not dat.parent.exists())
check("the drive's top-level folders stay", (drive / "Contents").is_dir() and (drive / "PIONEER" / "USBANLZ").is_dir())
seqs = [r["sequenceNo"] for r in rows(drive, f"SELECT sequenceNo FROM playlist_content "
                                              f"WHERE playlist_id = {demos.id} ORDER BY sequenceNo")]
check("the playlist is renumbered from 1", seqs == list(range(1, len(seqs) + 1)), str(seqs))
check("no row anywhere names them",
      not rows(drive, f"SELECT 1 FROM content WHERE path IN ('{victim.id}', '{tian.id}')")
      and not rows(drive, f"SELECT 1 FROM image WHERE image_id = {n}"))
check("the track count follows", rows(drive, "SELECT numberOfContents AS n FROM property")[0]["n"] == count - 2)
lib = pdb.read((drive / "PIONEER" / "rekordbox" / "export.pdb").read_bytes())
check("and the Device Library drops them, their entries and the artwork row",
      not {victim.id, tian.id} & {t["file_path"] for t in lib.tracks}
      and all(t != 1 for _i, t, _p in lib.entries) and n not in lib.artwork)
a.close()

print("== removing a track added this session, before Save ==")
drive = copy_of()
before = fingerprint(drive)
a = OneLibraryAdapter(drive)
target = a.place_audio(ONE, Track(id="x", title="Kick"))
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ONE, target)
[pending] = a.add_tracks([NewTrack(track=Track(id="x", title="Kick"), audio_path=target)])
a.remove_tracks([pending])
check("its copy is deleted at once", not target.exists())
a.save()
check("and the save leaves no trace of it",
      not rows(drive, f"SELECT 1 FROM content WHERE path = '{pending}'")
      and not (drive / pending.lstrip("/")).exists())
a.close()

print()
print("RESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
