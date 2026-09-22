"""Traktor native model -> the generic projection.

The only place an NML dataclass is turned into a generic `Track`/`TrackCues`.
Everything above this file sees generic types exclusively.
"""
from __future__ import annotations

import re

from ...core.model import CuePoint, GridMarker, Track, TrackCues
from . import beatgrid
from .cue_types import NATIVE_TO_CUE_TYPE

# Traktor displays keys in Open Key ("10m" / "8d") or Camelot ("8A" / "8B"),
# depending on a user preference — so the library can contain either. Parsing
# notation is platform knowledge and belongs here, not in the UI.
_OPEN_KEY = re.compile(r"^(\d{1,2})\s*([md])$", re.I)
_CAMELOT = re.compile(r"^(\d{1,2})\s*([ab])$", re.I)
_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


def parse_key(key: str | None) -> tuple[int | None, str | None]:
    """(Camelot wheel position 1-12, mode) for a Traktor display key.

    Open Key numbers the wheel seven semitones round from Camelot, so
    Open Key 1 == Camelot 8 (both are C major / A minor).
    """
    if not key:
        return None, None
    k = key.strip()
    if m := _CAMELOT.match(k):
        n = int(m.group(1))
        if 1 <= n <= 12:
            return n, "minor" if m.group(2).lower() == "a" else "major"
    if m := _OPEN_KEY.match(k):
        n = int(m.group(1))
        if 1 <= n <= 12:
            return ((n + 6) % 12) + 1, "minor" if m.group(2).lower() == "m" else "major"
    return None, None


def iso_date(value: str | None) -> str | None:
    """Traktor writes dates as "YYYY/M/D"; the model carries ISO-8601."""
    if not value:
        return None
    m = _DATE.match(value.strip())
    if not m:
        return value
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def primary_key(location) -> str:
    """Reconstruct the Traktor primary key used by playlist entries."""
    vol = location.volume or ""
    d = location.dir or ""
    f = location.file or ""
    return f"{vol}{d}{f}"


def _display_path(location) -> str:
    """Human-readable OS-ish path from Traktor's '/:'-separated dir."""
    d = (location.dir or "").replace("/:", "/")
    f = location.file or ""
    return f"{d}{f}"


def _rating_stars(ranking) -> int:
    if not ranking:
        return 0
    return max(0, min(5, round(ranking / 51)))


def to_track(e) -> Track:
    info = e.info
    loc = e.location
    # "Is this a cue?" must mean the same here as in `to_track_cues`, which
    # skips grid markers — a marker is projected as a beatgrid marker, not a
    # cue. Counting the raw CUE_V2 list instead made the library table report
    # every track as having one more cue than the deck showed, and made the
    # "has cues: no" filter match nothing, since a gridded track always had at
    # least one. The same class of bug as the Rekordbox hotcue miscount: one
    # question, two answers.
    markers = beatgrid.grid_markers(e)
    cues = [c for c in (e.cue_v2 or []) if getattr(c, "grid", None) is None]
    hotcues = sum(
        1 for c in cues if c.hotcue is not None and c.hotcue >= 0
    )
    wheel, mode = parse_key(info.key if info else None)
    return Track(
        id=primary_key(loc) if loc else (e.title or ""),
        artist=e.artist,
        title=e.title,
        album=e.album.title if e.album else None,
        genre=info.genre if info else None,
        label=info.label if info else None,
        remixer=info.remixer if info else None,
        producer=info.producer if info else None,
        mix=info.mix if info else None,
        comment=info.comment if info else None,
        bpm=beatgrid.effective_bpm(e),
        key=info.key if info else None,
        key_wheel=wheel,
        key_mode=mode,
        rating=_rating_stars(info.ranking if info else None),
        playcount=info.playcount if info else None,
        length=info.playtime if info else None,
        bitrate=info.bitrate if info else None,
        import_date=iso_date(info.import_date if info else None),
        last_played=iso_date(info.last_played if info else None),
        release_date=iso_date(info.release_date if info else None),
        filepath=_display_path(loc) if loc else None,
        cue_count=len(cues),
        hotcue_count=hotcues,
        grid_marker_count=len(markers),
        grid_locked=bool(e.lock),
        media_kind="stem" if getattr(e, "stems", None) is not None else "audio",
    )


def to_track_cues(entry) -> TrackCues:
    """Project an ENTRY's beatgrid + cues.

    The beatgrid is the FULL ordered marker list — a constant grid is a list of
    length one. Grid markers themselves are not cues; their companion cues are,
    because they occupy real hotcue slots, and each is tagged with the marker it
    belongs to so the UI can show it as beatgrid-owned rather than editable.
    """
    markers = beatgrid.grid_markers(entry)
    comps = beatgrid.companions(entry)
    marker_of = {id(c): i for i, c in comps.items()}
    grid_markers = [
        GridMarker(
            start=(m.start or 0.0) / 1000.0,  # Traktor stores START in ms
            bpm=m.grid.bpm if m.grid and m.grid.bpm else 0.0,
            name=m.name,
            companion=comps[i].hotcue if i in comps else None,
        )
        for i, m in enumerate(markers)
    ]
    cues = []
    for c in entry.cue_v2 or []:
        if getattr(c, "grid", None) is not None:
            continue  # a grid marker is projected as a beatgrid marker, not a cue
        marker = marker_of.get(id(c))
        slot = c.hotcue if c.hotcue is not None and c.hotcue >= 0 else None
        cues.append(
            CuePoint(
                name=c.name,
                # An unknown TYPE is projected as a plain cue rather than dropped:
                # preserving a cue we cannot name beats hiding it from the user.
                type=NATIVE_TO_CUE_TYPE.get(c.type, "cue"),
                # Every Traktor cue lives in the hotcue bank.
                role="hotcue",
                start=(c.start or 0.0) / 1000.0,
                length=(c.len or 0.0) / 1000.0,
                slot=slot,
                color=c.color,
                # A grid marker's companion belongs to the beatgrid: the store
                # refuses hotcue commands on it, so say so up front.
                editable=marker is None,
                readonly_reason="beatgrid_companion" if marker is not None else None,
                grid_marker=marker,
            )
        )
    return TrackCues(
        grid_markers=grid_markers, grid_locked=bool(entry.lock), cues=cues
    )
