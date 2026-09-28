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
from konduktor.core.model import CuePoint, GridMarker, TrackCues  # noqa: E402

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
        # Same pads, same places — to the millisecond Pioneer stores (a position
        # now crosses a 25 ms clock shift and back, so 2-dp rounding can flip).
        ho, hs = hotcues(o), hotcues(s)
        check(f"{track.title[:22]}: hot cues keep their PAD",
              [p for p, _ in ho] == [p for p, _ in hs]
              and all(abs(a - b) <= 0.011 for (_, a), (_, b) in zip(ho, hs)),
              f"{ho} vs {hs}")
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
from konduktor.adapters.rekordbox import artwork as artwork_mod  # noqa: E402

ol_root = Path(__file__).resolve().parent / "fixtures" / "onelibrary"
ol = OneLibraryDriver().open(ol_root)
demo = next(t for t in ol.tracks if "Demo Track 1" in (t.title or ""))
demo_item = ExportTrack(track=demo, destination=ol_root / demo.id.lstrip("/"),
                        cues=ol.track_cues(demo.id))
demo_beats = OneLibraryExporter._beats(demo_item)
# The reader moved the fixture's positions onto the decoded clock; packing puts
# them back on rekordbox's, so the bytes must come back unchanged.
from konduktor.adapters.rekordbox import timebase  # noqa: E402
demo_cues = OneLibraryExporter._cue_dicts(demo_item, demo_beats, timebase.offset(demo_item.destination))


def _real_cue_tags(path: Path) -> list[bytes]:
    b = path.read_bytes()
    off, out = struct.unpack(">I", b[4:8])[0], []
    while off < len(b):
        kind, length = b[off:off + 4], struct.unpack(">I", b[off + 8:off + 12])[0]
        if kind in (b"PCOB", b"PCO2"):
            out.append(b[off:off + length])
        off += length
    return out


def _mask_colour(tag: bytes) -> bytes:
    """A PCO2 tag with each entry's code + RGB (after its comment) zeroed."""
    if tag[:4] != b"PCO2":
        return tag
    out, off = bytearray(tag), struct.unpack(">I", tag[4:8])[0]
    while off < len(tag):
        length = struct.unpack(">I", tag[off + 8:off + 12])[0]
        at = off + 44 + struct.unpack(">I", tag[off + 40:off + 44])[0]
        out[at:at + 4] = bytes(4)
        off += length
    return bytes(out)


anlz = ol_root / "PIONEER" / "USBANLZ" / "P016" / "0000875E" / "ANLZ0000"
for suffix, extended, shape in ((".DAT", False, "hot + memory PCOB"),
                                (".EXT", True, "hot + memory PCOB, hot + memory PCO2")):
    real = _real_cue_tags(anlz.with_suffix(suffix))
    ours = W.cue_tags(demo_cues, extended=extended)
    check(f"{suffix}: the same tags in the same order ({shape})", len(real) == len(ours),
          f"{len(ours)} vs rekordbox's {len(real)}")
    # Byte-identical EXCEPT the four colour bytes of each PCP2 entry. The
    # fixture's cues carry rekordbox's OLD form (code 0 + an arbitrary RGB); the
    # writer now stores a palette code, which is what rekordbox 7 draws from —
    # pinned separately below against a real rekordbox 7 palette export.
    check(f"{suffix}: every cue tag is byte-identical to rekordbox's (colour aside)",
          all(_mask_colour(r) == _mask_colour(o) for r, o in zip(real, ours)),
          [i for i, (r, o) in enumerate(zip(real, ours)) if _mask_colour(r) != _mask_colour(o)])
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
                  checkpoint=lambda m, **_progress: seen.append(m)),
    wf_root,
)
dats = [p for p in wf_written.extra if p.suffix == ".DAT"]
check("two tracks, two .DAT files", len(dats) == 2)
check("every file written is reported, so a re-export can clear it",
      all(p.exists() for p in wf_written.all_paths)
      and {p.suffix for p in wf_written.extra} == {".DAT", ".EXT", ".2EX"})
