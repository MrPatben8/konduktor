"""The two Pioneer export targets, against the same contract as Traktor's.

`test_export.py` proves a Traktor collection can be written from nothing. These
two are harder in a way that test cannot cover: each writes a **database plus
per-track analysis files**, so what "the library" means is no longer one path,
and the prep has to survive a genuine change of representation rather than a
re-render of the same format.

Three crossings are worth pinning because each fails SILENTLY:

  * **Key notation.** The generic `Track.key` is the SOURCE platform's display
    string — Traktor's "10m". Copying it verbatim puts "10m" where a Pioneer
    deck expects "Cm". The only correct crossing is via `key_wheel`/`key_mode`.
  * **Hot-cue slot numbering.** ANLZ uses a DENSE 1-based `hot_cue`; `master.db`
    uses a SPARSE `Kind` bank that skips 4. Reuse one for the other and every
    cue from pad D lands one pad too far along — a cue in the wrong place, not
    an error.
  * **The beatgrid changes shape.** Traktor's is a marker list; Pioneer's is
    every beat. It has to expand on the way out and collapse back identically.

Both exports are read back with Konduktor's OWN adapters, which is the strongest
check available without the hardware: the reader is the code that has to accept
what the writer produced, and it was written against real rekordbox output.
"""
import os
import tempfile
from pathlib import Path

os.environ["KONDUKTOR_DATA_DIR"] = tempfile.mkdtemp()

from konduktor.adapters.onelibrary.driver import OneLibraryDriver  # noqa: E402
from konduktor.adapters.rekordbox.driver import RekordboxDriver  # noqa: E402
from konduktor.adapters.rekordbox.projection import parse_key, render_key  # noqa: E402
from konduktor.adapters.traktor.driver import TraktorDriver  # noqa: E402
from konduktor.adapters.traktor.projection import parse_key as traktor_key_parse  # noqa: E402
from konduktor.core import export as core_export  # noqa: E402
from konduktor.core.export import ExportPayload, ExportPlaylist, ExportTrack  # noqa: E402
from konduktor.core.model import GridMarker, TrackCues  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


FIXTURE = Path(__file__).resolve().parents[1] / "collection.nml"
src = TraktorDriver().open(FIXTURE)

# A flexible multi-tempo grid and a track with cues past pad C: the two things
# most likely to be quietly wrong.
flexible = next(t for t in src.tracks if (t.grid_marker_count or 0) > 1)
cued = next(t for t in src.tracks
            if (t.cue_count or 0) >= 5 and t.id != flexible.id)
chosen = [flexible, cued]


def build(root: Path) -> ExportPayload:
    items = []
    for i, track in enumerate(chosen):
        audio = root / ("House" if i % 2 else "Techno") / Path(track.filepath).name
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"\0" * 4096)
        items.append(ExportTrack(track=track, destination=audio,
                                 cues=src.track_cues(track.id)))
    return ExportPayload(
        name="GIG", tracks=items,
        playlists=[ExportPlaylist(name="Peak Time",
                                  track_ids=[t.id for t in chosen],
                                  folders=["House"])],
    )


def hotcues(cues):
    return sorted((c.slot, round(c.start, 2)) for c in cues.cues if c.role == "hotcue")


def grid(cues):
    return [(round(m.start, 2), round(m.bpm, 2)) for m in cues.grid_markers]


print("== the key crossing, as a unit ==")
# Traktor Open Key -> Camelot wheel -> Pioneer's musical naming.
for traktor_key, expected in (("10m", "Cm"), ("1m", "Am"), ("12d", "F")):
    wheel, mode = traktor_key_parse(traktor_key)
    check(f"{traktor_key} renders as {expected}", render_key(wheel, mode) == expected,
          render_key(wheel, mode))
