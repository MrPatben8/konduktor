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

from ...core import waveform
from ...core.export import ExportPayload, ExportTrack, WrittenLibrary
from ...core.cue_colors import effective_color
from ..rekordbox import anlz_writer as W
from ..rekordbox import artwork, palette, timebase
from ..rekordbox.beatgrid import beats_from_markers
from ..rekordbox.projection import render_key
from .capabilities import capabilities_for
from .layout import DB_SUBPATH

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).resolve().parents[3] / "fixtures" / "onelibrary" / "schema.sql"
#: rekordbox's scaffolding rows — browse menu items, categories, sort columns,
#: the colour palette — copied from a real export. See the file's header.
SEED = SCHEMA.with_name("seed.sql")

#: rekordbox writes this in `property.dbVersion` on a real export.
DB_VERSION = "1000"
#: Seen on every track of a real export; semantics undocumented, copied as-is.
ANALYSED_BITS = 41
#: `content.contentLink`, as rekordbox 7 writes it beside `analysedBits` 41 (a
#: re-analysed track carried 1902336 beside 105; what the bits mean is unknown).
#: NOT optional: measured by blanking it on one track of a rekordbox-written
#: stick — that row's song-list preview fell back to plain blue and gained a "?"
#: beside CUE, exactly as every row of Konduktor's exports did while it was NULL.
#: `masterDbId`/`masterContentId` were cleared the same way and changed nothing.
CONTENT_LINK = 788224

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
    #: A CDJ looks for `PIONEER/` at the root of the stick and nowhere else.
    drive_root = True

    def capabilities(self):
        """What a drive can hold — no device, no path, no library to read.

        `writable` is overridden to True. The adapter's own set says False
        because Konduktor cannot EDIT a plugged-in drive in place; an export
        target is a different question, and it is one Konduktor can now answer
        yes to. Leaving the adapter's answer here would have the UI report that
        the thing it just wrote cannot be written.
        """
        caps = capabilities_for()
        # An exported drive CARRIES artwork (`_write_art`); the adapter's False
        # means "cannot edit a plugged-in drive's art", a different question.
        return caps.model_copy(update={
            "writable": True, "readonly_cause": None,
            "tracks": caps.tracks.model_copy(update={"artwork": True}),
        })

    def write(self, payload: ExportPayload, destination: Path) -> WrittenLibrary:
        import sqlcipher3.dbapi2 as sqlcipher
        from pyrekordbox.devicelib_plus.database import BLOB
        from pyrekordbox.utils import deobfuscate

        root = Path(destination)
        library = root / "PIONEER" / DB_SUBPATH
        library.parent.mkdir(parents=True, exist_ok=True)
        # SQLCipher will not re-key an existing file — and the WAL and shared
        # memory files beside it must go too. rekordbox leaves both on a stick
        # it has mounted, and SQLite REPLAYS a leftover WAL into whatever
        # database it finds, so a fresh library would open with the old one's
        # pages written over it.
        for stale in (library, *(library.with_name(library.name + s) for s in ("-wal", "-shm"))):
            if stale.exists():
                stale.unlink()

        extra: list[Path] = []
        con = sqlcipher.connect(str(library))
        try:
            # The same universal key every vendor's drive uses — a OneLibrary
            # stick is readable by any of them, which is the point of it.
            con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
            con.executescript(SCHEMA.read_text())
            con.executescript(SEED.read_text(encoding="utf-8"))
            extra += self._fill(con, payload, root)
            con.commit()
        finally:
            con.close()
        return WrittenLibrary(library=library, extra=extra)

    # ---- the database --------------------------------------------------------

    def _fill(self, con, payload: ExportPayload, root: Path) -> list[Path]:
        lookups = {name: {} for name in ("artist", "album", "genre", "label", "key")}
        # Image ids are handed out in track order, one per track that HAS art —
        # rekordbox does not share an image between tracks, even identical ones.
        lookups["image"] = {"next": 0}
        written: list[Path] = []
        by_source: dict[str, int] = {}

        total = len(payload.tracks)
        for n, item in enumerate(payload.tracks, start=1):
            # Each track is DECODED for its waveform, so this loop is the slow
            # part of the write — report it, and let a cancel land between tracks.
            payload.checkpoint(f"Analysing {item.track.title or item.destination.name} ({n}/{total})")
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
        sample_rate, bit_depth = _audio_format(item.destination)
        image_id, art_files = self._write_art(con, item, root, lookups["image"])

        con.execute(
            "INSERT INTO content (content_id, title, titleForSearch, bpmx100, length, "
            "trackNo, artist_id_artist, album_id, genre_id, label_id, key_id, image_id, "
            "djComment, rating, releaseDate, dateAdded, path, fileName, fileSize, "
            "fileType, bitrate, bitDepth, samplingRate, isHotCueAutoLoadOn, "
            "isKuvoDeliverStatusOn, masterDbId, masterContentId, "
            "analysisDataFilePath, analysedBits, contentLink, hasModified, cueUpdateCount, "
            "analysisDataUpdateCount, informationUpdateCount) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                image_id,
                track.comment, track.rating or 0, track.release_date,
                date.today().isoformat(), rel, item.destination.name, size,
                # kbps, as every Pioneer library stores it; the generic model
                # carries bits per second.
                _file_type(item.destination), _kbps(track.bitrate),
                bit_depth, sample_rate, 1, 1, 0, content_id,
                anlz_rel, ANALYSED_BITS, CONTENT_LINK, 0, 0, 0, 0,
            ),
        )
        return art_files + self._write_anlz(item, rel, root / anlz_rel.lstrip("/"))

    # ---- artwork -------------------------------------------------------------

    @staticmethod
    def _write_art(con, item: ExportTrack, root: Path, counter: dict) -> tuple[int | None, list[Path]]:
        """One track's cover: four JPEGs and an `image` row, as rekordbox 7 writes.

        `PIONEER/Artwork/00001/` holds `a<n>.jpg` / `a<n>_m.jpg` and a byte-
        identical `b<n>.jpg` / `b<n>_m.jpg` (80 and 240 px); `image.path` names the
        `b` file and `content.image_id` points at the row. rekordbox's own export
        writes both letters — the `a` pair presumably for the legacy `export.pdb`
        readers — so both are written here too.

        Everything goes in folder `00001`. How rekordbox splits a large library
        across folders is not known (the reference export has 6 images); a single
        folder is what it demonstrably reads.
        """
        if not item.art:
            return None, []
        jpegs = artwork.pioneer_jpegs(item.art[0])
        if jpegs is None:
            return None, []
        counter["next"] += 1
        n = counter["next"]
        folder = root / "PIONEER" / "Artwork" / "00001"
        folder.mkdir(parents=True, exist_ok=True)
        small, medium = jpegs
        files = []
        for letter in ("a", "b"):
            for suffix, data in (("", small), ("_m", medium)):
                path = folder / f"{letter}{n}{suffix}.jpg"
                path.write_bytes(data)
                files.append(path)
        con.execute("INSERT INTO image (image_id, path) VALUES (?, ?)",
                    (n, f"/PIONEER/Artwork/00001/b{n}.jpg"))
        return n, files

    # ---- the analysis files --------------------------------------------------

    def _write_anlz(self, item: ExportTrack, rel: str, dat: Path) -> list[Path]:
        """The `.DAT`, `.EXT` and `.2EX`, as rekordbox 7 writes them.

        All three, and every cue list, even for a track with no cues: rekordbox
        never produces a track without them, and `PCO2` — the complete list,
        which readers prefer — lives only in the `.EXT`.

        **The `.DAT` must carry `PVBR` and the preview waveforms**, or rekordbox
        shows neither the grid nor the cues (see `anlz_writer`). The `.EXT` and
        `.2EX` waveforms only affect drawing: the song-list preview and the
        deck's scrolling waveform. Tag order is rekordbox's in each file:
          .DAT  PPTH PVBR PQTZ PWAV PWV2 PCOB PCOB
          .EXT  PPTH PWV3 PCOB PCOB PCO2 PCO2 PWV5 PWV4
          .2EX  PPTH PWV7 PWV6 PWVC
        An undecodable file still gets the `.DAT` (with a flat preview) and the
        `.EXT` cue lists — its grid and cues must show — but no drawn waveforms.
        """
        # rekordbox's clock runs ~25 ms behind the decoded audio on MP3/AAC (see
        # `timebase`): beats and cues are computed in the decoded time base and
        # shifted as they are packed, and the waveform frames are measured with
        # the same lead so they line up with the beats.
        off = timebase.offset(item.destination)
        measured = waveform.analyse(item.destination, lead=off)
        # The DECODED length where there is one: `Track.length` is whole seconds
        # rounded down, and a grid expanded to it loses the track's last beat.
        beats = self._beats(item, measured.duration if measured else None)
        cues = self._cue_dicts(item, beats, off)
        if measured:
            previews = W.preview_tags(measured.columns.rms, measured.columns.brightness)
            ext_waves, two_ex = W.waveform_tags(measured.frames.bands)
        else:
            previews, ext_waves, two_ex = W.flat_preview_tags(), [], []

        tags = [W.path_tag(rel), W.vbr_tag(W.mp3_samples(item.destination))]
        if beats:
            tags.append(W.beatgrid_tag([(n, bpm, timebase.to_pioneer(t, off))
                                        for n, bpm, t in beats]))
        tags += previews
        tags += W.cue_tags(cues, extended=False)
        ext = [W.path_tag(rel), *ext_waves[:1], *W.cue_tags(cues, extended=True), *ext_waves[1:]]
        written = [W.write_anlz(dat, tags), W.write_anlz(dat.with_suffix(".EXT"), ext)]
        if two_ex:
            written.append(W.write_anlz(dat.with_suffix(".2EX"), [W.path_tag(rel), *two_ex]))
        return written

    @staticmethod
    def _beats(item: ExportTrack, duration: float | None = None) -> list[tuple[int, float, float]]:
        """Every beat, expanded from the generic marker list.

        A Pioneer grid has no tempo markers — it is a flat list of beats — so a
        flexible multi-tempo grid crosses by expansion here and is collapsed
        back by `markers_from_beats` on read. Both directions share one
        definition of where a tempo change starts.
        """
        cues = item.cues
        if not cues or not cues.grid_markers:
            return []
        duration = (duration or float(item.track.length or 0)
                    or (cues.grid_markers[-1].start + 60.0))
        nums, bpms, times = beats_from_markers(cues.grid_markers, duration)
        return list(zip(nums, bpms, times))

    @staticmethod
    def _cue_dicts(item: ExportTrack, beats: list | None = None, off: float = 0.0) -> list[dict]:
        """Generic cues in the shape `anlz_writer` packs.

        Slot numbering is ANLZ's own: **dense 1-based**, 0 for a memory cue.
        The generic model's `slot` is 0-based, so every hot cue is +1. Reuse
        `master.db`'s sparse bank here and every cue from pad D lands one pad
        too far along.
        """
        out: list[dict] = []
        for cue in (item.cues.cues if item.cues else []):
            is_loop = cue.type == "loop" or cue.length > 0
            # A hot cue carries what Konduktor SHOWS — stored colour, else the
            # type's (blue cue, green loop) — as a rekordbox palette CODE, which
            # is what rekordbox draws from; "unset" draws its own defaults. A
            # memory cue keeps rekordbox's own "unset".
            code, rgb = palette.code_for(cue.color if cue.role == "memory" else effective_color(cue))
            out.append({
                "hot_cue": 0 if cue.role == "memory" or cue.slot is None else cue.slot + 1,
                "kind": 2 if is_loop else 1,
                "time_ms": int(round(timebase.to_pioneer(cue.start, off) * 1000)),
                "loop_ms": (int(round(timebase.to_pioneer(cue.start + cue.length, off) * 1000))
                            if is_loop else None),
                "rgb": rgb,
                "code": code,
                "beats": _loop_beats(cue.start, cue.length, beats or []) if is_loop else None,
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


#: rekordbox's `fileType` codes, as `pyrekordbox.devicelib_plus.FileType` has them.
#: `.stem.m4a` (a Traktor STEM) is an M4A to a Pioneer player.
_FILE_TYPES = {".mp3": 1, ".m4a": 4, ".mp4": 4, ".aac": 4, ".flac": 5, ".wav": 11,
               ".aif": 12, ".aiff": 12}


def _file_type(path: Path) -> int:
    return _FILE_TYPES.get(path.suffix.lower(), 1)


def _kbps(bitrate: int | None) -> int | None:
    """Generic bits per second → Pioneer's kbps."""
    if not bitrate:
        return None
    return int(round(bitrate / 1000))


def _audio_format(path: Path) -> tuple[int | None, int | None]:
    """(sample rate, bit depth) read from the COPIED file, or Nones.

    Not in the generic model, and not worth adding for one target: the file is
    right here, and reading its header costs a millisecond.
    """
    try:
        import mutagen

        info = getattr(mutagen.File(str(path)), "info", None)
    except Exception:  # an unreadable file still exports; these stay empty
        return None, None
    rate = getattr(info, "sample_rate", None)
    depth = getattr(info, "bits_per_sample", None)
    return (int(rate) if rate else None), (int(depth) if depth else None)


def _loop_beats(start: float, length: float, beats: list[tuple[int, float, float]]) -> int | None:
    """A loop's length in whole beats at the tempo governing its start.

    rekordbox stores it (`loop_num`/`loop_den`, e.g. 4/1). A loop that is not a
    whole number of beats — or a track with no grid — gets none, as rekordbox
    itself writes for a loop set by hand.
    """
    bpm = next((b for _, b, t in reversed(beats) if t <= start + 1e-3), None)
    if not bpm or length <= 0:
        return None
    count = length * bpm / 60.0
    whole = round(count)
    return whole if whole >= 1 and abs(count - whole) < 0.05 else None