for dat in dats:
    check(f"{dat.parent.name}: rekordbox's tag set, in rekordbox's order",
          _tag_kinds(dat) == REKORDBOX_DAT_ORDER, _tag_kinds(dat))
pvbr = _tag(dats[0], b"PVBR")
check("PVBR is 400 seek points plus a trailing length, as rekordbox writes",
      len(pvbr) == 1620, len(pvbr))
check("the writer reported each track to the status bar",
      len(seen) == 2 and all("Analysing" in m for m in seen), seen)

# The drawn waveforms: rekordbox 7's song-list preview and scrolling deck read
# the .2EX (3-band) and .EXT. Same tags, same order as rekordbox writes.
live_dat = next(d for d in dats if (d.with_suffix(".2EX")).exists())
check("a decodable track gets all three analysis files",
      live_dat.with_suffix(".EXT").exists() and live_dat.with_suffix(".2EX").exists())
check(".EXT: rekordbox's tag order",
      _tag_kinds(live_dat.with_suffix(".EXT")) == [b"PPTH", b"PWV3", b"PCOB", b"PCOB", b"PCO2", b"PCO2", b"PWV5", b"PWV4"],
      _tag_kinds(live_dat.with_suffix(".EXT")))
check(".2EX: rekordbox's tag order",
      _tag_kinds(live_dat.with_suffix(".2EX")) == [b"PPTH", b"PWV7", b"PWV6", b"PWVC"],
      _tag_kinds(live_dat.with_suffix(".2EX")))
pwv7 = _tag(live_dat.with_suffix(".2EX"), b"PWV7")
n7 = struct.unpack(">I", pwv7[16:20])[0]
check("PWV7 is 150 frames a second, 3 bytes each", abs(n7 - 8 * 150) <= 2 and len(pwv7) == 24 + 3 * n7, n7)
check("PWV6 is 1200 overview columns of 3 bytes", len(_tag(live_dat.with_suffix(".2EX"), b"PWV6")) == 20 + 3600)
# The song list's colour overview: without it rekordbox draws the row plain blue.
pwv4 = _tag(live_dat.with_suffix(".EXT"), b"PWV4")
check("PWV4 is 1200 columns of 6 bytes, with rekordbox's header",
      len(pwv4) == 24 + 7200 and pwv4[12:24] == bytes.fromhex("00000006000004b000000000"), pwv4[12:24].hex())
p4 = np.frombuffer(pwv4[24:], np.uint8).reshape(-1, 6).astype(int)
check("its colour overview is loud where the audio is loud (height byte)",
      p4[:600, 2].mean() > p4[600:, 2].mean() + 10, (p4[:600, 2].mean(), p4[600:, 2].mean()))
