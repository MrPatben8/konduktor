"""Writing a OneLibrary USB drive from nothing.

The export target that matters most for a DJ: a stick CDJ-class hardware reads.

## What a drive is

The destination folder IS the drive root. Three things go in it:

    <root>/PIONEER/rekordbox/exportLibrary.db      the library (SQLCipher)
    <root>/PIONEER/USBANLZ/P0xx/xxxxxxxx/ANLZ0000.{DAT,EXT}   per-track analysis
    <root>/…                                       the audio, already copied

Audio is already on disk when this runs, laid out by the runner mirroring the
source tree. Every path the database stores is **drive-relative with a leading
slash** — `/House/track.mp3` — which is not a host path, and the reader's
`DriveLayout.resolve()` is what joins it back to a mount point.

## What could not be reused, and why

`master.db`'s schema comes free from `pyrekordbox`'s ORM. This one has no ORM, so
the DDL is the checked-in `fixtures/onelibrary/schema.sql` — 22 tables extracted
from a real rekordbox 7 export, which is also what the read tests run against.

Cues are **not** in the `cue` table: rekordbox exports it empty and puts them in
the analysis files. So `anlz_writer` builds those, with ANLZ's own **dense
1-based** `hot_cue` numbering — not `master.db`'s sparse bank that skips 4.

## Deliberately not written

`PWAV`/`PWV2`-`PWV7` waveform tags, and the `cue` table's eight MPEG seek
columns. Both are documented unknowns (see the OneLibrary handoff §7): a CDJ
draws its waveform from those tags and seeks VBR MP3s with those columns, and
whether their absence is unplayable, ugly or fine has never been measured. A
guess here fails in a club rather than at a desk, so they are left out and said
so rather than invented.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

from ...core.export import ExportPayload, ExportTrack, WrittenLibrary
from ..rekordbox import anlz_writer as W
from ..rekordbox.beatgrid import beats_from_markers
from ..rekordbox.projection import render_key
from .capabilities import capabilities_for
from .layout import DB_SUBPATH

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).resolve().parents[3] / "fixtures" / "onelibrary" / "schema.sql"

#: rekordbox writes this in `property.dbVersion` on a real export.
DB_VERSION = "1000"
#: Seen on every track of a real export; semantics undocumented, copied as-is.
ANALYSED_BITS = 41

OTHER_PLAYLIST = "Other"


def _drive_relative(path: Path, root: Path) -> str:
    """`/House/track.mp3` — drive-relative, WITH the leading slash.

    The slash is not a host root. Joining one of these naively discards the
    mount point and silently yields a path that looks like an empty drive.
    """
    return "/" + path.relative_to(root).as_posix()


def _anlz_dir(drive_relative: str) -> str:
    """`/PIONEER/USBANLZ/P0xx/xxxxxxxx` for a track.

    rekordbox derives these two levels from a hash whose algorithm is not
    published. It does not need to match: the database stores the resulting path
    in `content.analysisDataFilePath` and that column is what a player follows.
    So this derives its own stable value in the same SHAPE, which keeps a drive
    recognisable to a human without pretending to reproduce rekordbox's scheme.
    """
    digest = hashlib.sha1(drive_relative.encode("utf-8")).hexdigest().upper()
    return f"/PIONEER/USBANLZ/P0{digest[:2]}/{digest[2:10]}"


class OneLibraryExporter:
    platform = "onelibrary"
    #: Relative to the drive root, which is what the destination folder is.
    library_filename = str(Path("PIONEER") / DB_SUBPATH)

    def capabilities(self):
        """What a drive can hold — no device, no path, no library to read.

        `writable` is overridden to True. The adapter's own set says False
        because Konduktor cannot EDIT a plugged-in drive in place; an export
        target is a different question, and it is one Konduktor can now answer
        yes to. Leaving the adapter's answer here would have the UI report that
        the thing it just wrote cannot be written.
        """
        return capabilities_for().model_copy(
            update={"writable": True, "readonly_cause": None}
        )

    def write(self, payload: ExportPayload, destination: Path) -> WrittenLibrary:
        import sqlcipher3.dbapi2 as sqlcipher
        from pyrekordbox.devicelib_plus.database import BLOB
        from pyrekordbox.utils import deobfuscate

        root = Path(destination)
        library = root / "PIONEER" / DB_SUBPATH
        library.parent.mkdir(parents=True, exist_ok=True)
        if library.exists():
            library.unlink()  # SQLCipher will not re-key an existing file

        extra: list[Path] = []
        con = sqlcipher.connect(str(library))
        try:
            # The same universal key every vendor's drive uses — a OneLibrary
            # stick is readable by any of them, which is the point of it.
            con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
            con.executescript(SCHEMA.read_text())
            extra += self._fill(con, payload, root)
            con.commit()
        finally:
            con.close()
        return WrittenLibrary(library=library, extra=extra)

    # ---- the database --------------------------------------------------------

    def _fill(self, con, payload: ExportPayload, root: Path) -> list[Path]:
        lookups = {name: {} for name in ("artist", "album", "genre", "label", "key")}
        written: list[Path] = []
        by_source: dict[str, int] = {}

        for n, item in enumerate(payload.tracks, start=1):
            by_source[item.source_id] = n
            written += self._write_track(con, n, item, root, lookups)

        self._write_playlists(con, payload, by_source)
        con.execute(
            "INSERT INTO property (deviceName, dbVersion, numberOfContents, "
            "createdDate, backGroundColorType, myTagMasterDBID) VALUES (?,?,?,?,?,?)",
            (payload.name, DB_VERSION, len(payload.tracks),
             date.today().isoformat(), 0, 0),
        )
        return written

    @staticmethod
    def _lookup(con, table: str, cache: dict, name: str | None) -> int | None:
        """Find-or-create a row in one of the name tables."""
        if not name:
            return None
        if name in cache:
            return cache[name]
        column = f"{table}_id"
        row = con.execute(f"SELECT {column} FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            cache[name] = row[0]
            return row[0]
        cur = con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
        cache[name] = cur.lastrowid
        return cur.lastrowid

    def _write_track(self, con, content_id: int, item: ExportTrack,
                     root: Path, lookups: dict) -> list[Path]:
        track = item.track
        rel = _drive_relative(item.destination, root)
        anlz_rel = f"{_anlz_dir(rel)}/ANLZ0000.DAT"

        try:
            size = item.destination.stat().st_size
        except OSError:
            size = 0

        con.execute(
            "INSERT INTO content (content_id, title, titleForSearch, bpmx100, length, "
            "trackNo, artist_id_artist, album_id, genre_id, label_id, key_id, "
            "djComment, rating, releaseDate, dateAdded, path, fileName, fileSize, "
            "fileType, bitrate, samplingRate, isHotCueAutoLoadOn, "
            "isKuvoDeliverStatusOn, masterDbId, masterContentId, "
            "analysisDataFilePath, analysedBits, hasModified, cueUpdateCount, "
            "analysisDataUpdateCount, informationUpdateCount) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                content_id, track.title, (track.title or "").lower(),
                int(round((track.bpm or 0) * 100)) or None, track.length,
                None,
                self._lookup(con, "artist", lookups["artist"], track.artist),
                self._lookup(con, "album", lookups["album"], track.album),
                self._lookup(con, "genre", lookups["genre"], track.genre),
                self._lookup(con, "label", lookups["label"], track.label),
                # NOT `track.key` — that is the SOURCE platform's notation, and
                # writing Traktor's "10m" into a Pioneer library shows "10m"
                # where the deck expects "Cm". Rendered from the parsed wheel.
                self._lookup(con, "key", lookups["key"],
                             render_key(track.key_wheel, track.key_mode)),
                track.comment, track.rating or 0, track.release_date,
                date.today().isoformat(), rel, item.destination.name, size,
                1, track.bitrate, None, 1, 1, 0, content_id,
                anlz_rel, ANALYSED_BITS, 0, 0, 0, 0,
            ),
        )
        return self._write_anlz(item, rel, root / anlz_rel.lstrip("/"))

    # ---- the analysis files --------------------------------------------------

    def _write_anlz(self, item: ExportTrack, rel: str, dat: Path) -> list[Path]:
        """The `.DAT` and, when there are cues beyond pad C, the `.EXT`.

        Always both when there are any cues: `PCO2` lives only in the `.EXT` and
        is the complete list, which is what the reader prefers.
        """
        cues = self._cue_dicts(item)
        beats = self._beats(item)

        tags = [W.path_tag(rel)]
        if beats:
            tags.append(W.beatgrid_tag(beats))
        tags += W.cue_tags(cues, extended=False)
        written = [W.write_anlz(dat, tags)]

        if cues:
            written.append(
                W.write_anlz(dat.with_suffix(".EXT"),
                             [W.path_tag(rel), *W.cue_tags(cues, extended=True)])
            )
        return written

    @staticmethod
    def _beats(item: ExportTrack) -> list[tuple[int, float, float]]:
        """Every beat, expanded from the generic marker list.

        A Pioneer grid has no tempo markers — it is a flat list of beats — so a
        flexible multi-tempo grid crosses by expansion here and is collapsed
        back by `markers_from_beats` on read. Both directions share one
        definition of where a tempo change starts.
        """
        cues = item.cues
        if not cues or not cues.grid_markers:
            return []
        duration = float(item.track.length or 0) or (cues.grid_markers[-1].start + 60.0)
        nums, bpms, times = beats_from_markers(cues.grid_markers, duration)
        return list(zip(nums, bpms, times))

    @staticmethod
    def _cue_dicts(item: ExportTrack) -> list[dict]:
        """Generic cues in the shape `anlz_writer` packs.

        Slot numbering is ANLZ's own: **dense 1-based**, 0 for a memory cue.
        The generic model's `slot` is 0-based, so every hot cue is +1. Reuse
        `master.db`'s sparse bank here and every cue from pad D lands one pad
        too far along.
        """
        out: list[dict] = []
        for cue in (item.cues.cues if item.cues else []):
            is_loop = cue.type == "loop" or cue.length > 0
            out.append({
                "hot_cue": 0 if cue.role == "memory" or cue.slot is None else cue.slot + 1,
                "kind": 2 if is_loop else 1,
                "time_ms": int(round(cue.start * 1000)),
                "loop_ms": int(round((cue.start + cue.length) * 1000)) if is_loop else None,
                "rgb": _rgb(cue.color),
                "beats": None,
            })
        return out

    # ---- playlists -----------------------------------------------------------

    def _write_playlists(self, con, payload: ExportPayload, by_source: dict) -> None:
        """One folder named after the export, with the tree preserved under it.

        `playlist` is a flat table with `playlist_id_parent`, so folders and
        playlists are the same row kind told apart by `attribute` (1 = folder).
        """
        next_id = [0]

        def add(name: str, parent: int | None, *, folder: bool) -> int:
            next_id[0] += 1
            con.execute(
                "INSERT INTO playlist (playlist_id, sequenceNo, name, attribute, "
                "playlist_id_parent) VALUES (?,?,?,?,?)",
                (next_id[0], next_id[0], name, 1 if folder else 0, parent),
            )
            return next_id[0]

        root = add(payload.name, None, folder=True)
        folders: dict[tuple[str, ...], int] = {(): root}

        for playlist in payload.playlists:
            parent = root
            for depth in range(1, len(playlist.folders) + 1):
                branch = tuple(playlist.folders[:depth])
                if branch not in folders:
                    folders[branch] = add(branch[-1], folders[branch[:-1]], folder=True)
                parent = folders[branch]
            self._fill_playlist(con, add(playlist.name, parent, folder=False),
                                playlist.track_ids, by_source)

        claimed = {t for p in payload.playlists for t in p.track_ids}
        loose = [t.source_id for t in payload.tracks if t.source_id not in claimed]
        if loose:
            self._fill_playlist(con, add(OTHER_PLAYLIST, root, folder=False),
                                loose, by_source)

    @staticmethod
    def _fill_playlist(con, playlist_id: int, track_ids: list[str], by_source: dict) -> None:
        for seq, source_id in enumerate(track_ids, start=1):
            content_id = by_source.get(source_id)
            if content_id is not None:
                con.execute(
                    "INSERT INTO playlist_content (playlist_id, content_id, sequenceNo) "
                    "VALUES (?,?,?)", (playlist_id, content_id, seq)
                )


def _rgb(color: str | None) -> tuple[int, int, int] | None:
    if not color or not color.startswith("#") or len(color) != 7:
        return None
    try:
        return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    except ValueError:
        return None
