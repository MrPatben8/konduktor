"""API response models (Pydantic)."""
from __future__ import annotations

from pydantic import BaseModel


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


class CreatePlaylist(BaseModel):
    name: str
    parent_id: str | None = None  # folder id ("fld:..."); None = root


class RenamePlaylist(BaseModel):
    name: str


class SetEntries(BaseModel):
    track_ids: list[str]  # ordered; full desired contents of the playlist


class EditTrack(BaseModel):
    track_id: str
    fields: dict[str, str | int | None]


class FileTagOutcome(BaseModel):
    track_id: str
    file: str
    ok: bool
    status: str
    detail: str = ""


class SaveResult(BaseModel):
    saved: bool
    backup: str | None = None
    playlists: int
    file_tags: list[FileTagOutcome] = []


class CuePoint(BaseModel):
    name: str | None = None
    type: int  # Traktor: 0 cue, 1 fade-in, 2 fade-out, 3 load, 4 grid, 5 loop
    start: float  # seconds
    length: float  # seconds (>0 for loops)
    hotcue: int  # -1 if not assigned to a hotcue slot
    color: str | None = None  # "#RRGGBB" if set


class TrackCues(BaseModel):
    bpm: float | None = None  # beatgrid BPM (from the grid marker; falls back to tempo)
    grid_anchor: float | None = None  # seconds — first grid marker (beat 1 of the grid)
    cues: list[CuePoint] = []  # cue/loop markers (grid markers excluded)


class SetHotcue(BaseModel):
    track_id: str
    slot: int  # 0–7
    start: float  # seconds
    type: int  # 0 cue, 1 fade-in, 2 fade-out, 3 load, 5 loop
    length: float = 0.0  # seconds (>0 for a loop hotcue)


class SetHotcueType(BaseModel):
    track_id: str
    slot: int
    type: int


class EditState(BaseModel):
    dirty: bool  # unsaved in-memory playlist changes exist
    nml_path: str


# ---- collection selection ----


class CollectionStatus(BaseModel):
    loaded: bool
    path: str | None = None
    tracks: int | None = None
    playlists: int | None = None


class OpenCollection(BaseModel):
    path: str


class FsEntry(BaseModel):
    name: str
    path: str


class FsListing(BaseModel):
    path: str
    parent: str | None  # None when at filesystem root
    home: str
    dirs: list[FsEntry]
    files: list[FsEntry]  # .nml files only
