"""Rekordbox native rows -> the generic projection.

The only place a ``pyrekordbox`` ORM object becomes a generic `Track` /
`TrackCues`. Everything above this file sees generic types exclusively.
"""
from __future__ import annotations

import re

from ...core.model import CuePoint, GridMarker, Track, TrackCues
from . import beatgrid
from .cue_types import cue_type, role_and_slot

# Rekordbox names keys musically ("Abm", "F", "Ebm") rather than in Camelot or
# Open Key notation. Parsing that is platform knowledge and belongs here.
_KEY_RE = re.compile(r"^([A-G])([#b]?)(m?)$", re.I)

# Camelot wheel position for each pitch class, per mode.
_CAMELOT_MINOR = {
    "A": 8, "E": 9, "B": 10, "F#": 11, "Gb": 11, "C#": 12, "Db": 12,
    "G#": 1, "Ab": 1, "D#": 2, "Eb": 2, "A#": 3, "Bb": 3,
    "F": 4, "C": 5, "G": 6, "D": 7,
}
_CAMELOT_MAJOR = {
    "C": 8, "G": 9, "D": 10, "A": 11, "E": 12, "B": 1, "F#": 2, "Gb": 2,
    "C#": 3, "Db": 3, "G#": 4, "Ab": 4, "D#": 5, "Eb": 5, "A#": 6, "Bb": 6,
    "F": 7,
}


def parse_key(key: str | None) -> tuple[int | None, str | None]:
    """(Camelot wheel position 1-12, mode) for a Rekordbox key name."""
    if not key:
        return None, None
    m = _KEY_RE.match(key.strip())
    if not m:
        return None, None
    letter, accidental, minor = m.group(1).upper(), m.group(2), m.group(3)
    pitch = letter + (accidental.replace("B", "b") if accidental else "")
    table = _CAMELOT_MINOR if minor else _CAMELOT_MAJOR
    n = table.get(pitch)
    return (n, "minor" if minor else "major") if n else (None, None)


# The inverse, for EXPORT. A track arriving from Traktor carries `key` as
# Traktor's own display string ("10m") and `key_wheel`/`key_mode` as the parsed
# position — so writing the string verbatim into a Pioneer library would show
# "10m" where the deck expects "Abm". Rendering from the wheel is the only
# correct crossing. Shared with the OneLibrary exporter: same vendor, same
# notation, and two tables would drift.
_MINOR_NAMES = {v: k for k, v in reversed(list(_CAMELOT_MINOR.items()))}
_MAJOR_NAMES = {v: k for k, v in reversed(list(_CAMELOT_MAJOR.items()))}


def render_key(wheel: int | None, mode: str | None) -> str | None:
    """A Pioneer-style key name ("Abm", "F") for a Camelot position + mode.

    Returns None when the position is unknown, which is honest: a made-up key is
    worse than a blank one, and Pioneer software shows blanks without complaint.
    """
    if not wheel or not (1 <= wheel <= 12):
        return None
    if mode == "minor":
        name = _MINOR_NAMES.get(wheel)
        return f"{name}m" if name else None
    return _MAJOR_NAMES.get(wheel)


def _iso_date(value) -> str | None:
    """Rekordbox already stores ISO-ish 'YYYY-MM-DD'; blanks become None."""
    if not value:
        return None
    s = str(value).strip()
    return s or None


def _bpm(row) -> float | None:
    """``djmdContent.BPM`` is the tempo x100, and 0 means 'not known'.

    It is a display value only — the real grid is in the ANLZ file — and it is
    genuinely 0 for analysed tracks in real libraries.
    """
    raw = getattr(row, "BPM", None)
    if not raw:
        return None
    return round(raw / 100.0, 2)


def _name_of(related) -> str | None:
    """A joined lookup row's display name, or None when unset."""
    if related is None:
        return None
    for attr in ("Name", "ScaleName"):
        value = getattr(related, attr, None)
        if value:
            return str(value)
    return None


