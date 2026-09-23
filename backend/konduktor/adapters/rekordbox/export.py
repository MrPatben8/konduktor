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
from . import anlz_writer as W
from .beatgrid import beats_from_markers
from .capabilities import capabilities_for
from .cue_types import kind_for
from .projection import render_key

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
OTHER_PLAYLIST = "Other"


class RekordboxExporter:
    platform = "rekordbox"
    library_filename = "master.db"

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

        db = MasterDatabase(path=str(library), key=key)
        extra: list[Path] = []
        ids: dict[str, str] = {}
        try:
            self._seed_registry(db, models)
            self._seed_device(db, payload)
            rows = {}
            for item in payload.tracks:
                content = self._add_track(db, item)
                rows[item.source_id] = content
                ids[item.source_id] = str(content.ID)
                extra += self._write_anlz(item, content, root, db)
            self._write_playlists(db, payload, rows)
            db.commit()
        finally:
            db.close()

        self._replay_cues(library, payload, ids)
        return WrittenLibrary(library=library, extra=extra)

    @staticmethod
    def _replay_cues(library: Path, payload: ExportPayload, ids: dict) -> None:
        """Write the cues through the ORDINARY adapter, not by hand.

        Same reason the Traktor exporter replays commands: `set_cue` already
        owns every convention a cue row needs — the sparse `Kind` bank, frames
        at 150 fps, the loop encoding, and `_sync_content_cue`, which rebuilds
        the `contentCue` JSON mirror the rows must agree with. Writing those
        again here would be a second definition of all of it, untested, and a
        stale mirror is this platform's version of a desynced companion cue.
        """
        from .adapter import RekordboxAdapter

        adapter = RekordboxAdapter(library)
        try:
            for item in payload.tracks:
                track_id = ids.get(item.source_id)
                if track_id is None or not item.cues:
                    continue
                for cue in item.cues.cues:
                    # Memory cues are a Rekordbox-only concept the generic model
                    # preserves but never writes; a cue with no slot has no pad
                    # to land on.
                    if cue.role != "hotcue" or cue.slot is None:
                        continue
                    adapter.set_cue(
                        track_id,
                        slot=cue.slot,
                        start_sec=cue.start,
                        cue_type=cue.type,
                        length_sec=cue.length,
                        name=cue.name,
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
        db.flush()
        return content

    @staticmethod
    def _lookup(db, kind: str, name: str | None):
        """Find-or-create a lookup row, returning its id.

        `djmdKey` is the odd one out twice over: its name column is `ScaleName`,
        not `Name`, and pyrekordbox has no `add_key`. So keys are inserted
        directly, with a `Seq` that keeps them in wheel order in Rekordbox's UI.
        """
        if not name:
            return None
        if kind == "key":
            found = db.get_key(ScaleName=name).first()
            if found is not None:
                return found.ID
            return _insert_key(db, name)
        found = {"artist": db.get_artist, "album": db.get_album,
                 "genre": db.get_genre, "label": db.get_label}[kind](Name=name).first()
        if found is not None:
            return found.ID
        created = {"artist": db.add_artist, "album": db.add_album,
                   "genre": db.add_genre, "label": db.add_label}[kind](name)
        db.flush()
        return created.ID

    # ---- analysis ---------------------------------------------------------------

    def _write_anlz(self, item: ExportTrack, content, root: Path, db) -> list[Path]:
        """The ANLZ file carrying this track's grid, and the column pointing at it.

        Rekordbox derives the path from the track's UUID, and the database column
        and the file on disk have to agree — so the same derivation is used here
        rather than a path of our own choosing.
        """
        track_uuid = str(content.UUID)
        # Rekordbox derives these two levels from the track's UUID, and the
        # column and the file have to agree — so the same derivation is used.
        rel = f"/PIONEER/USBANLZ/{track_uuid[:3]}/{track_uuid[3:]}/ANLZ0000.DAT"
        content.AnalysisDataPath = rel
        content.Analysed = 105        # what a rekordbox-analysed track carries
        content.AnalysisUpdated = 1
        db.flush()

        tags = [W.path_tag(str(item.destination))]
        markers = item.cues.grid_markers if item.cues else []
        if markers:
            duration = float(item.track.length or 0) or (markers[-1].start + 60.0)
            nums, bpms, times = beats_from_markers(markers, duration)
            if nums:
                tags.append(W.beatgrid_tag(list(zip(nums, bpms, times))))
        # AnalysisDataPath is rooted at a `share` directory BESIDE master.db,
        # not at the library root — the read path joins it that way, and writing
        # it anywhere else produces a track whose grid silently reads as empty.
        return [W.write_anlz(root / "share" / rel.lstrip("/"), tags)]

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


def _insert_key(db, name: str) -> str:
    """A `djmdKey` row. Raw SQL for the same reason the registry row is: the
    ORM's DateTime binder raises on the NULLs a real row carries."""
    from pyrekordbox.masterdb import models

    now = datetime.now().isoformat(sep=" ", timespec="milliseconds")
    seq = (db.query(models.DjmdKey).count() or 0) + 1
    key_id = str(uuidlib.uuid4().int % 2_147_483_647)
    db.session.execute(
        text('INSERT INTO "djmdKey" (ID, ScaleName, Seq, UUID, created_at, '
             "updated_at) VALUES (:id, :name, :seq, :uuid, :now, :now)"),
        {"id": key_id, "name": name, "seq": seq,
         "uuid": str(uuidlib.uuid4()), "now": now},
    )
    db.flush()
    return key_id
