"""OneLibrary rows -> the generic projection.

The only place a ``devicelib_plus`` ORM object becomes a generic `Track` /
`TrackCues`. Everything above this file sees generic types exclusively.

Field conventions are read off a real rekordbox 7 export rather than guessed:

  * ``bpmx100`` is the tempo in hundredths, and it is a DISPLAY value — the real
    grid is per-beat in the ANLZ file, and a variable-tempo track stores only its
    first tempo here.
  * ``rating`` is 0-5 directly, like `master.db` and unlike Traktor's /51.
  * ``length`` is whole seconds.
  * ``path`` is drive-relative with a leading slash; the store resolves it.
  * dates arrive as `datetime` from the ORM, not as strings.
"""
from __future__ import annotations

from ...core.model import GridMarker, Track, TrackCues
from ..rekordbox.projection import parse_key


def _name_of(related) -> str | None:
    """A joined lookup row's display name, or None when unset."""
    if related is None:
        return None
    value = getattr(related, "name", None)
    return str(value) if value else None


def _iso_date(value) -> str | None:
    """`YYYY-MM-DD` from whatever the column holds.

    The ORM converts the stored strings to `datetime`, so this takes the date
    part of one — but a drive written by another vendor's software may well
    store a plain string, and passing that through unchanged is better than
    losing it.
    """
    if not value:
        return None
    date = getattr(value, "date", None)
    if callable(date):
        try:
            return date().isoformat()
        except Exception:  # noqa: BLE001
            pass
    text = str(value).strip()
    return text.split(" ")[0] if text else None


def _bpm(row) -> float | None:
    raw = getattr(row, "bpmx100", None)
    if not raw:
        return None
    return round(int(raw) / 100.0, 2)


def to_track(row, drive_path: str | None = None) -> Track:
    """Project one ``Content`` row.

    **The three count fields are approximations here**, and deliberately so. Cues
    and the beatgrid both live in the track's ANLZ files, and parsing a track's
    `.EXT` costs ~23 ms — ~23 s for a 1,000-track drive if done at open. So a
    track that has an analysis file and a tempo is reported as having one grid
    marker (constant tempo, true for all but a fraction of a percent of tracks)
    and no cues, and all three are corrected the moment the track's cues are
    actually read. `adapter.track_cues` is where that happens.

    Reporting zero cues rather than a guess is the honest direction to be wrong
    in: a count that appears from nowhere on selection reads as the detail
    arriving, whereas one that shrinks reads as data being lost.
    """
    key_name = _name_of(getattr(row, "key", None))
    wheel, mode = parse_key(key_name)
    has_analysis = bool(getattr(row, "analysisDataFilePath", None))
    bpm = _bpm(row)
    return Track(
        id=str(getattr(row, "path", "") or ""),
        artist=_name_of(getattr(row, "artist", None)),
        title=getattr(row, "title", None),
        album=_name_of(getattr(row, "album", None)),
        genre=_name_of(getattr(row, "genre", None)),
        label=_name_of(getattr(row, "label", None)),
        remixer=_name_of(getattr(row, "remixer", None)),
        # OneLibrary has `composer` and `lyricist`, neither of which is Traktor's
        # "producer" or "mix", so nothing is mapped onto them approximately.
        producer=None,
        mix=None,
        comment=getattr(row, "djComment", None),
        bpm=bpm,
        key=key_name,
        key_wheel=wheel,
        key_mode=mode,
        rating=max(0, min(5, int(getattr(row, "rating", 0) or 0))),
        playcount=getattr(row, "djPlayCount", None),
        length=getattr(row, "length", None),
        bitrate=getattr(row, "bitrate", None),
        import_date=_iso_date(getattr(row, "dateAdded", None)),
        last_played=None,  # the drive records history as playlists, not per track
        release_date=_iso_date(getattr(row, "releaseDate", None))
        or (str(getattr(row, "releaseYear", "") or "") or None),
        filepath=drive_path,
        cue_count=0,
        hotcue_count=0,
        grid_marker_count=1 if (bpm and has_analysis) else 0,
        grid_locked=False,  # OneLibrary has no grid lock
        media_kind="audio",
    )


def to_track_cues(cues: list, grid_markers: list[GridMarker]) -> TrackCues:
    """Project a track's cues and beatgrid.

    `cues` are already generic — see `store.cues` for why that boundary sits
    where it does — so this is the assembly point rather than a translation.
    Nothing here is editable: a OneLibrary drive is read-only in Konduktor, and
    `capabilities.writable` says so at the library level.
    """
    return TrackCues(grid_markers=grid_markers, grid_locked=False, cues=list(cues))