check("all 24 keys round-trip through the wheel",
      all(parse_key(render_key(*parse_key(n))) == parse_key(n)
          for n in ("Abm", "Am", "Bbm", "Bm", "Cm", "C#m", "Dm", "Ebm", "Em",
                    "Fm", "F#m", "Gm", "C", "Db", "D", "Eb", "E", "F", "F#",
                    "G", "Ab", "A", "Bb", "B")))
check("an unknown key stays blank rather than invented",
      render_key(None, None) is None and render_key(99, "minor") is None)


for platform, reopen in (
    ("onelibrary", lambda r: OneLibraryDriver().open(r)),
    ("rekordbox", lambda r: RekordboxDriver().open(r / "master.db")),
):
    print(f"\n== {platform}: a library written from nothing ==")
    exporter = core_export.for_platform(platform)
    check("it is a registered target", exporter is not None)
    caps = exporter.capabilities()
    # The OneLibrary ADAPTER reports read-only — Konduktor cannot edit a drive
    # in place. An export target is a different question and the answer is yes.
    check("static capabilities say it is writable", caps.writable, str(caps.readonly_cause))

    root = Path(tempfile.mkdtemp()) / "GIG"
    payload = build(root)
    written = exporter.write(payload, root)

    check("the library exists", written.library.is_file(), str(written.library))
    check("and is inside the destination",
          written.library.is_relative_to(root))
    # Both write a database PLUS per-track analysis files. Under-reporting them
    # leaves orphans a re-export cannot clear, because the manifest is the list.
    check("analysis files are REPORTED, not just written",
          len(written.extra) >= len(chosen), f"{len(written.extra)} for {len(chosen)} tracks")
    check("every reported path exists", all(p.is_file() for p in written.extra))
    check("all_paths includes the library",
          written.library in written.all_paths
          and len(written.all_paths) == len(written.extra) + 1)

    back = reopen(root)
    check("it re-opens with Konduktor's own adapter", len(back.tracks) == len(chosen),
          f"{len(back.tracks)} of {len(chosen)}")
    by_title = {t.title: t for t in back.tracks}
    check("titles survived", all(t.title in by_title for t in chosen))
    check("artists survived",
          all(by_title[t.title].artist == t.artist for t in chosen if t.artist))
    check("bpm survived",
          all(abs((by_title[t.title].bpm or 0) - (t.bpm or 0)) < 0.02
              for t in chosen if t.bpm))

    print(f"-- {platform}: the key was CONVERTED, not copied")
    for track in chosen:
        if not track.key:
            continue
        out = by_title[track.title].key
        wheel, mode = traktor_key_parse(track.key)
        check(f"{track.key!r} -> {out!r}", out == render_key(wheel, mode),
              f"expected {render_key(wheel, mode)}")
        check("and is NOT the source's own notation", out != track.key)

    print(f"-- {platform}: the prep crossed")
    for track in chosen:
        s, o = src.track_cues(track.id), back.track_cues(by_title[track.title].id)
        check(f"{track.title[:22]}: hot cues keep their PAD",
              hotcues(o) == hotcues(s), f"{hotcues(o)} vs {hotcues(s)}")
        check(f"{track.title[:22]}: the grid survives expansion + collapse",
              grid(o) == grid(s), f"{grid(o)} vs {grid(s)}")
    check("the flexible grid really is multi-tempo",
          len({b for _a, b in grid(src.track_cues(flexible.id))}) > 1)
    check("and a cue past pad C came across",
          max((c.slot or 0) for c in src.track_cues(cued.id).cues) >= 3)

    print(f"-- {platform}: the playlist tree")
    tree = {}

    def walk(nodes, path=()):
        for n in nodes:
            tree[path + (n.name,)] = n
            walk(n.children, path + (n.name,))

    tree.clear()
    walk(back.playlist_tree())
    check("a folder named after the export", ("GIG",) in tree)
    check("nested folders are preserved", ("GIG", "House") in tree)
    check("with the playlist inside",
          ("GIG", "House", "Peak Time") in tree
          and tree[("GIG", "House", "Peak Time")].count == len(chosen))

    print(f"-- {platform}: re-exporting replaces rather than doubling")
    again = exporter.write(build(root), root)
    reopened = reopen(root)
    check("the track count did not double", len(reopened.tracks) == len(chosen),
          str(len(reopened.tracks)))
    check("nor did the tree", len(reopened.playlist_tree()) == 1)
    check("and it still reports its files", len(again.all_paths) == len(written.all_paths))

