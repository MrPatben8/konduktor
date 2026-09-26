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

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
raise SystemExit(1 if failed else 0)
