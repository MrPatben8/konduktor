"""The generic model — Konduktor's platform-independent read projection.

Every adapter projects its native library into these types, and the HTTP layer
serves them directly. Nothing here may reference a platform's own encodings: a
value that only makes sense for one format belongs in that adapter, not here.

`AutoHotcue` is the one command-input type that lives alongside them, because it
is the shape a batch cue placement takes in generic terms.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Track(BaseModel):
    """A single track, flattened from a Traktor collection ENTRY."""

    id: str  # primary key: "<VOLUME><DIR><FILE>", used to join playlist entries
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    genre: str | None = None
    label: str | None = None
    remixer: str | None = None
    producer: str | None = None
    mix: str | None = None
    comment: str | None = None
    bpm: float | None = None
    key: str | None = None  # Traktor display key, e.g. "10m"
    rating: int = 0  # 0-5 stars (derived from RANKING/51)
    playcount: int | None = None
    length: int | None = None  # seconds
    bitrate: int | None = None
    import_date: str | None = None
    last_played: str | None = None
    release_date: str | None = None
    filepath: str | None = None  # human-readable OS path
    cue_count: int = 0
    hotcue_count: int = 0
    has_grid: bool = False
    grid_markers: int = 0  # >1 = a flexible (multi-tempo) beatgrid
    is_stem: bool = False  # a Stem file (<STEMS> child) vs a normal audio track


class TrackPage(BaseModel):
    total: int  # total matching the filter (before pagination)
    offset: int
    limit: int
    items: list[Track]


class GenreCount(BaseModel):
    name: str
    count: int


class Facets(BaseModel):
    """Distinct values available for building filter UI."""

    genres: list[GenreCount]
    keys: list[GenreCount]
    bpm_min: float | None
    bpm_max: float | None
    total_tracks: int


class Stats(BaseModel):
    total_tracks: int
    total_playlists: int
    rated: int
    unrated: int
    missing_key: int
    missing_genre: int
    missing_bpm: int
    no_cues: int
    rating_breakdown: dict[int, int]  # stars -> count
    bpm_histogram: list[dict]  # [{bucket: "120-130", count: n}]
    top_genres: list[GenreCount]


class PlaylistNode(BaseModel):
    """A node in the playlist tree — either a FOLDER or a PLAYLIST/SMARTLIST."""

    id: str  # stable synthetic id ("pl-<n>" / "fld-<n>")
    name: str
    type: str  # "FOLDER" | "PLAYLIST" | "SMARTLIST"
    uuid: str | None = None
    count: int = 0  # track count (for playlists)
    children: list["PlaylistNode"] = []


PlaylistNode.model_rebuild()


# ---- write request bodies ----


class CuePoint(BaseModel):
    name: str | None = None
    type: int  # Traktor: 0 cue, 1 fade-in, 2 fade-out, 3 load, 4 grid, 5 loop
    start: float  # seconds
    length: float  # seconds (>0 for loops)
    hotcue: int  # -1 if not assigned to a hotcue slot
    color: str | None = None  # "#RRGGBB" if set
    # Index of the grid marker this cue is the companion of, if any. Such a cue
    # holds a real hotcue slot but belongs to the beatgrid and is not editable.
    grid_marker: int | None = None


class GridMarker(BaseModel):
    """One beatgrid marker. Its position in the list IS its index/identity."""

    start: float  # seconds
    bpm: float  # governs from this marker until the next one
    name: str | None = None  # Traktor's label — display only, NOT a discriminator
    companion: int | None = None  # hotcue slot of the paired white cue, if any


class TrackCues(BaseModel):
    # THE beatgrid, ordered by start. Empty = no grid; one marker = constant
    # tempo. There is deliberately no scalar bpm/anchor here: a single "the BPM"
    # is what made flexible grids render and edit wrongly.
    grid_markers: list[GridMarker] = []
    locked: bool = False  # Traktor LOCK flag
    cues: list[CuePoint] = []  # cue/loop markers (grid markers themselves excluded)


class AutoHotcue(BaseModel):
    slot: int  # 0–7
    start: float  # seconds
    name: str | None = None  # positional label (e.g. "Drop")
    type: int = 0  # plain cue
    length: float = 0.0  # seconds (>0 for a loop hotcue)


class RemapSample(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    exists: bool


class RemapPreview(BaseModel):
    total: int  # tracks with a resolvable location
    matched: int  # how many match the `from` prefix
    existing: int  # of matched, how many exist at the `to` target
    samples: list[RemapSample] = []


class PrefixGroup(BaseModel):
    prefix: str  # common directory prefix of a group of tracks
    count: int  # tracks sharing it


class PrefixSuggestions(BaseModel):
    primary: str  # best guess for the `from` prefix (largest group)
    groups: list[PrefixGroup] = []  # alternatives, ranked by count
