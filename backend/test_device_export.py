"""The "Rekordbox Export" target: a stick's legacy Device Library (`export.pdb`).

What Rekordbox lists as a stick's Device Library and pre-OneLibrary players
read. There is no public spec, so this pins the writer to bytes REKORDBOX wrote:
rows and a whole page taken from a real rekordbox 7 device export
(`fixtures/rekordbox/device/reference_rows.json`) must be reproduced exactly.
Then the structure a player walks (page chains, allocation, row indexes), and
an export end to end — including that alongside the OneLibrary target the stick
carries ONE set of analysis files and artwork, as a rekordbox-made stick does.
"""
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("KONDUKTOR_DATA_DIR", tempfile.mkdtemp(prefix="konduktor-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from PIL import Image  # noqa: E402

import konduktor.adapters  # noqa: E402,F401 — registers every target
from konduktor.adapters.rekordbox import pdb as P  # noqa: E402
from konduktor.core import export as core_export  # noqa: E402
from konduktor.core.export import ExportPayload, ExportPlaylist, ExportTrack  # noqa: E402
from konduktor.core.model import CuePoint, GridMarker, Track, TrackCues  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


REF = json.loads((P.TEMPLATE_DIR / "reference_rows.json").read_text())

print("== rows are rekordbox's bytes ==")
fields = REF["track_row_demo_track_2_fields"]
ours = P.track_row({k: v for k, v in fields.items() if k not in P.TRACK_STRINGS},
                   {k: fields[k] for k in P.TRACK_STRINGS})
check("a track row is byte-identical to rekordbox's", ours.hex() == REF["track_row_demo_track_2"])
# rekordbox leaves variable slack after a row on its page (6-9 bytes, sometimes
# stale data); readers find rows through the page's index, never by size. So a
# row is compared over its own bytes.
artist = P.artist_row(1, "Loopmasters").rstrip(b"\0")
check("an artist row is byte-identical", REF["artist_row_loopmasters"].startswith(artist.hex()))
plist = P.playlist_tree_row(1, 0, 1, "demos", folder=False).rstrip(b"\0")
check("a playlist row is byte-identical", REF["playlist_row_demos"].startswith(plist.hex()))

print("== a page is rekordbox's bytes ==")
page = bytes.fromhex(REF["keys_page"])
n = (page[0x19] >> 5) | (page[0x1A] << 3)
used = struct.unpack_from("<H", page, 0x1E)[0]
offs = [struct.unpack_from("<H", page, P.PAGE - 6 - 2 * r)[0] for r in range(n)] + [used]
rows = [page[0x28 + offs[r]:0x28 + offs[r + 1]] for r in range(n)]
rebuilt = P._data_page(REF["keys_page_index"], P.KEYS, struct.unpack_from("<I", page, 12)[0], rows,
                       struct.unpack_from("<I", page, 16)[0], shift_at=None)
check("a data page rebuilt from its rows is byte-identical (header, heap, row index)", rebuilt == page)

print("== strings ==")
for text in ("", "Am", "x" * 126, "x" * 127, "Café del Mar", "日本語"):
    enc = P.dsql(text)
    check(f"{text[:12]!r} ({len(text)} chars) round-trips", P.read_dsql(enc, 0) == text)
check("up to 126 ASCII chars is a short string", P.dsql("x" * 126)[0] & 1 == 1)
check("127+ ASCII chars is a long ASCII string (0x40)", P.dsql("x" * 127)[0] == 0x40)
check("non-ASCII is UTF-16LE (0x90)", P.dsql("Café")[0] == 0x90)

print("== the writer follows rekordbox's allocation ==")
tmpl = P.template()
w = P.PdbWriter(tmpl)
_slot, candidate, first, _last = w.tables[P.TRACKS]
start_unused = w.next_unused
many = [P.track_row({"subtype": 0x24, "id": i}, {"title": f"Track {i} " + "x" * 200,
                                                  "file_path": f"/Contents/{i}.mp3"}) for i in range(1, 41)]
w.fill(P.TRACKS, many)
w.fill(P.ARTISTS, [P.artist_row(1, "A")])
out = w.render()
lib = P.read(out)
check("all 40 tracks read back, in order", [t["id"] for t in lib.tracks] == list(range(1, 41)))
tpages = [p for p in lib.pages[P.TRACKS] if not p["flags"] & 0x40]
check("they span several pages", len(tpages) > 1, len(tpages))
check("the first data page is the table's reserved candidate", tpages[0]["index"] == candidate)
check("further pages come from the next-unused counter",
      all(p["index"] >= start_unused for p in tpages[1:]))
check("the chain ends in a fresh reserved candidate beyond every data page",
      tpages[-1]["next"] >= max(p["index"] for p in tpages))
check("the header page still starts the chain", lib.pages[P.TRACKS][0]["index"] == first)
check("every page's free space follows rekordbox's arithmetic",
      all(p["free"] == P.PAGE - 0x28 - p["used"] - (2 * p["rows"] + 4 * ((p["rows"] + 15) // 16)) for p in tpages))
check("tracks pages carry flags 0x34, others 0x24",
      all(p["flags"] == 0x34 for p in tpages)
      and all(p["flags"] == 0x24 for p in lib.pages[P.ARTISTS] if not p["flags"] & 0x40))
fixed_pages = (13, 14, 33, 34, 35, 36, 37, 38)
check("the fixed tables (colours, columns, menus) are untouched",
      all(out[p * P.PAGE:(p + 1) * P.PAGE] == tmpl[p * P.PAGE:(p + 1) * P.PAGE] for p in fixed_pages))
print("== header pages index their data (else rekordbox: 'Device library is corrupted') ==")
# A header page holds an index: its own number, the FIRST DATA PAGE, ... a
# count and entries `page << 3 | flag`. Left pointing nowhere, rekordbox 7 called
# a Konduktor-written library corrupted. Rules read off a real export.
w.stamp_track_count(40)
out = w.render()
def index(buf, table):
    _t, _e, first, _l = next(struct.unpack_from("<4I", buf, 28 + 16 * i) for i in range(20)
                             if struct.unpack_from("<I", buf, 28 + 16 * i)[0] == table)
    o = first * P.PAGE
    n = struct.unpack_from("<H", buf, o + 0x38)[0]
    return (struct.unpack_from("<I", buf, o + 0x2C)[0],
            [(e >> 3, e & 7) for e in struct.unpack_from(f"<{n}I", buf, o + 0x3C)],
            struct.unpack_from("<2H", buf, o + 0x20), struct.unpack_from("<H", buf, o + 0x26)[0])
first_data, entries, (u5, nrl), u7 = index(out, P.TRACKS)
check("the tracks header points at its first data page", first_data == tpages[0]["index"])
check("and lists every data page: full ones flag 0, the last (with room) flag 3",
      entries == [(p["index"], 3 if k == len(tpages) - 1 else 0) for k, p in enumerate(tpages)], entries)
check("its u7 counts them; u5 / nrl = pages with room / the first of them",
      u7 == len(tpages) and (u5, nrl) == (1, len(tpages) - 1), (u5, nrl, u7))
check("a non-indexed table's header still points at its data",
      index(out, P.ARTISTS)[0] == next(p["index"] for p in P.read(out).pages[P.ARTISTS] if not p["flags"] & 0x40)
      and index(out, P.ARTISTS)[1] == [])
h_first, h_entries, _, h_u7 = index(out, P.HISTORY)
check("with tracks, the history page is indexed too (flag 0, count 1)",
      h_entries == [(h_first, 0)] and h_u7 == 1, (h_entries, h_u7))
hist_row = next(r for h, r in P._rows(out, *struct.unpack_from("<2I", out, 28 + 16 * 19 + 8)) if r is not None)
check("the history row counts the tracks and flags that there are some",
      out[hist_row + 3] == 1 and struct.unpack_from("<I", out, hist_row + 4)[0] == 40)
full = [p for p in tpages[:-1]]
check("full tracks pages carry 0x1FFF/0x1FFF, the last rows+1 / 0",
      all(struct.unpack_from("<2H", out, p["index"] * P.PAGE + 0x20) == (0x1FFF, 0x1FFF) for p in full)
      and struct.unpack_from("<2H", out, tpages[-1]["index"] * P.PAGE + 0x20) == (tpages[-1]["rows"] + 1, 0))
check("the file header's second counter is 1 + pages with room", struct.unpack_from("<I", out, 16)[0] == 2)

try:
    w.fill(P.TRACKS, many[:1])
    check("filling a table twice is refused", False, "it accepted")
except ValueError:
    check("filling a table twice is refused", True)

print("== an export end to end ==")
root = Path(tempfile.mkdtemp())
sr = 22050
tone = np.random.default_rng(1).uniform(-0.4, 0.4, sr * 6).astype(np.float32)
tracks = []
for i, (title, artist, key) in enumerate((("One", "Artist A", (8, "minor")), ("Two", "Artist B", (None, None)),
                                          ("Três", "Artist A", (8, "minor")))):
    audio = root / "Contents" / ("House" if i < 2 else "Techno") / f"{title}.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    sf.write(audio, tone, sr)
    png = __import__("io").BytesIO()
    Image.new("RGB", (300, 300), (40 * i, 90, 160)).save(png, format="PNG")
    t = Track(id=f"src{i}", title=title, artist=artist, album="LP", genre="House", bpm=125.0,
              key_wheel=key[0], key_mode=key[1], length=6, rating=i + 1, filepath=str(audio))
    tracks.append(ExportTrack(track=t, destination=audio, art=(png.getvalue(), "image/png"),
                              cues=TrackCues(track_id=t.id, grid_markers=[GridMarker(start=0.0, bpm=125.0)],
                                             cues=[CuePoint(type="cue", role="hotcue", start=1.0, length=0.0, slot=0)])))
payload = ExportPayload(name="GIG", tracks=tracks, playlists=[
    ExportPlaylist(name="Peak", track_ids=["src0", "src1"], folders=["House"])])
dev = core_export.for_platform("rekordbox_export")
check("the target is registered, named 'Rekordbox Export'", dev is not None and dev.display_name == "Rekordbox Export")
check("and the master.db target is 'Rekordbox Library'",
      core_export.for_platform("rekordbox").display_name == "Rekordbox Library")
written = dev.write(payload, root)
lib = P.read(written.library.read_bytes())
check("export.pdb lands in PIONEER/rekordbox/", written.library == root / "PIONEER" / "rekordbox" / "export.pdb")
check("with exportExt.pdb beside it", (root / "PIONEER" / "rekordbox" / "exportExt.pdb") in written.extra)
check("three tracks, titles intact (UTF-16 included)", [t["title"] for t in lib.tracks] == ["One", "Two", "Três"])
check("artists are shared, not duplicated", lib.artists == {1: "Artist A", 2: "Artist B"})
check("the key is rendered in rekordbox's notation", lib.keys == {1: "Am"}, lib.keys)
check("a track with no key has key 0", lib.tracks[1]["key_id"] == 0)
check("bpm x100, rating, file type, bitmask as rekordbox writes them",
      all(t["tempo"] == 12500 and t["u6"] == 11 and t["bitmask"] == 0xC0700 and t["u5"] == 0x29 for t in lib.tracks)
      and [t["rating"] for t in lib.tracks] == [1, 2, 3])
check("the file path is drive-relative", lib.tracks[0]["file_path"] == "/Contents/House/One.wav", lib.tracks[0]["file_path"])
check("no computer's masterDbId is claimed", all(t["u3"] == 0 and t["u4"] == 0 for t in lib.tracks))
tree = {p["name"]: p for p in lib.playlists}
check("the tree: a folder named after the export > House > Peak, plus Other for loose tracks",
      tree["GIG"]["folder"] and tree["House"]["parent"] == tree["GIG"]["id"]
      and tree["Peak"]["parent"] == tree["House"]["id"] and tree["Other"]["parent"] == tree["GIG"]["id"])
peak = [e for e in lib.entries if e[2] == tree["Peak"]["id"]]
check("playlist entries in order, 1-based", peak == [(1, 1, tree["Peak"]["id"]), (2, 2, tree["Peak"]["id"])], peak)
check("every track has an analysis path, and the files exist",
      all((root / t["analyze_path"].lstrip("/")).is_file() for t in lib.tracks))
check("artwork rows name the a<n>.jpg files, which exist",
      all((root / p.lstrip("/")).is_file() for p in lib.artwork.values()) and len(lib.artwork) == 3)
check("every file written is reported, so a re-export can clear it",
      all(p.exists() for p in written.all_paths))

print("== alongside OneLibrary: one set of analysis files and artwork ==")
both = Path(tempfile.mkdtemp())
for t in tracks:
    dst = both / t.destination.relative_to(root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(t.destination.read_bytes())
import dataclasses  # noqa: E402
moved = [dataclasses.replace(t, destination=both / t.destination.relative_to(root)) for t in tracks]
both_payload = ExportPayload(name="GIG", tracks=moved, playlists=payload.playlists)
ol_written = core_export.for_platform("onelibrary").write(both_payload, both)
stamp = {p: p.stat().st_mtime_ns for p in ol_written.extra if p.suffix in (".DAT", ".EXT", ".2EX")}
calls = []
both_payload.checkpoint = lambda m, **_progress: calls.append(m)
dev_written = dev.write(both_payload, both)
dat_files = sorted(both.rglob("*.DAT"))
check("still one .DAT per track", len(dat_files) == 3, len(dat_files))
check("the device library reuses the analysis OneLibrary wrote (not rewritten)",
      all(p.stat().st_mtime_ns == m for p, m in stamp.items()))
lib2 = P.read(dev_written.library.read_bytes())
from konduktor.adapters.onelibrary.driver import OneLibraryDriver  # noqa: E402
ol = OneLibraryDriver().open(both)
check("both libraries point at the same analysis files",
      sorted(str(r.analysisDataFilePath) for r in ol._store.iter_content()) == sorted(t["analyze_path"] for t in lib2.tracks))
check("the a<n>.jpg files both write are the same files",
      {p.name for p in (both / "PIONEER/Artwork/00001").iterdir()} >= {Path(v).name for v in lib2.artwork.values()})
check("progress is reported per track", len(calls) == 3, calls)

print("== the export dialog lists the targets in a fixed order ==")
from fastapi.testclient import TestClient  # noqa: E402
from konduktor.main import app  # noqa: E402

names = [o["name"] for o in TestClient(app).get("/api/export-targets").json()]
check("OneLibrary, Traktor, Rekordbox Export, Rekordbox Library",
      names[:4] == ["OneLibrary", "Traktor", "Rekordbox Export", "Rekordbox Library"], names)
flags = {o["name"]: o for o in TestClient(app).get("/api/export-targets").json()}
check("Rekordbox Library says it is computer-only (the dialog warns beside a stick format)",
      flags["Rekordbox Library"]["computer_only"] and not flags["Rekordbox Export"]["computer_only"]
      and flags["Rekordbox Export"]["drive_root"])

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