print("\n== OneLibrary cue tags are rekordbox's BYTES, not merely parseable ==")
# pyrekordbox ignores `len_header` and the constants, so an early writer with a
# 12-byte PCPT header round-tripped through our own reader perfectly while
# rekordbox showed no cues and no grid. So: read the fixture's cues with the
# reader, pack them with the writer, and demand rekordbox's exact bytes back.
import struct  # noqa: E402

from konduktor.adapters.onelibrary.export import OneLibraryExporter, _file_type, _kbps  # noqa: E402
from konduktor.adapters.rekordbox import anlz_writer as W  # noqa: E402

ol_root = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
ol = OneLibraryDriver().open(ol_root)
demo = next(t for t in ol.tracks if "Demo Track 1" in (t.title or ""))
demo_item = ExportTrack(track=demo, destination=ol_root / demo.id.lstrip("/"),
                        cues=ol.track_cues(demo.id))
demo_beats = OneLibraryExporter._beats(demo_item)
demo_cues = OneLibraryExporter._cue_dicts(demo_item, demo_beats)


def _real_cue_tags(path: Path) -> list[bytes]:
    b = path.read_bytes()
    off, out = struct.unpack(">I", b[4:8])[0], []
    while off < len(b):
        kind, length = b[off:off + 4], struct.unpack(">I", b[off + 8:off + 12])[0]
        if kind in (b"PCOB", b"PCO2"):
            out.append(b[off:off + length])
        off += length
    return out


anlz = ol_root / "PIONEER" / "USBANLZ" / "P016" / "0000875E" / "ANLZ0000"
for suffix, extended, shape in ((".DAT", False, "hot + memory PCOB"),
                                (".EXT", True, "hot + memory PCOB, hot + memory PCO2")):
    real = _real_cue_tags(anlz.with_suffix(suffix))
    ours = W.cue_tags(demo_cues, extended=extended)
    check(f"{suffix}: the same tags in the same order ({shape})", len(real) == len(ours),
          f"{len(ours)} vs rekordbox's {len(real)}")
    check(f"{suffix}: every cue tag is byte-identical to rekordbox's",
          all(r == o for r, o in zip(real, ours)),
          [i for i, (r, o) in enumerate(zip(real, ours)) if r != o])
check("a track with no cues still gets every list, empty",
      [t[:4] for t in W.cue_tags([], extended=True)] == [b"PCOB", b"PCOB", b"PCO2", b"PCO2"])
check("a Traktor STEM file is an M4A to a Pioneer player",
      _file_type(Path("x.stem.m4a")) == 4 and _file_type(Path("x.MP3")) == 1)
check("bitrate is written in kbps", _kbps(320_000) == 320 and _kbps(None) is None)
check("the reader hands back bits per second, as the table expects",
      all((t.bitrate or 0) >= 1000 for t in ol.tracks if t.bitrate))

print("\n== every OneLibrary .DAT carries what rekordbox needs to show a grid ==")
# Measured by stripping tags from a rekordbox-written stick: with only PPTH +
# PQTZ + PCOB in the .DAT, rekordbox shows NO grid and NO hot cues. PVBR alone
# is not enough, nor PWAV + PWV2 alone.
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from konduktor.core import waveform  # noqa: E402

REKORDBOX_DAT_ORDER = [b"PPTH", b"PVBR", b"PQTZ", b"PWAV", b"PWV2", b"PCOB", b"PCOB"]


