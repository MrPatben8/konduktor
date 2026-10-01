"""A NEW track's rows and files in a `master.db` library.

Shared by the two places a track is created rather than edited: the Rekordbox
Library exporter (a `master.db` from nothing) and `RekordboxStore.add_tracks`
(a track added to the user's own library). One definition, because a track
row, its lookups and its analysis files are the same thing in both, and two
copies would drift.

What a track Rekordbox itself adds looks like was MEASURED (two files dragged
into Rekordbox 7.2's collection, 2026-10-01, every row and file diffed): a
`djmdContent` row, three ANLZ files plus a `.3EX`, the embedded cover as three
JPEGs, one `contentFile` row per file (its `Hash` the file's MD5), and its own
analysis results — key, mixer gain, phrases — which nothing here can produce.
"""
from __future__ import annotations

import uuid as uuidlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from . import anlz_writer as W
from . import artwork

#: `djmdContent.ContentLink` — NOT optional, and a bit field. Measured by giving
#: five tracks of a Konduktor-written library different values and loading it
#: in Rekordbox 7: with it NULL the song-list preview is plain blue with a "?"
#: beside CUE; any value colours the preview; the 0x200000 bit (0x2C060E here,
#: 0x0C060E without it) clears the "?"; and 0x3D060E — what the real library's
#: MP3/M4A tracks carry, and what Rekordbox writes for a track it analyses
#: itself — adds two more bits that draw an extra badge, most likely claiming
#: phrase/vocal analysis Konduktor does not write. So the value that claims
#: only what is written: 0x2C060E.
CONTENT_LINK = 2885134


def anlz_rel(track_uuid: str) -> str:
    """`djmdContent.AnalysisDataPath` for a track. Rekordbox derives it from
    the track's UUID, and the column and the file have to agree."""
    return f"/PIONEER/USBANLZ/{track_uuid[:3]}/{track_uuid[3:]}/ANLZ0000.DAT"


def artwork_rel(track_uuid: str) -> str:
    """The track's artwork FOLDER — the same UUID split as its analysis."""
    return f"/PIONEER/Artwork/{track_uuid[:3]}/{track_uuid[3:]}"


def anlz_files(audio: Path, measured, beats: list | None) -> dict[str, bytes]:
    """The analysis files for a track, by suffix: `.DAT`, `.EXT`, `.2EX`.

    The same files as a rekordbox-analysed track. The `.DAT` needs PVBR and the
    preview waveforms before rekordbox shows a grid at all (measured on a
    OneLibrary stick; the same Pioneer file here); the `.EXT`/`.2EX` carry the
    drawn waveforms. Cue lists are present but EMPTY — master.db keeps cues in
    djmdCue. `measured` is `waveform.analyse`'s result (None: flat previews).
    `beats` is `(beat-in-bar, bpm, seconds)` ON REKORDBOX'S CLOCK; None writes
    no `PQTZ`, and `[]` an empty one for `write_pqtz` to fill later.
    """
    path = str(audio)
    tags = [W.path_tag(path), W.vbr_tag(W.mp3_samples(audio))]
    if beats is not None:
        tags.append(W.beatgrid_tag(beats))
    if measured:
        tags += W.preview_tags(measured.columns.rms, measured.columns.brightness)
        ext_waves, two_ex = W.waveform_tags(measured.frames.bands)
    else:
        tags += W.flat_preview_tags()
        ext_waves, two_ex = [], []
    tags += W.cue_tags([], extended=False)
    out = {
        ".DAT": W.anlz_bytes(tags),
        ".EXT": W.anlz_bytes([W.path_tag(path), *ext_waves[:1],
                              *W.cue_tags([], extended=True), *ext_waves[1:]]),
    }
    if two_ex:
        out[".2EX"] = W.anlz_bytes([W.path_tag(path), *two_ex])
    return out


def artwork_files(art: bytes | None) -> dict[str, bytes] | None:
    """The cover as a rekordbox-managed track carries it, by file name.

    `artwork.jpg` (the cover, fit to 800 px), `artwork_m.jpg` (240) and
    `artwork_s.jpg` (80), the last two letterboxed square; `djmdContent.
    ImagePath` names `artwork.jpg`. All measured on a real Rekordbox 7 library.
    None when there is no art or it cannot be read.
    """
    if not art:
        return None
    jpegs = artwork.library_jpegs(art)
    if jpegs is None:
        return None
    return dict(zip(("artwork.jpg", "artwork_m.jpg", "artwork_s.jpg"), jpegs))


def lookup(db, kind: str, name: str | None):
    """Find-or-create a lookup row, returning its id.

    Lookups are foreign keys; pyrekordbox's `add_*` RAISES on an existing name,
    so every one is find-or-create — the same rule the in-place adapter follows.
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
        return insert_key(db, name)
    found = {"artist": db.get_artist, "album": db.get_album,
             "genre": db.get_genre, "label": db.get_label}[kind](Name=name).first()
    if found is not None:
        return found.ID
    created = {"artist": db.add_artist, "album": db.add_album,
               "genre": db.add_genre, "label": db.add_label}[kind](name)
    db.flush()
    return created.ID


def insert_key(db, name: str) -> str:
    """A `djmdKey` row. Raw SQL: the ORM's DateTime binder raises on the NULLs
    a real row carries."""
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
