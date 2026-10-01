"""Writing a fresh Rekordbox `master.db` from nothing.

## The shape, and why it is not a hand-built schema

The handoff expected this to be the hard target — "a from-scratch SQLCipher
master.db, lookup-table FKs, USN bookkeeping". Most of that turned out to be
available:

  * the **schema** is the checked-in DDL of a real Rekordbox 7 library,
    `fixtures/rekordbox/schema.sql`. NOT `Base.metadata.create_all()`, which was
    the plan until it failed: the ORM models 37 tables where the real thing has
    47, and marks columns NOT NULL that Rekordbox leaves nullable — a database
    built from the models refuses `agentRegistry` rows Rekordbox writes daily;
  * the **key** is the same one reading uses, `deobfuscate(BLOB)`, so an
    exported database is readable by any Rekordbox that reads the user's own;
  * `add_content`, `create_playlist`, `create_playlist_folder` and
    `add_to_playlist` already exist and assign the ids, UUIDs and USNs.

So this fills a new database through the library's own API rather than inventing
row shapes — the same "create, then use the ordinary write path" the Traktor
exporter uses, for the same reason: a second definition would drift from the one
the read path is tested against.

## What is genuinely written here

**Cue rows and their JSON mirror.** Rekordbox keeps cues in `djmdCue` AND in a
`contentCue.Cues` mirror that must agree; leaving the mirror stale is this
platform's version of Traktor's companion-cue desync. Field conventions are the
adapter's, not guessed: positions in frames at **150 fps**, `ContentUUID` is the
TRACK's UUID, an uncoloured cue is `Color=-1`, a loop is `Color=255` with
`BeatLoopSize = (beats << 16) | 1`, and `Kind` is the **sparse** bank
`1,2,3,5,6,7,8,9` that skips 4 — taken from `cue_types.kind_for`, not
redefined, because two copies of a mapping that subtle would drift.

**Analysis files.** The grid lives in a per-track ANLZ `PQTZ` tag, not in the
database, so each track gets one built by the shared `anlz_writer` and pointed
at by `djmdContent.AnalysisDataPath`. Rekordbox derives that path from the
track's UUID — `/PIONEER/USBANLZ/<first 3>/<rest>/ANLZ0000.DAT` — which is
reproduced here because the column and the file have to agree. That path is
rooted at a **`share/` directory beside `master.db`**, not at the library root.

## The catch worth knowing

`djmdContent.FolderPath` is an **absolute host path**, unlike Traktor's
volume-relative LOCATION and OneLibrary's drive-relative path. A Rekordbox
export is therefore NOT portable to another machine by copying the folder: it
describes files where they are right now. Swapping it into a Rekordbox install
on the same machine works; carrying it to a gig does not.
"""
from __future__ import annotations

import logging
import uuid as uuidlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from ...core.export import ExportPayload, ExportTrack, WrittenLibrary
from ...core.cue_colors import effective_color
from . import new_content, palette
from . import timebase
from .beatgrid import beats_from_markers
from .capabilities import capabilities_for
from .cue_types import kind_for
from .projection import render_key
from .store import rekordbox_probe_suppressed

log = logging.getLogger(__name__)

#: A real Rekordbox 7 library's DDL. See the fixture's README for why the ORM's
#: `create_all()` is not used.
_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "rekordbox"
SCHEMA = _FIXTURES / "schema.sql"
#: Rekordbox's own scaffolding rows — menu items, colours, categories, sort
#: options. Not optional: `add_content` looks up djmdMenuItems('TRACK') and
#: raises on an empty table. Carries no user data and no machine identity;
#: `djmdDevice`/`djmdProperty` are generated per export instead.
SEED = _FIXTURES / "seed.sql"

#: Rekordbox counts cue positions in frames at 150 fps, not milliseconds.
FRAMES_PER_SECOND = 150

#: `djmdContent.ContentLink` — see `new_content`, where its bits are measured.
CONTENT_LINK = new_content.CONTENT_LINK

#: Rekordbox's playlist side file, beside master.db. It lists every playlist
#: node with a `Timestamp` matching the row's `updated_at` (ms); a playlist the
#: file does not vouch for is not shown. The skeleton is a real Rekordbox 7
#: file's, minus its nodes — pyrekordbox adds ours as it creates them.
PLAYLISTS_XML = "masterPlaylists6.xml"
PLAYLISTS_XML_SKELETON = (
    '<?xml version="1.0" encoding="UTF-8"?>\n\n'
    '<MASTER_PLAYLIST Version="3.0.0" AutomaticSync="0">\n'
    '  <PRODUCT Name="rekordbox" Version="7.2.18" Company="Pioneer DJ"/>\n'
    '  <PLAYLISTS>\n  </PLAYLISTS>\n'
    '</MASTER_PLAYLIST>\n'
)
OTHER_PLAYLIST = "Other"


