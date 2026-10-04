"""Traktor native model -> the generic projection.

The only place an NML dataclass is turned into a generic `Track`/`TrackCues`.
Everything above this file sees generic types exclusively.
"""
from __future__ import annotations

import re

from ...core import musical_key
from ...core.model import CuePoint, GridMarker, HotcueChip, Track, TrackCues
from . import beatgrid, timebase
from .cue_types import NATIVE_TO_CUE_TYPE

# Traktor displays keys in Open Key ("10m" / "8d") or Camelot ("8A" / "8B"),
# depending on a user preference, and INFO@KEY also holds whatever a file's key
# tag said ("Gm", "Amin") — so every notation is parsed (`core/musical_key`).
_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
# "YYYY", "YYYY-MM" or "YYYY-MM-DD", optionally followed by a time (a
# timestamp's date part is the date).
_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[T ].*)?$")


def parse_key(key: str | None) -> tuple[int | None, str | None]:
    """(Camelot wheel position 1-12, mode) for a Traktor key string."""
    return musical_key.parse(key)


def musical_key_value(wheel: int, mode: str) -> int:
    """`<MUSICAL_KEY VALUE>`: the tonic's pitch class (C = 0), plus 12 for minor
    — read off the real collection, where 12 pairs with "10m" (C minor) and 21
    with "1m" (A minor) on every entry carrying both."""
    return musical_key.pitch_class_of(wheel, mode) + (12 if mode == "minor" else 0)


def _from_musical_key(value: int | None) -> tuple[int | None, str | None]:
    if value is None or not 0 <= value <= 23:
        return None, None
    mode = "minor" if value >= 12 else "major"
    return musical_key.wheel_of(value % 12, mode), mode


def entry_key(e) -> tuple[str | None, int | None, str | None]:
    """(display string, wheel, mode) for an ENTRY.

    INFO@KEY is the text Traktor shows and writes to the file's tag; the
    analysed key itself is `<MUSICAL_KEY>`. 1,354 of the real collection's
    8,485 entries carry only the latter — analysed, with every value 0-23
    present, just never given a text — and Traktor shows a key for them, so
    they fall back to it, rendered in Open Key (Traktor's own default).
    """
    info = e.info
    text = info.key if info else None
    wheel, mode = parse_key(text)
    if wheel is None:
        mk = e.musical_key.value_attribute if e.musical_key is not None else None
        wheel, mode = _from_musical_key(mk)
        if wheel is not None and not text:
            text = musical_key.render(wheel, mode, "open_key")
    return text, wheel, mode


def iso_date(value: str | None) -> str | None:
    """Traktor writes dates as "YYYY/M/D"; the model carries ISO-8601."""
    if not value:
        return None
    m = _DATE.match(value.strip())
    if not m:
        return value
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def traktor_date(value: str | None) -> str | None:
    """The inverse of `iso_date`: an ISO-8601 date as Traktor writes it.

    Traktor's form is unpadded "YYYY/M/D" — every one of the 16,000-odd dates
    in the real collection. A year alone becomes "YYYY/1/1", as Traktor itself
    records a year-only release date (2,094 of 2,122 there). Anything that is
    neither ISO nor already Traktor's form passes through unchanged, as
    `iso_date` does in the other direction: losing a date is worse than
    carrying one Traktor may not parse.
    """
    if not value:
        return None
    s = value.strip()
    if m := _DATE.match(s):
        y, mo, d = m.groups()
        return f"{y}/{int(mo)}/{int(d)}"
    if m := _ISO_DATE.match(s):
        y, mo, d = m.groups()
        return f"{y}/{int(mo or 1)}/{int(d or 1)}"
    return value


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
    # The same "is this in the bank" test as `to_track_cues`' slot, so the
    # table's dots and the deck's pads cannot disagree.
    chips = sorted(
        (
            HotcueChip(
                slot=c.hotcue,
                type=NATIVE_TO_CUE_TYPE.get(c.type, "cue"),
                color=c.color or None,
            )
            for c in cues
            if c.hotcue is not None and c.hotcue >= 0
        ),
        key=lambda chip: chip.slot,
    )
    key_text, wheel, mode = entry_key(e)
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
        # Traktor's "Comment 2" is stored in INFO@RATING (stars are RANKING).
        comment2=info.rating if info else None,
        bpm=beatgrid.effective_bpm(e),
        key=key_text,
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
        hotcue_count=len(chips),
        hotcues=chips,
        grid_marker_count=len(markers),
        grid_locked=bool(e.lock),
        media_kind="stem" if getattr(e, "stems", None) is not None else "audio",
    )


def to_track_cues(entry, offset_ms: float = 0.0) -> TrackCues:
    """Project an ENTRY's beatgrid + cues.

    `offset_ms` is how far the entry's positions sit behind the decoded audio
    (`TraktorStore.time_offset_ms`, see `timebase`); positions come out in the
    decoded time base, unclamped.

    The beatgrid is the FULL ordered marker list — a constant grid is a list of
    length one. Grid markers themselves are not cues; their companion cues are,
    because they occupy real hotcue slots. Each is tagged with the marker it sits
    on, but is an ordinary, editable hotcue: Traktor 4 keeps a beatgrid without
    one, so nothing depends on it staying put.
    """
    markers = beatgrid.grid_markers(entry)
    comps = beatgrid.companions(entry)
    marker_of = {id(c): i for i, c in comps.items()}
    grid_markers = [
        GridMarker(
            start=timebase.from_traktor_ms(m.start or 0.0, offset_ms),  # START is ms
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
                start=timebase.from_traktor_ms(c.start or 0.0, offset_ms),
                length=(c.len or 0.0) / 1000.0,
                slot=slot,
                color=c.color,
                editable=True,
                readonly_reason=None,
                grid_marker=marker,
            )
        )
    return TrackCues(
        grid_markers=grid_markers, grid_locked=bool(entry.lock), cues=cues
    )