def _tag_kinds(path: Path) -> list[bytes]:
    b = path.read_bytes()
    off, out = struct.unpack(">I", b[4:8])[0], []
    while off < len(b):
        out.append(b[off:off + 4])
        off += struct.unpack(">I", b[off + 8:off + 12])[0]
    return out


def _tag(path: Path, kind: bytes) -> bytes:
    b = path.read_bytes()
    off = struct.unpack(">I", b[4:8])[0]
    while off < len(b):
        tl = struct.unpack(">I", b[off + 8:off + 12])[0]
        if b[off:off + 4] == kind:
            return b[off:off + tl]
        off += tl
    raise LookupError(kind)


wf_root = Path(tempfile.mkdtemp())
# Placeholder bytes nothing can decode: the grid must STILL be accepted.
dead = wf_root / "Contents" / "dead.mp3"
dead.parent.mkdir(parents=True)
dead.write_bytes(b"\0" * 4096)
# Real audio: 8 s of continuous noise, loud for the first half, quiet after.
# (Continuous: a column is 20 ms here, so sparse clicks would leave most silent.)
sr = 22050
tone = np.random.default_rng(0).uniform(-1, 1, sr * 8).astype(np.float32)
tone[: sr * 4] *= 0.5
tone[sr * 4:] *= 0.02
live = wf_root / "Contents" / "live.wav"
sf.write(live, tone, sr)

grid = TrackCues(track_id="x", cues=[], grid_markers=[GridMarker(start=0.0, bpm=125.0)])
dead_t = cued.model_copy(update={"id": "dead", "title": "dead"})
live_t = cued.model_copy(update={"id": "live", "title": "live", "length": 8})
seen: list[str] = []
wf_written = core_export.for_platform("onelibrary").write(
    ExportPayload(name="WF", tracks=[ExportTrack(track=dead_t, destination=dead, cues=grid),
                                     ExportTrack(track=live_t, destination=live, cues=grid)],
                  checkpoint=seen.append),
    wf_root,
)
dats = [p for p in wf_written.extra if p.suffix == ".DAT"]
check("two tracks, two .DAT files", len(dats) == 2)
for dat in dats:
    check(f"{dat.parent.name}: rekordbox's tag set, in rekordbox's order",
          _tag_kinds(dat) == REKORDBOX_DAT_ORDER, _tag_kinds(dat))
pvbr = _tag(dats[0], b"PVBR")
check("PVBR is 400 seek points plus a trailing length, as rekordbox writes",
      len(pvbr) == 1620, len(pvbr))
check("the writer reported each track to the status bar",
      len(seen) == 2 and all("Analysing" in m for m in seen), seen)

cols = waveform.columns(live, 400)
check("decodable audio is measured", cols is not None and abs(cols.duration - 8.0) < 0.05)
check("a loud half measures louder than a quiet half",
      cols is not None and cols.rms[:200].mean() > 5 * cols.rms[200:].mean())
check("undecodable bytes are no overview, not an error", waveform.columns(dead, 400) is None)
heights = {dat: np.frombuffer(_tag(dat, b"PWAV")[20:], np.uint8) & 31 for dat in dats}
check("the undecodable file still gets a (flat) preview", any(h.max() == 0 for h in heights.values()))
check("the decodable one gets a real one, loud where the audio is loud",
      any(h[:200].mean() > h[200:].mean() + 3 for h in heights.values()))


class _Stop(Exception):
    pass


def _cancel_on_second(message: str, calls=[]):  # noqa: B006 — deliberate counter
    calls.append(message)
    if len(calls) == 2:
        raise _Stop()


try:
    core_export.for_platform("onelibrary").write(
        ExportPayload(name="WF", tracks=[ExportTrack(track=dead_t, destination=dead, cues=grid),
                                         ExportTrack(track=live_t, destination=live, cues=grid)],
                      checkpoint=_cancel_on_second),
        wf_root,
    )
    check("a cancel between tracks stops the writer", False, "it finished")
except _Stop:
    check("a cancel between tracks stops the writer", True)

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
