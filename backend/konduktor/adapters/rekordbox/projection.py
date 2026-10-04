"""Rekordbox native rows -> the generic projection.

The only place a ``pyrekordbox`` ORM object becomes a generic `Track` /
`TrackCues`. Everything above this file sees generic types exclusively.
"""
from __future__ import annotations

from ...core import musical_key
from ...core.model import CuePoint, GridMarker, HotcueChip, Track, TrackCues
from . import beatgrid, palette, timebase
from .cue_types import cue_type, role_and_slot

# Rekordbox names keys musically ("Abm", "F", "Ebm") — but its key table also
# holds whatever text a track's key TAG carried when it was imported, so a real
# stick has "12A" beside "Abm". Parsing therefore accepts every notation.
def parse_key(key: str | None) -> tuple[int | None, str | None]:
    """(Camelot wheel position 1-12, mode) for a key name in any notation."""
    return musical_key.parse(key)


# The inverse, for writing. A track arriving from Traktor carries `key` as
# Traktor's own display string ("10m") and `key_wheel`/`key_mode` as the parsed
# position — so writing the string verbatim into a Pioneer library would show
# "10m" where the deck expects "Cm". Rendering from the wheel is the only
# correct crossing. Shared by the adapter and both Pioneer exporters.
def render_key(wheel: int | None, mode: str | None) -> str | None:
    """A Pioneer key name ("Abm", "F") for a Camelot position + mode, spelt as
    rekordbox spells its own (flats; see `core/musical_key`).

    Returns None when the position is unknown, which is honest: a made-up key is
    worse than a blank one, and Pioneer software shows blanks without complaint.
    """
    return musical_key.render(wheel, mode, "musical")


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


def to_track(row, cue_kinds=(), *, stem: bool = False) -> Track:
    """Project one ``DjmdContent`` row.

    `cue_kinds` is this track's ``(Kind, OutMsec, ColorTableIndex)`` from the store's single
    query — passed in rather than read off the row, which would be a per-track
    round trip. "Is this a hot cue, and on which pad?" is `role_and_slot`, the
    same definition `to_track_cues` uses, so the table's count and dots agree
    with the pads shown in the deck (it also rules out the reserved Kind).

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
    chips: list[HotcueChip] = []
    for kind, out_msec, color_code in cue_kinds:
        role, slot = role_and_slot(kind)
        if role == "hotcue" and slot is not None:
            chips.append(HotcueChip(slot=slot, type=cue_type(out_msec),
                                    color=palette.hex_for(color_code)))
    chips.sort(key=lambda chip: chip.slot)
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
        # kbps here; the generic model (and the table) carry bits per second.
        bitrate=(getattr(row, "BitRate", None) or 0) * 1000 or None,
        import_date=_iso_date(getattr(row, "StockDate", None)),
        last_played=None,  # not modelled as a date in master.db
        release_date=_iso_date(getattr(row, "ReleaseDate", None)),
        filepath=str(getattr(row, "FolderPath", "") or "") or None,
        cue_count=len(cue_kinds),
        hotcue_count=len(chips),
        hotcues=chips,
        grid_marker_count=1 if (_bpm(row) and getattr(row, "AnalysisDataPath", None)) else 0,
        grid_locked=False,  # Rekordbox has no per-track grid lock
        # What the FILE is (`store.is_stem`): Rekordbox itself plays a stem
        # file's mix, but Konduktor's deck plays its stems, and the Type column
        # must not disagree with the deck.
        media_kind="stem" if stem else "audio",
    )


def to_track_cues(cue_rows: list, grid: tuple | None, time_offset: float = 0.0) -> TrackCues:
    """Project a track's cues and beatgrid.

    `grid` is the per-beat ``(times, bpms, beats)`` from the ANLZ file, collapsed here
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
        markers = beatgrid.markers_from_beats(*grid)

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
                # rekordbox's clock -> the decoded audio's (see `timebase`); the
                # grid arrives already converted by the store.
                start=timebase.from_pioneer(in_msec / 1000.0, time_offset),
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
    """Rekordbox stores a PALETTE CODE (`ColorTableIndex`), not an RGB value;
    `Color` is -1 on a coloured cue. A loop's uncoloured convention is code 0."""
    return palette.hex_for(getattr(row, "ColorTableIndex", None))