class RekordboxExporter:
    platform = "rekordbox"
    menu_order = 40
    #: A COMPUTER library (master.db), which Rekordbox never reads from a stick —
    #: named so, beside "Rekordbox Export" (the stick's Device Library).
    display_name = "Rekordbox Library"
    #: Rekordbox opens a master.db only from its own library folder.
    computer_only = True
    library_filename = "master.db"
    drive_root = False

    def capabilities(self):
        """What a Rekordbox library can hold, with no library to read.

        `capabilities_for` normally takes the loaded library's version and cloud
        state; a brand-new one has no version and is definitionally not synced.
        """
        return capabilities_for(
            None,
            editable_fields=[
                "title", "artist", "album", "genre", "label", "remixer",
                "release_date", "comment", "rating",
            ],
        )

    def write(self, payload: ExportPayload, destination: Path) -> WrittenLibrary:
        # `masterdb`'s OWN key blob. `devicelib_plus` has a different one — a
        # OneLibrary drive uses a universal key shared across vendors, and
        # master.db does not. Mixing them up produces a database Rekordbox
        # cannot open, and pyrekordbox rejects the wrong one outright.
        import sqlcipher3.dbapi2 as sqlcipher
        from pyrekordbox.masterdb import MasterDatabase, models
        from pyrekordbox.masterdb.database import BLOB
        from pyrekordbox.utils import deobfuscate

        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        library = root / self.library_filename
        if library.exists():
            library.unlink()  # SQLCipher will not re-key an existing file

        key = deobfuscate(BLOB)
        con = sqlcipher.connect(str(library))
        try:
            con.execute(f"PRAGMA key='{key}'")
            con.executescript(SCHEMA.read_text())
            con.executescript(SEED.read_text())
            con.commit()
        finally:
            con.close()

        # BEFORE the database is opened: pyrekordbox keeps masterPlaylists6.xml
        # in step with every playlist it creates — but only if the file exists
        # at open time. Without it Rekordbox opened the library, generated its
        # own file listing our playlists with Timestamp 0, and showed NONE of
        # them (measured by swapping an export into a real Rekordbox 7).
        playlists_xml = root / PLAYLISTS_XML
        playlists_xml.write_text(PLAYLISTS_XML_SKELETON, encoding="utf-8")

        db = MasterDatabase(path=str(library), key=key)
        extra: list[Path] = [playlists_xml]
        ids: dict[str, str] = {}
        try:
            self._seed_registry(db, models)
            self._seed_device(db, payload)
            rows = {}
            total = len(payload.tracks)
            for n, item in enumerate(payload.tracks, start=1):
                # Each track is decoded for its waveform: the slow part.
                payload.checkpoint(f"Analysing {item.track.title or item.destination.name} ({n}/{total})", step=n, of=total)
                content = self._add_track(db, item)
                rows[item.source_id] = content
                ids[item.source_id] = str(content.ID)
                extra += self._write_anlz(item, content, root, db)
                extra += self._write_art(item, content, root)
            self._write_playlists(db, payload, rows)
            # A brand-new file: a running Rekordbox cannot have it open, so its
            # process-wide veto protects nothing here.
            with rekordbox_probe_suppressed():
                db.commit()
        finally:
            db.close()

        self._replay_cues(library, payload, ids)
        return WrittenLibrary(library=library, extra=extra)

    @staticmethod
    def _replay_cues(library: Path, payload: ExportPayload, ids: dict) -> None:
        """Write the cues through the ORDINARY cue-row code, not by hand.

        Same reason the Traktor exporter replays commands: `set_cue` already
        owns every convention a cue row needs — the sparse `Kind` bank, frames
        at 150 fps, the loop encoding, and `_sync_content_cue`, which rebuilds
        the `contentCue` JSON mirror the rows must agree with. Writing those
        again here would be a second definition of all of it, untested, and a
        stale mirror is this platform's version of a desynced companion cue.
        """
        from .adapter import RekordboxAdapter

        adapter = RekordboxAdapter(library)
        # The store, not the adapter's commands: the adapter refuses memory cues
        # (editing them in a user's library is off-limits) and takes no colour.
        # Building a NEW library is neither an edit nor lossy by choice, so the
        # exporter writes both — through the same `_write_cue` every edit uses.
        store = adapter._store
        try:
            for item in payload.tracks:
                track_id = ids.get(item.source_id)
                if track_id is None or not item.cues:
                    continue
                for cue in item.cues.cues:
                    if cue.role == "memory" or cue.slot is None:
                        # rekordbox's own "unset" for a memory cue with no colour.
                        code, _rgb = palette.code_for(cue.color)
                        store.add_memory_cue(
                            track_id, start_sec=cue.start, cue_type=cue.type,
                            length_sec=cue.length, name=cue.name, color_code=code or None,
                        )
                        continue
                    # What Konduktor SHOWS — stored colour, else the type's (blue
                    # cue, green loop) — as a palette code, which is what
                    # rekordbox draws from. White (a grid companion) has no swatch:
                    # code 0, i.e. uncoloured.
                    code, _rgb = palette.code_for(effective_color(cue))
                    store.set_cue(
                        track_id, slot=cue.slot, start_sec=cue.start, cue_type=cue.type,
                        length_sec=cue.length, name=cue.name, color_code=code or None,
                    )
            adapter.save()
        finally:
            adapter.close()

    # ---- rows -----------------------------------------------------------------

    @staticmethod
    def _seed_registry(db, models) -> None:
        """`agentRegistry.localUpdateCount` is where Rekordbox's USNs come from.

        A database with no row there has nothing for the library's own
        bookkeeping to increment, so it is created before anything else.
        """
        existing = db.query(models.AgentRegistry).filter_by(
            registry_id="localUpdateCount").first()
        if existing is not None:
            return
        # Inserted with raw SQL, NOT the ORM. `AgentRegistry`'s DateTime columns
        # bind through a processor that calls `.astimezone()` unconditionally,
        # so a NULL `date_1` — which is exactly what a real Rekordbox library
        # has — raises on INSERT. Reading such a row works; writing one does
        # not. Raw SQL sidesteps the processor and stores what Rekordbox stores.
        now = datetime.now().isoformat(sep=" ", timespec="milliseconds")
        db.session.execute(
            text('INSERT INTO "agentRegistry" (registry_id, int_1, created_at, '
                 "updated_at) VALUES (:k, 1, :now, :now)"),
            {"k": "localUpdateCount", "now": now},
        )
        db.flush()

    @staticmethod
    def _seed_device(db, payload: ExportPayload) -> None:
        """This library's own identity — a fresh device and DB id per export.

        Deliberately NOT copied from the fixture: a real `djmdDevice` row holds
        the machine's NAME and UUIDs, which have no business in a repo or in
        somebody else's exported library. Rekordbox stamps content rows with
        `MasterDBID`, so the value has to exist; it just has to be ours.
        """
        now = datetime.now().isoformat(sep=" ", timespec="milliseconds")
        device_id = str(uuidlib.uuid4())
        db_id = str(uuidlib.uuid4().int % 2_147_483_647)
        db.session.execute(
            text('INSERT INTO "djmdProperty" (DBID, DBVersion, DeviceID, '
                 "created_at, updated_at) VALUES (:db, '6000', :dev, :now, :now)"),
            {"db": db_id, "dev": device_id, "now": now},
        )
        db.session.execute(
            text('INSERT INTO "djmdDevice" (ID, MasterDBID, Name, UUID, '
                 "rb_local_usn, created_at, updated_at) "
                 "VALUES (:dev, :db, :name, :uuid, 1, :now, :now)"),
            {"dev": device_id, "db": db_id, "name": payload.name,
             "uuid": str(uuidlib.uuid4()), "now": now},
        )
        db.flush()

    def _add_track(self, db, item: ExportTrack):
        """One `djmdContent` row, through pyrekordbox's own helper."""
        track = item.track
        content = db.add_content(
            str(item.destination),
            Title=track.title,
            BPM=int(round((track.bpm or 0) * 100)) or None,
            Length=track.length,
            Rating=track.rating or 0,
            Commnt=track.comment,
            ReleaseDate=track.release_date,
        )
        # Lookups are foreign keys; pyrekordbox's add_* RAISES on an existing
        # name, so every one is find-or-create — the same rule the in-place
        # adapter follows.
        content.ArtistID = self._lookup(db, "artist", track.artist)
        content.AlbumID = self._lookup(db, "album", track.album)
        content.GenreID = self._lookup(db, "genre", track.genre)
        content.LabelID = self._lookup(db, "label", track.label)
        # NOT `track.key`: that is the SOURCE platform's notation, so a Traktor
        # export would put "10m" where Rekordbox shows "Cm".
        content.KeyID = self._lookup(db, "key", render_key(track.key_wheel, track.key_mode))
        content.ContentLink = CONTENT_LINK
        db.flush()
        return content

    @staticmethod
    def _lookup(db, kind: str, name: str | None):
        return new_content.lookup(db, kind, name)

    # ---- analysis ---------------------------------------------------------------

    def _write_anlz(self, item: ExportTrack, content, root: Path, db) -> list[Path]:
        """The ANLZ file carrying this track's grid, and the column pointing at it.

        Rekordbox derives the path from the track's UUID, and the database column
        and the file on disk have to agree — so the same derivation is used here
        rather than a path of our own choosing.
        """
        rel = new_content.anlz_rel(str(content.UUID))
        content.AnalysisDataPath = rel
        content.Analysed = 105        # what a rekordbox-analysed track carries
        content.AnalysisUpdated = 1
        db.flush()

        off = timebase.offset(item.destination)
        measured = item.waveform(lead=off)
        beats = None
        markers = item.cues.grid_markers if item.cues else []
        if markers:
            duration = ((measured.duration if measured else 0.0)
                        or float(item.track.length or 0) or (markers[-1].start + 60.0))
            nums, bpms, times = beats_from_markers(markers, duration)
            if nums:
                # Onto rekordbox's clock (~25 ms behind on MP3/AAC — `timebase`).
                # The cues need no such line: they are replayed through
                # `set_cue`, which applies the same offset itself.
                beats = [(n, bpm, timebase.to_pioneer(t, off))
                         for n, bpm, t in zip(nums, bpms, times)]
        # AnalysisDataPath is rooted at a `share` directory BESIDE master.db,
        # not at the library root — the read path joins it that way, and writing
        # it anywhere else produces a track whose grid silently reads as empty.
        dat = root / "share" / rel.lstrip("/")
        written = []
        for suffix, data in new_content.anlz_files(item.destination, measured, beats).items():
            target = dat.with_suffix(suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(target)
        return written

    # ---- artwork ------------------------------------------------------------------

    @staticmethod
    def _write_art(item: ExportTrack, content, root: Path) -> list[Path]:
        """The cover, as a rekordbox-managed track carries it.

        Three JPEGs under `share/PIONEER/Artwork/<uuid[:3]>/<uuid[3:]>/` — the
        same UUID split as the analysis folder — named `artwork.jpg` (the cover,
        fit to 800 px), `artwork_m.jpg` (240) and `artwork_s.jpg` (80), the last
        two letterboxed square. `djmdContent.ImagePath` names `artwork.jpg`,
        rooted at `share/` like `AnalysisDataPath`. All measured on a real
        Rekordbox 7 library.
        """
        files = new_content.artwork_files(item.art[0] if item.art else None)
        if files is None:
            return []
        rel = new_content.artwork_rel(str(content.UUID))
        folder = root / "share" / rel.lstrip("/")
        folder.mkdir(parents=True, exist_ok=True)
        written = []
        for name, data in files.items():
            (folder / name).write_bytes(data)
            written.append(folder / name)
        content.ImagePath = f"{rel}/artwork.jpg"
        return written

    # ---- playlists ---------------------------------------------------------------

    def _write_playlists(self, db, payload: ExportPayload, rows: dict) -> None:
        """The tree, under one folder named after the export."""
        root = db.create_playlist_folder(payload.name)
        db.flush()
        folders: dict[tuple[str, ...], object] = {(): root}

        for playlist in payload.playlists:
            parent = root
            for depth in range(1, len(playlist.folders) + 1):
                branch = tuple(playlist.folders[:depth])
                if branch not in folders:
                    folders[branch] = db.create_playlist_folder(
                        branch[-1], parent=folders[branch[:-1]])
                    db.flush()
                parent = folders[branch]
            self._fill(db, db.create_playlist(playlist.name, parent=parent),
                       playlist.track_ids, rows)

        claimed = {t for p in payload.playlists for t in p.track_ids}
        loose = [t.source_id for t in payload.tracks if t.source_id not in claimed]
        if loose:
            self._fill(db, db.create_playlist(OTHER_PLAYLIST, parent=root), loose, rows)

    @staticmethod
    def _fill(db, playlist, track_ids: list[str], rows: dict) -> None:
        db.flush()
        for source_id in track_ids:
            content = rows.get(source_id)
            if content is not None:
                db.add_to_playlist(playlist, content)
        db.flush()

