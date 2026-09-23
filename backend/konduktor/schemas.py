"""API response models (Pydantic)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .core.capabilities import CueRole, CueType


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


class SetGridLock(BaseModel):
    track_id: str
    locked: bool


class SetCue(BaseModel):
    track_id: str
    slot: int  # bank slot; capabilities.cues.hotcue_slots bounds it
    start: float  # seconds
    type: CueType = "cue"
    role: CueRole = "hotcue"
    length: float = 0.0  # seconds (>0 for a loop cue)
    name: str | None = None
    color: str | None = None


class AutoHotcuesRequest(BaseModel):
    track_id: str
    max_cues: int | None = None  # defaults to MAX_HOTCUES (8) server-side


class AutoGridRequest(BaseModel):
    track_id: str


class SetCueType(BaseModel):
    track_id: str
    slot: int
    type: CueType


class EditState(BaseModel):
    dirty: bool  # unsaved in-memory changes exist
    library: LibraryInfo


# ---- collection selection ----


class CollectionStatus(BaseModel):
    loaded: bool
    path: str | None = None
    tracks: int | None = None
    playlists: int | None = None
    # Identity of the loaded library, so the UI can name it without parsing the
    # path (a Serato library is a directory, not a file).
    library: LibraryInfo | None = None


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
    # EVERY library detected, best first. `auto` is simply the first of these.
    # Carried in full because one platform can genuinely have several at once —
    # two Traktor versions installed, or two sticks plugged in — and silently
    # anointing one of them is how the old single-candidate route could open a
    # USB drive as the user's collection.
    detected: list[CollectionCandidate] = []
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
    LibraryInfo,
    PlatformOption,
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


# ---- import sources ---------------------------------------------------
#
# A SOURCE is a library being read FROM — a plugged-in OneLibrary stick whose
# tracks are about to be imported. It is deliberately not a `CollectionStatus`:
# a source is never saved, never edited and never the fallback when nothing is
# loaded, so reusing the loaded-library shape would invite routes to treat the
# two as interchangeable.


class SourceCandidate(BaseModel):
    """A removable library Konduktor can currently see."""

    path: str
    label: str  # what a person recognises, e.g. "OneLibrary — Hardy"
    platform: str
    tracks: int | None = None  # None until it is opened; a probe would be slow
    modified: float | None = None


class SourceStatus(BaseModel):
    loaded: bool
    path: str | None = None
    label: str | None = None
    platform: str | None = None
    tracks: int | None = None
    playlists: int | None = None


class OpenSource(BaseModel):
    path: str


class ImportRequest(BaseModel):
    """What to import, and where its audio should land.

    Empty `track_ids` AND empty `playlist_ids` means the whole drive — an
    explicit "import everything" rather than a mistake, matching the settled
    export design's separate "export entire library" action.
    """

    destination: str  # folder the audio is copied into
    track_ids: list[str] = []
    playlist_ids: list[str] = []
    # Playlists land in a folder named after the drive. Null keeps them at the
    # root; the default is filled in from the source's label.
    folder_name: str | None = None


class JobStatus(BaseModel):
    id: str
    kind: str
    state: str  # running | done | failed | cancelled
    total: int
    done: int
    message: str
    result: dict | None = None
    error: str | None = None
    started_at: float
    finished_at: float | None = None
