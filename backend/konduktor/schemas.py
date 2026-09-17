"""API response models (Pydantic)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    commit: str | None = None  # sha of the version-history commit (None if deduped)
    playlists: int
    file_tags: list[FileTagOutcome] = []


class HistoryEntry(BaseModel):
    id: str  # commit sha
    timestamp: str  # ISO 8601, UTC
    summary: str  # human-readable edit summary


class AddGridMarker(BaseModel):
    track_id: str
    start: float  # seconds
    bpm: float | None = None  # unset = inherit the tempo governing that position


class GridMarkerEdit(BaseModel):
    track_id: str
    index: int  # into the start-ordered marker list
    start: float | None = None  # move it (clamped between its neighbours)
    bpm: float | None = None  # retempo the section it opens


class ReplaceGrid(BaseModel):
    track_id: str
    markers: list[GridMarker] = []  # [] is equivalent to deleting the grid


class SetLock(BaseModel):
    track_id: str
    locked: bool


class SetHotcue(BaseModel):
    track_id: str
    slot: int  # 0–7
    start: float  # seconds
    type: int  # 0 cue, 1 fade-in, 2 fade-out, 3 load, 5 loop
    length: float = 0.0  # seconds (>0 for a loop hotcue)
    name: str | None = None  # cue label; defaults to Traktor's "n.n." when unset


class AutoHotcuesRequest(BaseModel):
    track_id: str
    max_cues: int | None = None  # defaults to MAX_HOTCUES (8) server-side


class AutoGridRequest(BaseModel):
    track_id: str


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


class CollectionCandidate(BaseModel):
    path: str
    label: str  # folder name, e.g. "Traktor 4.5.0"
    version: str | None = None  # "4.5.0"
    modified: float | None = None  # collection.nml mtime, epoch seconds
    exists: bool = True


class CollectionOptions(BaseModel):
    auto: CollectionCandidate | None = None  # best auto-detected (latest version)
    recent: CollectionCandidate | None = None  # last opened (from userprefs)


class FsEntry(BaseModel):
    name: str
    path: str


class FsListing(BaseModel):
    path: str
    parent: str | None  # None when at filesystem root
    home: str
    dirs: list[FsEntry]
    files: list[FsEntry]  # .nml files only


# ---- path remapping ----
# `from` is a Python keyword, so the field is `from_` with a `from` alias
# (populate_by_name lets it be set either way; responses serialize as "from").


class PathMappingInfo(BaseModel):
    """A per-collection OS-path prefix remapping (blank prefixes = no mapping)."""

    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(default="", alias="from")
    to: str = ""


class RemapResult(BaseModel):
    rewritten: int  # tracks whose LOCATION was rewritten
    commit: str | None = None  # sha of the version-history commit for the rewrite


# The generic model lives in core.model; re-exported here so the HTTP layer has
# one import site. The dependency runs schemas -> core, never the reverse.
from .core.model import (  # noqa: E402,F401
    AutoHotcue,
    CuePoint,
    Facets,
    GenreCount,
    GridMarker,
    PlaylistNode,
    PrefixGroup,
    PrefixSuggestions,
    RemapPreview,
    RemapSample,
    Stats,
    Track,
    TrackCues,
    TrackPage,
)