def to_track(row, cue_counts: tuple[int, int] = (0, 0)) -> Track:
    """Project one ``DjmdContent`` row.

    `cue_counts` is ``(total, hotcues)`` from the store's single aggregate
    query — passed in rather than read off the row, which would be a per-track
    round trip.

    **`grid_marker_count` is an approximation here**: the real count needs the
    track's ANLZ file, which is far too slow to read for every track at open
    (~12 s on a library of 8,500). A track with a BPM is reported as 1 (constant
    tempo, true for all but a fraction of a percent of real tracks) and the value
    is corrected the moment the track's grid is actually read.

    BPM — not the presence of an analysis file — is the signal, because Rekordbox
    analyses one-shot samples too and gives them an ANLZ with no beatgrid at all.
    On the reference library the two sets agree exactly: all 22 tracks with no
    BPM are precisely the 22 with no grid.
    """
    total_cues, hotcues = cue_counts
    key_name = _name_of(getattr(row, "Key", None))
    wheel, mode = parse_key(key_name)
    return Track(
        id=str(row.ID),
        artist=_name_of(getattr(row, "Artist", None)),
        title=getattr(row, "Title", None),
        album=_name_of(getattr(row, "Album", None)),
        genre=_name_of(getattr(row, "Genre", None)),
        label=_name_of(getattr(row, "Label", None)),
        remixer=_name_of(getattr(row, "Remixer", None)),
        producer=None,  # Rekordbox has Composer, which is not the same field
        mix=None,
        comment=getattr(row, "Commnt", None),
        bpm=_bpm(row),
        key=key_name,
        key_wheel=wheel,
        key_mode=mode,
        # Rekordbox stores 0-5 stars directly — no /51 conversion, unlike Traktor.
        rating=max(0, min(5, int(getattr(row, "Rating", 0) or 0))),
        playcount=getattr(row, "DJPlayCount", None),
        length=getattr(row, "Length", None),
        bitrate=getattr(row, "BitRate", None),
        import_date=_iso_date(getattr(row, "StockDate", None)),
        last_played=None,  # not modelled as a date in master.db
        release_date=_iso_date(getattr(row, "ReleaseDate", None)),
        filepath=str(getattr(row, "FolderPath", "") or "") or None,
        cue_count=total_cues,
        hotcue_count=hotcues,
        grid_marker_count=1 if (_bpm(row) and getattr(row, "AnalysisDataPath", None)) else 0,
        grid_locked=False,  # Rekordbox has no per-track grid lock
        media_kind="audio",
    )


def to_track_cues(cue_rows: list, grid: tuple[list[float], list[float]] | None) -> TrackCues:
    """Project a track's cues and beatgrid.

    `grid` is the per-beat ``(times, bpms)`` from the ANLZ file, collapsed here
    into the generic marker list. Rekordbox pairs nothing with a grid marker, so
    `companion` is always None — that is a Traktor convention.

    `editable` says whether the adapter will accept a command on THIS cue, which
    is the only thing the UI gates on. Every hot cue is editable, loops included.

    **Memory cues are not**: Rekordbox is the only platform that has them, so
    under the two-platform promotion rule they are shown and never written. A cue
    sitting on the reserved `Kind` occupies no pad and is projected the same way,
    because that is how Rekordbox itself displays one.
    """
    markers: list[GridMarker] = []
    if grid is not None:
        times, bpms = grid
        markers = beatgrid.markers_from_beats(times, bpms)

    cues: list[CuePoint] = []
    for c in cue_rows:
        role, slot = role_and_slot(getattr(c, "Kind", None))
        out_msec = getattr(c, "OutMsec", None)
        in_msec = getattr(c, "InMsec", 0) or 0
        kind = cue_type(out_msec)
        length = 0.0
        if out_msec is not None and out_msec > 0:
            length = max(0.0, (out_msec - in_msec) / 1000.0)
        editable = role == "hotcue"
        cues.append(
            CuePoint(
                name=(getattr(c, "Comment", None) or None),
                type=kind,
                role=role,
                start=in_msec / 1000.0,
                length=length,
                slot=slot,
                color=_cue_color(c),
                editable=editable,
                readonly_reason=None if editable else "platform_managed",
                grid_marker=None,
            )
        )
    return TrackCues(grid_markers=markers, grid_locked=False, cues=cues)


def _cue_color(row) -> str | None:
    """Rekordbox stores a PALETTE INDEX, not an RGB value.

    The generic model carries ``#RRGGBB``, and the built-in palette that index
    refers to is not yet known, so no colour is claimed rather than a wrong one
    being invented. `capabilities.cues.color` already says "palette", so the UI
    knows not to offer a free colour picker.
    """
    return None
