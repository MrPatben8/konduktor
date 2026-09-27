"""The "Rekordbox Export" target: a USB stick's legacy Device Library (`export.pdb`).

What Rekordbox lists as a stick's **Device Library**, and what pre-OneLibrary
players (CDJ-2000NXS2, many XDJs) read. Not to be confused with the "Rekordbox
Library" target (`export.py`), which writes a COMPUTER library (`master.db`)
that Rekordbox never reads from a stick.

## Shared with the OneLibrary target — deliberately

Rekordbox's own device export writes `export.pdb` AND `exportLibrary.db`, both
pointing at ONE set of audio, analysis files and artwork. This target does the
same: the analysis files are written by the OneLibrary exporter's own
`_write_anlz`, at the same paths (`PIONEER/USBANLZ/P0xx/xxxxxxxx/`), and the
artwork is the `a<n>.jpg` pair the OneLibrary target also writes (rekordbox's
`a` files are the ones `export.pdb` names; OneLibrary's `image` table names the
byte-identical `b` files). When both targets are ticked, whichever runs second
finds the analysis already on the stick — written moments ago by the first, as
the runner clears the previous export before copying — and reuses it rather
than decoding every track again.

## The database

`pdb.PdbWriter` fills rekordbox's own empty device library (a template) with
rows; see `pdb.py` for the format and why a template. Values follow a real
rekordbox 7 device export:

  * `bitmask` 0xC0700 — the same value as OneLibrary's `content.contentLink`,
    which is what makes rekordbox draw a track's song-list preview there;
  * `u2` = the track id + 0x600 (rekordbox: masterContentId + 0x600);
  * `u3`/`u4` = 0: they are the halves of rekordbox's `masterDbId`, the link to a
    computer's collection, which a Konduktor export does not claim;
  * `u5` 0x29 (= `analysedBits` 41), `u6` the file type, `u7` 3;
  * strings "ON" for kuvo_public / autoload_hotcues; the three unnamed strings
    rekordbox fills from its phrase analysis are left EMPTY — this exporter
    writes no phrases, and whether players care is what the device test checks.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from ...core.export import ExportPayload, ExportTrack, WrittenLibrary
from . import artwork, pdb
from .projection import render_key

log = logging.getLogger(__name__)

PDB_DIR = Path("PIONEER") / "rekordbox"
OTHER_PLAYLIST = "Other"
BITMASK = 0xC0700
ANALYSED = 0x29


class RekordboxDeviceExporter:
    platform = "rekordbox_export"
    menu_order = 30
    display_name = "Rekordbox Export"
    library_filename = str(PDB_DIR / "export.pdb")
    #: A player looks for `PIONEER/` at the root of the stick.
    drive_root = True

    def capabilities(self):
        """What a stick's device library can hold — the OneLibrary target's
        answer, since both describe the same drive and its analysis files."""
        from ..onelibrary.export import OneLibraryExporter

        return OneLibraryExporter().capabilities()

    def write(self, payload: ExportPayload, destination: Path) -> WrittenLibrary:
        # Imported here, not at module level: the OneLibrary package imports this
        # package's modules, and a module-level import would be circular.
        from ..onelibrary.export import (
            OneLibraryExporter, _anlz_dir, _audio_format, _drive_relative, _file_type, _kbps,
        )

        root = Path(destination)
        (root / PDB_DIR).mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        drive = OneLibraryExporter()
        extra: list[Path] = []

        ids = {name: {} for name in ("artist", "album", "genre", "label", "key")}
        rows = {t: [] for t in (pdb.TRACKS, pdb.ARTISTS, pdb.ALBUMS, pdb.GENRES,
                                pdb.LABELS, pdb.KEYS, pdb.ARTWORK)}
        builders = {"artist": (pdb.ARTISTS, pdb.artist_row), "album": (pdb.ALBUMS, pdb.album_row),
                    "genre": (pdb.GENRES, pdb.id_string_row), "label": (pdb.LABELS, pdb.id_string_row),
                    "key": (pdb.KEYS, pdb.key_row)}

        def lookup(kind: str, name: str | None) -> int:
            if not name:
                return 0
            table = ids[kind]
            if name not in table:
                table[name] = len(table) + 1
                t, build = builders[kind]
                rows[t].append(build(table[name], name))
            return table[name]

        by_source: dict[str, int] = {}
        art_n = 0
        total = len(payload.tracks)
        for n, item in enumerate(payload.tracks, start=1):
            payload.checkpoint(f"Writing the device library: {item.track.title or item.destination.name} ({n}/{total})")
            track = item.track
            by_source[item.source_id] = n
            rel = _drive_relative(item.destination, root)
            anlz_rel = f"{_anlz_dir(rel)}/ANLZ0000.DAT"
            dat = root / anlz_rel.lstrip("/")
            extra += self._analysis(drive, item, rel, dat)

            artwork_id = 0
            art = self._art(item, root, art_n + 1)
            if art:
                art_n += 1
                artwork_id = art_n
                extra += art
                rows[pdb.ARTWORK].append(pdb.id_string_row(art_n, f"/PIONEER/Artwork/00001/a{art_n}.jpg"))

            sample_rate, bit_depth = _audio_format(item.destination)
            try:
                size = item.destination.stat().st_size
            except OSError:
                size = 0
            year = int(track.release_date[:4]) if (track.release_date or "")[:4].isdigit() else 0
            rows[pdb.TRACKS].append(pdb.track_row(
                {
                    "subtype": 0x24, "bitmask": BITMASK, "sample_rate": sample_rate or 0,
                    "file_size": size, "u2": n + 0x600,
                    "artwork_id": artwork_id,
                    "key_id": lookup("key", render_key(track.key_wheel, track.key_mode)),
                    "label_id": lookup("label", track.label),
                    "bitrate": _kbps(track.bitrate) or 0,
                    "tempo": int(round((track.bpm or 0) * 100)),
                    "genre_id": lookup("genre", track.genre),
                    "album_id": lookup("album", track.album),
                    "artist_id": lookup("artist", track.artist),
                    "id": n, "year": year, "sample_depth": bit_depth or 0,
                    "duration": min(int(track.length or 0), 0xFFFF),
                    "u5": ANALYSED, "rating": max(0, min(5, int(track.rating or 0))),
                    "u6": _file_type(item.destination), "u7": 3,
                },
                {
                    "kuvo_public": "ON", "autoload_hotcues": "ON",
                    "date_added": today, "release_date": track.release_date or "",
                    "mix_name": track.mix or "", "analyze_path": anlz_rel,
                    "analyze_date": today, "comment": track.comment or "",
                    "title": track.title or item.destination.stem,
                    "filename": item.destination.name, "file_path": rel,
                },
            ))

        tree, entries = self._playlists(payload, by_source)
        writer = pdb.PdbWriter(pdb.template("export.pdb"))
        for table in (pdb.TRACKS, pdb.GENRES, pdb.ARTISTS, pdb.ALBUMS, pdb.LABELS, pdb.KEYS, pdb.ARTWORK):
            writer.fill(table, rows[table])
        writer.fill(pdb.PLAYLIST_TREE, tree)
        writer.fill(pdb.PLAYLIST_ENTRIES, entries)
        writer.stamp_history_date(date.today())
        writer.stamp_track_count(len(rows[pdb.TRACKS]))

        library = root / self.library_filename
        library.write_bytes(writer.render())
        ext = root / PDB_DIR / "exportExt.pdb"
        ext.write_bytes(pdb.template("exportExt.pdb"))
        return WrittenLibrary(library=library, extra=[ext, *extra])

    # ---- shared drive files ---------------------------------------------------

    @staticmethod
    def _analysis(drive, item: ExportTrack, rel: str, dat: Path) -> list[Path]:
        """This track's analysis files — reused when the OneLibrary target has
        just written them in this same export, else written the same way."""
        existing = [dat.with_suffix(s) for s in (".DAT", ".EXT", ".2EX") if dat.with_suffix(s).exists()]
        if dat.exists() and dat.with_suffix(".EXT").exists():
            return existing
        return drive._write_anlz(item, rel, dat)

    @staticmethod
    def _art(item: ExportTrack, root: Path, n: int) -> list[Path]:
        """`a<n>.jpg` / `a<n>_m.jpg` — the pair export.pdb's artwork table names,
        numbered in track order exactly as the OneLibrary target numbers them."""
        if not item.art:
            return []
        folder = root / "PIONEER" / "Artwork" / "00001"
        small, medium = folder / f"a{n}.jpg", folder / f"a{n}_m.jpg"
        if small.exists() and medium.exists():
            return [small, medium]
        jpegs = artwork.pioneer_jpegs(item.art[0])
        if jpegs is None:
            return []
        folder.mkdir(parents=True, exist_ok=True)
        small.write_bytes(jpegs[0])
        medium.write_bytes(jpegs[1])
        return [small, medium]

    # ---- playlists ------------------------------------------------------------

    @staticmethod
    def _playlists(payload: ExportPayload, by_source: dict) -> tuple[list[bytes], list[bytes]]:
        """The tree under one folder named after the export, as the other
        targets build it. `sort_order` is a node's position among its siblings."""
        tree: list[bytes] = []
        entries: list[bytes] = []
        next_id = [0]
        siblings: dict[int, int] = {}

        def add(name: str, parent: int, *, folder: bool) -> int:
            next_id[0] += 1
            order = siblings.get(parent, 0)
            siblings[parent] = order + 1
            tree.append(pdb.playlist_tree_row(next_id[0], parent, order, name, folder=folder))
            return next_id[0]

        def fill(playlist_id: int, source_ids: list[str]) -> None:
            index = 0
            for source_id in source_ids:
                track_id = by_source.get(source_id)
                if track_id is not None:
                    index += 1
                    entries.append(pdb.playlist_entry_row(index, track_id, playlist_id))

        root = add(payload.name, 0, folder=True)
        folders: dict[tuple[str, ...], int] = {(): root}
        for playlist in payload.playlists:
            parent = root
            for depth in range(1, len(playlist.folders) + 1):
                branch = tuple(playlist.folders[:depth])
                if branch not in folders:
                    folders[branch] = add(branch[-1], folders[branch[:-1]], folder=True)
                parent = folders[branch]
            fill(add(playlist.name, parent, folder=False), playlist.track_ids)
        claimed = {t for p in payload.playlists for t in p.track_ids}
        loose = [t.source_id for t in payload.tracks if t.source_id not in claimed]
        if loose:
            fill(add(OTHER_PLAYLIST, root, folder=False), loose)
        return tree, entries
