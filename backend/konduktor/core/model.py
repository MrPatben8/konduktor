"""The generic model — Konduktor's platform-independent read projection.

Every adapter projects its native library into these types, and the HTTP layer
serves them directly. Nothing here may reference a platform's own encodings: a
value that only makes sense for one format belongs in that adapter, not here.

`AutoHotcue` is the one command-input type that lives alongside them, because it
is the shape a batch cue placement takes in generic terms.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import CueRole, CueType, MediaKind, PlaylistKind


class Track(BaseModel):
    """A single track in the library, as the app sees it."""

    id: str  # the adapter's stable track key; opaque above the adapter
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
    # The platform's own display string, shown verbatim ("10m", "8A", "Am").
    key: str | None = None
    # Camelot wheel position 1-12 + mode, parsed BY THE ADAPTER — notation is
    # platform knowledge. None when the key is absent or unparseable.
    key_wheel: int | None = None
    key_mode: Literal["major", "minor"] | None = None
    rating: int = 0  # 0-5 stars (derived from RANKING/51)
    playcount: int | None = None
    length: int | None = None  # seconds
    bitrate: int | None = None
    # ISO-8601 "YYYY-MM-DD", normalised by the adapter.
    import_date: str | None = None
    last_played: str | None = None
    release_date: str | None = None
    filepath: str | None = None  # human-readable OS path
    cue_count: int = 0
    hotcue_count: int = 0
    # 0 = no beatgrid, 1 = constant tempo, >1 = a flexible (multi-tempo) grid.
    grid_marker_count: int = 0
    grid_locked: bool = False
    media_kind: MediaKind = "audio"


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
    """A node in the playlist tree.

    `id` is OPAQUE: the adapter mints it and decodes it, and the UI never
    constructs, parses or prefixes one. `kind` is for choosing an icon — every
    behavioural question is answered by the flags, so the UI never has to infer
    what a node can do from what it is called.
    """

    id: str
    name: str
    kind: PlaylistKind = "playlist"
    count: int = 0  # track count (for playlists)
    children: list["PlaylistNode"] = []

    selectable: bool = False  # has a static, listable entry set
    can_add_tracks: bool = False
    can_reorder: bool = False
    can_rename: bool = False
    can_delete: bool = False
    can_contain_children: bool = False  # a valid parent for a new playlist


PlaylistNode.model_rebuild()


# ---- write request bodies ----


class CuePoint(BaseModel):
    name: str | None = None
    type: CueType = "cue"
    # "memory" cues have no bank slot. Only Rekordbox has them, so they are
    # modelled and preserved but not yet editable anywhere.
    role: CueRole = "hotcue"
    start: float  # seconds
    length: float  # seconds (>0 for loops)
    slot: int | None = None  # bank slot; None for a cue not in a bank
    color: str | None = None  # "#RRGGBB" as the platform stored it
    # False when the adapter refuses commands on this cue — the UI gates on THIS
    # rather than on any platform-specific reason.
    editable: bool = True
    readonly_reason: Literal["beatgrid_companion", "platform_managed"] | None = None
    # Index of the grid marker this cue mirrors, where the platform pairs them.
    # A display hint only; None on platforms that do not.
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
    grid_locked: bool = False
    cues: list[CuePoint] = []  # cue/loop markers (grid markers themselves excluded)


class AutoHotcue(BaseModel):
    """One cue in a batch placement (Auto Hotcues)."""

    slot: int
    start: float  # seconds
    name: str | None = None  # positional label (e.g. "Drop")
    type: CueType = "cue"
    length: float = 0.0  # seconds (>0 for a loop cue)


class LibraryInfo(BaseModel):
    """Identity of the loaded library, for display and for composing copy."""

    platform: str
    name: str  # the DJ app's name, e.g. "Traktor"
    library_label: str  # what the user thinks of the artefact as, e.g. "collection.nml"
    path: str
    display_name: str  # short label for the status bar
    version: str | None = None


class PlatformOption(BaseModel):
    """A platform the picker can offer before any library is open."""

    platform: str
    name: str
    library_label: str
    selects: Literal["file", "directory"] = "file"
    installed: bool = False


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
