"""Traktor's own block inside an audio file — removed from every copy made for a
Traktor library, so Traktor cannot restore its stale grid over the entry's.

Measured in Traktor 4.5 (2026-10-24, see `adapters/traktor/file_block.py`): an
entry Konduktor wrote has no MODIFIED_DATE, so Traktor re-reads the file and
takes the block's BPM (`HBPM`) over the collection's, LOCK or not. With the
block removed from the copy, the entry's grid, lock, cues — and the art, which
Traktor still imports from the file's ordinary tags — all survive.

Self-contained: generated audio in each format carrying a block that says 175
while the library's grid says 176, and collections built from Konduktor's own
empty Traktor skeleton. Pins:
  - `file_block`: each format's block found and removed, everything else in
    the file kept (audio sample-identical, other tags, a foreign PRIV frame),
    a file without one never rewritten, a garbage file refused quietly;
  - export: every copy for a Traktor target loses the block, the SOURCE keeps
    it, the manifest records `prepared`, a re-export rewrites nothing, a copy
    kept from an export made before this (no `prepared`) is cleaned IN PLACE
    rather than copied again, a target without the hook leaves the block, and
    adding Traktor to such a set cleans the kept copies;
  - local push: the shared folder's copies lose it, the ledger records it;
  - import: a copied file loses it, a file added in place is never touched.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import tempfile
import time
from pathlib import Path

os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="konduktor-fileblock-appdata-")
os.environ.pop("KONDUKTOR_NML", None)

import av  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from mutagen import File as MutagenFile  # noqa: E402
from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import APIC, ID3, PRIV, TIT2  # noqa: E402
from mutagen.mp4 import MP4, MP4Cover  # noqa: E402

from konduktor import exporter, exports, importer, local_push, prefs  # noqa: E402
from konduktor.adapters.traktor import file_block  # noqa: E402
from konduktor.adapters.traktor.adapter import TraktorAdapter  # noqa: E402
from konduktor.adapters.traktor.export import SKELETON  # noqa: E402
from konduktor.app_state import STATE  # noqa: E402
from konduktor.core import apps_running, pyav  # noqa: E402
from konduktor.core import export as core_export  # noqa: E402
from konduktor.core.export import ExportPayload, ExportTrack  # noqa: E402
from konduktor.core.model import GridMarker, Track, TrackCues  # noqa: E402
from konduktor.exporter import _read_manifest as read_manifest  # noqa: E402
from konduktor.jobs import JOBS  # noqa: E402

failed = False
ROOT = Path(tempfile.mkdtemp(prefix="konduktor-fileblock-"))
def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 90)).save(buf, "JPEG")
    return buf.getvalue()


JPEG = _jpeg()  # a real (tiny) cover, so the exporters read it


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


# ---- generated audio, each carrying a block that says 175 BPM ------------------------

def block(bpm: float) -> bytes:
    """A minimal `DMRT` tree as Traktor writes it: reversed 4CCs, each chunk
    = name + payload size + child count + payload."""
    def chunk(name: str, payload: bytes, children: int = 0) -> bytes:
        return name[::-1].encode() + struct.pack("<II", len(payload), children) + payload
    data = chunk("DATA", chunk("HBPM", struct.pack("<f", bpm)), 1)
    hdr = chunk("HDR ", chunk("VRSN", struct.pack("<I", 7)), 1)
    return chunk("DMRT", hdr + data, 2)


def music(seconds=6, sr=44100, seed=0):
    t = np.arange(int(sr * seconds)) / sr
    rng = np.random.default_rng(seed)
    beat = (np.sin(2 * np.pi * 2 * t) > 0.6).astype(float)
    return np.vstack([0.3 * (0.5 * np.sin(2 * np.pi * 90 * t) * beat + 0.2 * rng.standard_normal(t.size))
                      for _ in range(2)]).astype(np.float32)


def encode(path: Path, fmt: str, codec: str, frame: int, seed: int):
    x = music(seed=seed)
    with av.open(str(path), "w", format=fmt) as c:
        s = c.add_stream(codec, rate=44100, layout="stereo")
        s.bit_rate = 192_000
        for o in range(0, x.shape[1], frame):
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(x[:, o:o + frame]), format="fltp", layout="stereo")
            f.sample_rate, f.pts = 44100, o
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def make(path: Path, seed: int, *, with_block: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix
    if ext == ".mp3":
        encode(path, "mp3", "libmp3lame", 1152, seed)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=path.stem))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=JPEG))
        tags.add(PRIV(owner="SomeOtherApp", data=b"keep me"))
        if with_block:
            tags.add(PRIV(owner="TRAKTOR4", data=block(175.0)))
        tags.save(path, v2_version=3)
    elif ext == ".m4a":
        encode(path, "ipod", "aac", 1024, seed)
        m = MP4(path)
        if m.tags is None:
            m.add_tags()
        m.tags["\xa9nam"] = [path.stem]
        m.tags["covr"] = [MP4Cover(JPEG, imageformat=MP4Cover.FORMAT_JPEG)]
        m.save()
        if with_block:
            m = MP4(path)
            # As Traktor writes it: an atom mutagen cannot parse, kept raw.
            m.tags._failed_atoms["NITR"] = [b"\x00\x00\x00\x08NTKB" + b"\x00" * 4 + block(175.0)]
            m.save()
    elif ext == ".flac":
        sf.write(str(path), music(seed=seed).T, 44100, subtype="PCM_16")
        f = FLAC(path)
        f["title"] = path.stem
        if with_block:
            f["traktor4"] = base64.b64encode(block(175.0)).decode()
        f.save()
    return path


def pcm(path: Path) -> list[np.ndarray]:
    out = []
    with pyav.open_input(str(path)) as c:
        for s in c.streams.audio:
            c.seek(0)
            out.append(np.concatenate([f.to_ndarray().ravel() for f in c.decode(s)]))
    return out


def tag_keys(path: Path) -> set[str]:
    tags = MutagenFile(path).tags
    return set(tags.keys()) | set(getattr(tags, "_failed_atoms", {}))


print("== file_block on each format ==")
unit = ROOT / "unit"
for name in ("a.mp3", "b.m4a", "c.flac"):
    src = make(unit / "src" / name, seed=1)
    copy = unit / "copy" / (name + ".konduktor-partial")  # as the runners strip it
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, copy)
    before = tag_keys(copy)
    check(f"{name}: the block is found", file_block.has_block(copy))
    check(f"{name}: strip reports it removed one", file_block.strip(copy) is True)
    check(f"{name}: none is left", not file_block.has_block(copy))
    lost = before - tag_keys(copy)
    check(f"{name}: only the block is gone from the tags",
          len(lost) == 1 and ("TRAKTOR4" in next(iter(lost)) or next(iter(lost)) in ("NITR", "traktor4")),
          str(lost))
    check(f"{name}: the audio is sample-identical",
          all(np.array_equal(a, b) for a, b in zip(pcm(src), pcm(copy))))
    data, mtime = copy.read_bytes(), copy.stat().st_mtime_ns
    check(f"{name}: a second strip changes nothing, and does not rewrite the file",
          file_block.strip(copy) is False and copy.read_bytes() == data and copy.stat().st_mtime_ns == mtime)
mp3 = unit / "copy" / "a.mp3.konduktor-partial"
id3 = ID3(mp3)
check("mp3: a foreign PRIV frame and the cover stay", "PRIV:SomeOtherApp:keep me" in id3 and id3.getall("APIC"))
check("mp3: the file keeps its ID3 version (2.3)", id3.version[:2] == (2, 3))
check("m4a: the cover stays", "covr" in MP4(unit / "copy" / "b.m4a.konduktor-partial").tags)
clean = make(unit / "clean.mp3", seed=2, with_block=False)
data, mtime = clean.read_bytes(), clean.stat().st_mtime_ns
check("a file without a block is never rewritten",
      file_block.strip(clean) is False and clean.read_bytes() == data and clean.stat().st_mtime_ns == mtime)
junk = unit / "junk.mp3"
junk.write_bytes(b"ID3" + b"\xff" * 64)
check("an unreadable file is refused quietly", file_block.strip(junk) is False and not file_block.has_block(junk))


# ---- a source library whose grids (176) disagree with the files' blocks (175) ---------

lib_root = ROOT / "MyMusic"
files = [make(lib_root / "DnB" / n, seed=i) for i, n in enumerate(("one.mp3", "two.m4a", "three.flac"))]
files.append(make(lib_root / "DnB" / "plain.mp3", seed=9, with_block=False))
seed = []
for i, path in enumerate(files):
    track = Track(id=f"seed{i}", title=path.stem, artist="Test")
    cues = TrackCues(grid_markers=[GridMarker(start=0.25, bpm=176.0)], grid_locked=True)
    seed.append(ExportTrack(track=track, destination=path, cues=cues))
core_export.for_platform("traktor").write(
    ExportPayload(name="Source", tracks=seed, playlists=[], root_folder=True), lib_root)
source_bytes = {p: p.read_bytes() for p in files}
STATE.open(lib_root / "collection.nml")
ad = STATE.adapter
LIB = STATE.library_id
ids = [t.id for t in ad.tracks]
check("the source library holds the four tracks", len(ids) == 4, str(ids))


def run_job(kind, fn):
    job = JOBS.submit(kind, fn)
    while not job.finished:
        time.sleep(0.02)
    assert job.error is None, job.error
    return job.result


def copies(dest: Path) -> dict[str, Path]:
    return {p.name: p for p in (dest / "Contents").rglob("*") if p.is_file() and not p.name.startswith(".")}


def records(dest: Path) -> dict[str, dict]:
    return {Path(rel).name: r for rel, r in (read_manifest(dest).get("audio") or {}).items()}


def bpms(nml: Path) -> dict[str, float]:
    a = TraktorAdapter(nml)
    return {Path(a.audio_path(t.id)).name: a.track_cues(t.id).grid_markers[0].bpm for t in a.tracks}


print("== an export to Traktor ==")
dest = ROOT / "GIG"
gig = exports.create(LIB, name="GIG", targets=["traktor"], destination=str(dest))
exports.add(LIB, gig.id, track_ids=ids)
gig = exports.get(LIB, gig.id)
run_job("export", lambda h: exporter.run(ad, gig, exporter.plan(ad, gig), h))
out = copies(dest)
check("every track was copied", set(out) == {p.name for p in files}, str(sorted(out)))
check("no copy carries Traktor's block", not any(file_block.has_block(p) for p in out.values()))
check("the SOURCE files are untouched", all(p.read_bytes() == b for p, b in source_bytes.items()))
check("a file that had no block is copied byte for byte", out["plain.mp3"].read_bytes() == source_bytes[files[3]])
check("the covers are still in the copies",
      ID3(out["one.mp3"]).getall("APIC") and "covr" in MP4(out["two.m4a"]).tags)
recs = records(dest)
check("the manifest records each copy as prepared for Traktor",
      all(r.get("prepared") == ["traktor"] for r in recs.values()), str(recs))
check("and records the copy's size as it is now (after the strip)",
      all(r["copy_size"] == out[n].stat().st_size for n, r in recs.items()))
check("the exported grids are the library's (176)", all(abs(b - 176.0) < 0.01 for b in bpms(dest / "collection.nml").values()))

print("== a re-export rewrites nothing ==")
stamp = {n: (p.stat().st_ino, p.stat().st_mtime_ns) for n, p in out.items()}
run_job("export", lambda h: exporter.run(ad, gig, exporter.plan(ad, gig), h))
check("every copy kept as it was (same inode, same mtime)",
      {n: (p.stat().st_ino, p.stat().st_mtime_ns) for n, p in copies(dest).items()} == stamp)

print("== a copy kept from an export made before this fix is cleaned in place ==")
# Put the block back into one copy and drop `prepared`, as an older export's
# manifest and stick would be — the record matching the file, so it is kept.
old_copy = copies(dest)["one.mp3"]
tags = ID3(old_copy)
tags.add(PRIV(owner="TRAKTOR4", data=block(175.0)))
tags.save(old_copy, v2_version=3)
manifest = read_manifest(dest)
for rel, r in manifest["audio"].items():
    r.pop("prepared", None)
    if Path(rel).name == "one.mp3":
        st = old_copy.stat()
        r["copy_size"], r["copy_mtime_ns"] = st.st_size, st.st_mtime_ns
exporter.manifest_path(dest).write_text(json.dumps(manifest))
inode = old_copy.stat().st_ino
again = exporter.plan(ad, gig)
check("the old copy is planned as KEPT, not copied again",
      next(t for t in again.exportable if t.destination.name == "one.mp3").reuse is not None)
run_job("export", lambda h: exporter.run(ad, gig, again, h))
cleaned = copies(dest)["one.mp3"]
check("its block is gone", not file_block.has_block(cleaned))
check("in place: the same file, not a new copy", cleaned.stat().st_ino == inode)
check("every record says prepared again", all(r.get("prepared") == ["traktor"] for r in records(dest).values()))
check("and the next export keeps it as it is",
      next(t for t in exporter.plan(ad, gig).exportable if t.destination.name == "one.mp3").reuse is not None)

print("== a target without the hook leaves the block; adding Traktor cleans the kept copies ==")
pdest = ROOT / "PIONEER"
pio = exports.create(LIB, name="PIONEER", targets=["rekordbox_export"], destination=str(pdest))
exports.add(LIB, pio.id, track_ids=ids)
pio = exports.get(LIB, pio.id)
run_job("export", lambda h: exporter.run(ad, pio, exporter.plan(ad, pio), h))
pout = copies(pdest)
check("a Rekordbox Export's copies keep Traktor's block (none of its business)",
      file_block.has_block(pout["one.mp3"]) and file_block.has_block(pout["two.m4a"]))
check("and are recorded as prepared by nobody", all(r.get("prepared") == [] for r in records(pdest).values()))
inodes = {n: p.stat().st_ino for n, p in pout.items()}
pio = exports.update(LIB, pio.id, targets=["rekordbox_export", "traktor"])
run_job("export", lambda h: exporter.run(ad, pio, exporter.plan(ad, pio), h))
pout = copies(pdest)
check("with Traktor added, no copy carries the block", not any(file_block.has_block(p) for p in pout.values()))
check("cleaned in place, none copied again", {n: p.stat().st_ino for n, p in pout.items()} == inodes)
check("recorded as prepared for Traktor", all(r.get("prepared") == ["traktor"] for r in records(pdest).values()))

print("== a local push into this computer's Traktor ==")
laptop = ROOT / "laptop" / "collection.nml"
laptop.parent.mkdir(parents=True)
laptop.write_text(SKELETON, encoding="utf-8")
AUDIO = ROOT / "LocalAudio"
apps_running.running = lambda platforms: set()
prefs.update_prefs({"localLibraries": {"traktor": str(laptop), "rekordbox": str(ROOT / "none" / "master.db")},
                    "localAudioRoot": str(AUDIO), "localNormalise": False})
push = exports.create(LIB, name="Push", targets=["traktor"], destination="", kind="computer", mode="merge")
exports.add(LIB, push.id, track_ids=ids)
push = exports.get(LIB, push.id)
built = local_push.plan(ad, push)
check("the push can run", built.blocked is None, str(built.blocked))
run_job("local-push", lambda h: local_push.run(ad, push, built, h))
pushed = {p.name: p for p in AUDIO.rglob("*") if p.is_file() and p.suffix in (".mp3", ".m4a", ".flac")}
check("every track's copy is in the shared folder", set(pushed) == {p.name for p in files}, str(sorted(pushed)))
check("none of them carries Traktor's block", not any(file_block.has_block(p) for p in pushed.values()))
ledger = local_push._read_ledger()["audio"][str(AUDIO)]
check("the ledger records them as prepared for Traktor",
      all(r.get("prepared") == ["traktor"] for r in ledger.values()), str(ledger))
check("the laptop's collection has the library's grids (176)",
      all(abs(b - 176.0) < 0.01 for b in bpms(laptop).values()), str(bpms(laptop)))
check("the SOURCE files are still untouched", all(p.read_bytes() == b for p, b in source_bytes.items()))

print("== an import into a Traktor collection ==")
into = ROOT / "into" / "collection.nml"
into.parent.mkdir(parents=True)
into.write_text(SKELETON, encoding="utf-8")
dest_ad = TraktorAdapter(into)
folder = ROOT / "into" / "Imported"
run_job("import", lambda h: importer.run(ad, dest_ad, importer.plan(ad, dest_ad, folder, track_ids=ids[:2]), h))
imported = [p for p in folder.rglob("*") if p.is_file()]
check("the copied files arrived", len(imported) == 2, str(imported))
check("and carry no block", imported and not any(file_block.has_block(p) for p in imported))
ref = ROOT / "ref" / "collection.nml"
ref.parent.mkdir(parents=True)
ref.write_text(SKELETON, encoding="utf-8")
ref_ad = TraktorAdapter(ref)
run_job("import", lambda h: importer.run(ad, ref_ad, importer.plan(ad, ref_ad, None, track_ids=ids[:2]), h,
                                         reference=True))
check("a file added IN PLACE is never touched", all(p.read_bytes() == b for p, b in source_bytes.items()))
check("so it keeps its block (it is the user's file)", file_block.has_block(files[0]))

shutil.rmtree(ROOT, ignore_errors=True)
print("\nALL PASS" if not failed else "\nSOME CHECKS FAILED")
raise SystemExit(1 if failed else 0)