p7 = np.frombuffer(pwv7[24:], np.uint8).reshape(-1, 3).astype(int)
check("the 3-band detail is loud where the audio is loud",
      p7[: n7 // 2].mean() > p7[n7 // 2:].mean() + 10, (p7[: n7 // 2].mean(), p7[n7 // 2:].mean()))
dead_dat = next(d for d in dats if d != live_dat)
check("an undecodable track still gets its .EXT cue lists, but no invented waveform",
      dead_dat.with_suffix(".EXT").exists() and not dead_dat.with_suffix(".2EX").exists()
      and b"PWV3" not in _tag_kinds(dead_dat.with_suffix(".EXT")))

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


def _cancel_on_second(message: str, calls=[], **_progress):  # noqa: B006 — deliberate counter
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

print("\n== positions cross onto rekordbox's clock (25 ms later on MP3/AAC) ==")
# Measured: rekordbox's grids sit a median 25.0 ms later than the decoded audio
# on 16 MP3s and 24.5 ms on 4 M4As, 0 on WAV — the codec delay it does not trim.
# Written at the decoded time, every cue showed ~25 ms early in rekordbox.
check("an MP3's offset is 1105 samples", abs(timebase.offset("a.mp3") - 1105 / 44100) < 1e-9)
check("a Traktor STEM (AAC) has it too", timebase.offset("a.stem.m4a") > 0.02)
check("a lossless file has none", timebase.offset("a.wav") == 0.0 and timebase.offset("a.flac") == 0.0)
check("a file of unknown location has none", timebase.offset(None) == 0.0)
probe = [CuePoint(type="cue", role="hotcue", start=30.0, length=0.0, slot=1)]
for name, want in (("probe.mp3", 30025), ("probe.wav", 30000)):
    it = ExportTrack(track=cued, destination=Path(name), cues=TrackCues(track_id="p", cues=probe, grid_markers=[]))
    got = OneLibraryExporter._cue_dicts(it, [], timebase.offset(it.destination))[0]["time_ms"]
    check(f"a cue at 30.000 s in {name} is stored at {want} ms", got == want, got)
check("reading it back undoes it exactly",
      abs(timebase.from_pioneer(30025 / 1000, timebase.offset("a.mp3")) - 30.0) < 0.0005)
check("a position inside the codec delay reads as the track's start",
      timebase.from_pioneer(0.010, timebase.offset("a.mp3")) == 0.0)

print("\n== cue colours cross as rekordbox PALETTE CODES ==")
# rekordbox draws a hot cue from the palette code, not the RGB: an export writing
# code 0 had every cue drawn in rekordbox's defaults (green cues, orange loops).
# The table was measured from a rekordbox 7 export of all 16 swatches.
from konduktor.adapters.rekordbox import palette  # noqa: E402

check("a measured swatch keeps its exact code and RGB",
      palette.code_for("#1AFF00") == (0x16, (0x1A, 0xFF, 0x00))
      and palette.code_for("#FF00A1") == (0x31, (0xFF, 0x00, 0xA1)))
check("a Konduktor cue (blue) is rekordbox's light blue", palette.code_for("#4D94FF")[0] == 0x05)
check("a Konduktor loop (green) is rekordbox's GREEN, not the nearer-hued teal",
      palette.code_for("#3DDC84")[0] == 0x16)
check("white has no swatch: code 0, RGB kept", palette.code_for("#FFFFFF") == (0, (255, 255, 255)))
check("no colour is code 0, black", palette.code_for(None) == (0, (0, 0, 0)))
check("any other colour takes the nearest-hue swatch, and that swatch's RGB",
      palette.code_for("#FF8C00") == (0x26, palette.PALETTE[0x26]))
typed = [CuePoint(type="cue", role="hotcue", start=1.0, length=0.0, slot=0),
         CuePoint(type="loop", role="hotcue", start=2.0, length=1.0, slot=1),
         CuePoint(type="cue", role="hotcue", start=3.0, length=0.0, slot=2, color="#FFFFFF"),
         CuePoint(type="cue", role="memory", start=4.0, length=0.0, slot=None)]
got = {c["hot_cue"]: (c["code"], c["rgb"]) for c in OneLibraryExporter._cue_dicts(
    ExportTrack(track=cued, destination=Path("t.wav"),
                cues=TrackCues(track_id="t", cues=typed, grid_markers=[])), [], 0.0)}
check("an uncoloured Traktor cue exports blue, a loop green, the grid cue white",
      got[1][0] == 0x05 and got[2][0] == 0x16 and got[3] == (0, (255, 255, 255)), got)
check("a memory cue stays uncoloured, as rekordbox writes it", got[0] == (0, (0, 0, 0)), got[0])
entry = W.cue_tags([{"hot_cue": 1, "kind": 1, "time_ms": 1000, "loop_ms": None,
                     "rgb": (0x1A, 0xFF, 0x00), "code": 0x16, "beats": None}], extended=True)[2]
check("the code is packed where rekordbox 7 puts it — byte 44, before the RGB",
      entry[20 + 44:20 + 48] == bytes([0x16, 0x1A, 0xFF, 0x00]), entry[64:68].hex())

print("\n== artwork crosses the way rekordbox 7 writes it ==")
# Measured on a rekordbox 7 export: PIONEER/Artwork/00001/{a,b}<n>{,_m}.jpg at
# 80 and 240 px (a == b byte for byte), baseline JPEG q85 4:2:0, non-square art
# letterboxed onto black, image.path -> the b file, content.image_id -> the row.
import io  # noqa: E402

from PIL import Image, JpegImagePlugin  # noqa: E402

art_root = Path(tempfile.mkdtemp())
art_audio = art_root / "Contents" / "art.wav"
art_audio.parent.mkdir(parents=True)
sf.write(art_audio, tone, sr)
plain_audio = art_root / "Contents" / "plain.wav"
sf.write(plain_audio, tone, sr)
wide = Image.new("RGB", (400, 100), (220, 30, 30))          # a wide red banner
png = io.BytesIO(); wide.save(png, format="PNG")
with_art = ExportTrack(track=live_t.model_copy(update={"id": "art", "title": "art"}),
                       destination=art_audio, cues=grid, art=(png.getvalue(), "image/png"))
without = ExportTrack(track=live_t.model_copy(update={"id": "plain", "title": "plain"}),
                      destination=plain_audio, cues=grid)
art_written = core_export.for_platform("onelibrary").write(
    ExportPayload(name="ART", tracks=[with_art, without]), art_root)
folder = art_root / "PIONEER" / "Artwork" / "00001"
names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
check("one image: a1, a1_m, b1, b1_m — and none for the track without art",
      names == ["a1.jpg", "a1_m.jpg", "b1.jpg", "b1_m.jpg"], names)
check("every art file is reported, so a re-export can clear it",
      all((folder / n) in art_written.extra for n in names))
check("the a and b copies are byte-identical, as rekordbox writes them",
      (folder / "a1.jpg").read_bytes() == (folder / "b1.jpg").read_bytes()
      and (folder / "a1_m.jpg").read_bytes() == (folder / "b1_m.jpg").read_bytes())
small_img, medium_img = Image.open(folder / "b1.jpg"), Image.open(folder / "b1_m.jpg")
check("80 px and 240 px", small_img.size == (80, 80) and medium_img.size == (240, 240))
ref = io.BytesIO(); Image.new("RGB", (8, 8)).save(ref, format="JPEG", quality=85, subsampling=2)
check("baseline JPEG, quality 85, 4:2:0 — rekordbox's own tables",
      medium_img.quantization == Image.open(ref).quantization
      and JpegImagePlugin.get_sampling(medium_img) == 2 and not medium_img.info.get("progressive"))
px = np.asarray(medium_img.convert("RGB")).astype(int)
check("a wide cover is letterboxed onto black, not cropped or stretched",
      px[:40].mean() < 8 and px[-40:].mean() < 8 and px[120, 120, 0] > 150,
      (px[:40].mean(), px[120, 120].tolist()))
reopened = OneLibraryDriver().open(art_root)
by_title_art = {t.title: t for t in reopened.tracks}
served = reopened.cover_art(by_title_art["art"].id)
check("Konduktor's reader serves it back (the 240 px file)",
      served is not None and served[0] == (folder / "b1_m.jpg").read_bytes())
check("and a track without art has none", reopened.cover_art(by_title_art["plain"].id) is None)

print("\n== content.contentLink is what rekordbox writes ==")
# Measured: blanking it on ONE track of a rekordbox-written stick turned that
# row's song-list preview plain blue and put a "?" beside CUE — exactly what
# every row of Konduktor's exports showed while it was NULL.
import sqlcipher3.dbapi2 as _sq  # noqa: E402
from pyrekordbox.devicelib_plus.database import BLOB as _BLOB  # noqa: E402
from pyrekordbox.utils import deobfuscate as _deob  # noqa: E402

_con = _sq.connect(str(art_written.library))
_con.execute(f"PRAGMA key='{_deob(_BLOB)}'")
links = {r[0] for r in _con.execute("SELECT contentLink FROM content")}
bits = {r[0] for r in _con.execute("SELECT analysedBits FROM content")}
_con.close()
check("every track carries contentLink 788224, beside analysedBits 41",
      links == {788224} and bits == {41}, (links, bits))
check("an unreadable cover costs the artwork, not the export",
      artwork_mod.pioneer_jpegs(b"not an image") is None)

print("\n== a Rekordbox library carries masterPlaylists6.xml, vouching for every playlist ==")
# Measured by swapping an export into a real Rekordbox 7: without this file
# Rekordbox opened the library, generated one listing our playlists with
# Timestamp 0, and showed NONE of them in its sidebar.
import xml.etree.ElementTree as ET  # noqa: E402

rb_root = Path(tempfile.mkdtemp())
rb_written = core_export.for_platform("rekordbox").write(build(rb_root), rb_root)
pl_xml = rb_root / "masterPlaylists6.xml"
check("the file is written beside master.db", pl_xml.is_file())
check("and reported, so a re-export can clear it", pl_xml in rb_written.all_paths)
nodes = ET.parse(pl_xml).getroot().find("PLAYLISTS").findall("NODE")
rb_db = RekordboxDriver().open(rb_root / "master.db")
flat = []


def _walk(ns):
    for n in ns:
        flat.append(n)
        _walk(n.children)


_walk(rb_db.playlist_tree())
check("one node per playlist and folder in the database", len(nodes) == len(flat), (len(nodes), len(flat)))
_rbc = _sq.connect(str(rb_root / "master.db"))
from pyrekordbox.masterdb.database import BLOB as _MBLOB  # noqa: E402
_rbc.execute(f"PRAGMA key='{_deob(_MBLOB)}'")
_links = {r[0] for r in _rbc.execute("SELECT ContentLink FROM djmdContent")}
_rbc.close()
check("every track carries ContentLink 0x2C060E (coloured preview, no '?')",
      _links == {2885134}, _links)
check("every node carries a real timestamp, not 0",
      nodes and all(int(n.get("Timestamp", "0")) > 0 for n in nodes), [n.get("Timestamp") for n in nodes])
rb_db.close()

print("\n== the Rekordbox target: cue colours, memory cues and artwork ==")
rbp_root = Path(tempfile.mkdtemp())
rbp_audio = rbp_root / "Contents" / "parity.wav"
rbp_audio.parent.mkdir(parents=True)
sf.write(rbp_audio, tone, sr)
parity_cues = [
    CuePoint(type="cue", role="hotcue", start=1.0, length=0.0, slot=0),                   # -> light blue
    CuePoint(type="loop", role="hotcue", start=2.0, length=0.96, slot=1),                 # -> green
    CuePoint(type="cue", role="hotcue", start=0.0, length=0.0, slot=2, color="#FFFFFF"),  # grid cue
    CuePoint(type="cue", role="memory", start=4.0, length=0.0, slot=None),
]
parity = ExportTrack(track=live_t.model_copy(update={"id": "parity", "title": "parity"}),
                     destination=rbp_audio, art=(png.getvalue(), "image/png"),
                     cues=TrackCues(track_id="parity", cues=parity_cues,
                                    grid_markers=[GridMarker(start=0.0, bpm=125.0)]))
rbp_written = core_export.for_platform("rekordbox").write(ExportPayload(name="PARITY", tracks=[parity]), rbp_root)

_c = _sq.connect(str(rbp_root / "master.db"))
_c.execute(f"PRAGMA key='{_deob(_MBLOB)}'")
cue_rows = _c.execute("SELECT Kind, ColorTableIndex, Color, InMsec FROM djmdCue ORDER BY InMsec").fetchall()
mirror = _c.execute("SELECT Cues FROM contentCue").fetchone()
image_path = _c.execute("SELECT ImagePath FROM djmdContent").fetchone()[0]
_c.close()
by_kind = {k: (cti, col) for k, cti, col, _ms in cue_rows}
check("four cue rows: three hot cues and a memory cue (Kind 0)",
      sorted(k for k, *_ in cue_rows) == [0, 1, 2, 3], cue_rows)
check("a plain cue is rekordbox's light blue (code 5)", by_kind.get(1) == (0x05, -1), by_kind.get(1))
check("a loop is rekordbox's green (code 22)", by_kind.get(2) == (0x16, -1), by_kind.get(2))
check("the white grid cue has no swatch, so stays uncoloured", by_kind.get(3) == (None, -1), by_kind.get(3))
check("the memory cue is written, uncoloured", by_kind.get(0) == (None, -1), by_kind.get(0))
check("the contentCue mirror holds all four, colours included",
      mirror is not None and mirror[0].count('"Kind"') == 4 and '"ColorTableIndex": 22' in mirror[0].replace(":22", ": 22"),
      mirror and mirror[0][:200])

art_dir = rbp_root / "share" / image_path.lstrip("/").rsplit("/", 1)[0] if image_path else None
check("ImagePath names artwork.jpg under the track's UUID folder",
      bool(image_path) and image_path.startswith("/PIONEER/Artwork/") and image_path.endswith("/artwork.jpg"), image_path)
sizes = {p.name: Image.open(p).size for p in art_dir.iterdir()} if art_dir and art_dir.exists() else {}
check("three JPEGs: the cover fit to 800 px (aspect kept), 240 and 80 px squares",
      sizes == {"artwork.jpg": (400, 100), "artwork_m.jpg": (240, 240), "artwork_s.jpg": (80, 80)}, sizes)
check("every art file is reported, so a re-export can clear it",
      art_dir is not None and all(p in rbp_written.extra for p in art_dir.iterdir()))
rb_back = RekordboxDriver().open(rbp_root / "master.db")
t_back = rb_back.tracks[0]
served = rb_back.cover_art(t_back.id)
check("Konduktor's Rekordbox reader serves the cover back",
      served is not None and served[0] == (art_dir / "artwork.jpg").read_bytes())
memory_back = [c for c in rb_back.track_cues(t_back.id).cues if c.role == "memory"]
check("and reads the memory cue back, at its place",
      len(memory_back) == 1 and abs(memory_back[0].start - 4.0) < 0.002, memory_back)
rb_back.close()

print("\n== overview columns sit where their audio is (no drift) ==")
# np.array_split gives the FIRST n % k chunks an extra frame, so every early
# overview column held later audio than its position: ~1 bar early mid-track in
# rekordbox, right again near the end. A burst at a known time, with a frame
# count whose remainder is large (24,420 % 1200 = 420), must land in its column.
from konduktor.core.waveform import even_slices  # noqa: E402

n_frames = 24420
check("even_slices covers every frame once, in order",
      [x.start for x in even_slices(n_frames, 1200)][1:] == [x.stop for x in even_slices(n_frames, 1200)][:-1]
      and even_slices(n_frames, 1200)[-1].stop == n_frames)
for when in (0.35, 0.5, 0.9):
    burst = np.full((n_frames, 3), 1.0)
    at = int(when * n_frames)
    burst[at:at + 40] = 100.0
    ext_t, two_t = W.waveform_tags(burst)
    tags_t = {x[:4]: x for x in ext_t + two_t}
    col6 = int(np.argmax(np.frombuffer(tags_t[b"PWV6"][20:], np.uint8).reshape(-1, 3)[:, 0]))
    col4 = int(np.argmax(np.frombuffer(tags_t[b"PWV4"][24:], np.uint8).reshape(-1, 6)[:, 3]))
    want = at * 1200 / n_frames
    check(f"a burst {int(when * 100)}% in lands in its overview column (PWV6 {col6}, PWV4 {col4}, want ~{want:.0f})",
          abs(col6 - want) <= 2 and abs(col4 - want) <= 2)

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
