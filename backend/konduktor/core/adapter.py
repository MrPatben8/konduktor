"""The adapter contract: what the app needs from a DJ library, generically.

Konduktor's save model is **projection + command replay**. The generic model is
a READ PROJECTION; edits are commands an adapter replays onto its RETAINED
NATIVE MODEL, which stays the write target. The generic model is never
serialized back over a library — that is what preserves byte-exact saves, and
it is also the only model that works for formats you cannot regenerate at all
(a Rekordbox SQLite row, a Serato tag inside an audio file).

Two rules keep this honest:

  * **No native type appears in any signature here.** If a concept cannot be
    expressed generically it does not belong on the protocol.
  * **Every mutating command returns its refreshed projection.** That makes the
    refresh an adapter-internal invariant rather than something each route has
    to remember; a forgotten refresh is a silently stale UI that no byte-level
    test would catch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .capabilities import Capabilities
from .model import Facets, PlaylistNode, Stats, Track, TrackCues, TrackPage
from .pathmap import PathMapping


@dataclass
class SaveOutcome:
    """What a save produced, in terms every platform can answer.

    Promoted here once a second adapter needed it (the two-platform rule). The
    fields are deliberately optional because platforms differ in kind, not just
    in detail:

      * `snapshot` is the exact bytes written, for the version-history commit —
        and it is None on a platform Konduktor does not version. A Rekordbox
        library is `master.db` plus ~200 analysis files plus a playlist XML, so
        there is no single blob that *is* the library, and restoring the database
        alone would roll back the app's own auth and sampler state.
      * `tag_results` reports embedded audio-file tag writes, which only a
        platform that does them will populate.
    """

    summary: str  # human-readable edit summary, for the version-history message
    snapshot: bytes | None = None
    tag_results: list = field(default_factory=list)


@dataclass
class NewTrack:
    """A track to ADD to a library, described generically.

    Import is the first operation that puts a track into a library that never
    had one, so it needs a way to say "here is a track" without naming a
    platform. That is all this is: the generic projection of a track, plus the
    audio file it should point at.

      * `track` carries the metadata. **Its `id` is ignored** — an id belongs to
        the library that holds the track, and this one comes from somewhere
        else. The receiving adapter assigns its own and returns it.
      * `cues` is optional because a track can be imported without prep, and
        because a platform may hold the track but not (yet) its cues.
      * `audio_path` is a path on THIS machine that must already exist. Copying
        the audio is the caller's job, deliberately: an adapter's business is
        the library, and a file copy that failed halfway is not something a
        library write should be discovering.
    """

    track: Track
    audio_path: Path
    cues: TrackCues | None = None


class AdapterError(Exception):
    """Base for every failure an adapter reports to the HTTP layer."""


class LibraryNotSupported(AdapterError):
    """No driver can open this path, or it failed to parse."""


class NotFound(AdapterError):
    """Unknown track, playlist or marker index."""


class InvalidCommand(AdapterError):
    """Well-formed request the library cannot accept (bad slot, BPM <= 0, ...)."""


class Unsupported(AdapterError):
    """The platform cannot represent this edit at all."""


@runtime_checkable
class LibraryAdapter(Protocol):
    """One open library. Stateful; exactly one is loaded at a time."""

    platform: str
    path: Path

    # ---- identity -------------------------------------------------------
    def capabilities(self) -> Capabilities: ...
    def reload(self) -> None: ...

    # ---- read projection -------------------------------------------------
    @property
    def tracks(self) -> list[Track]: ...
    def track(self, track_id: str) -> Track | None: ...
    def query_tracks(self, **kw) -> TrackPage: ...
    def facets(self) -> Facets: ...
    def stats(self, playlist_count: int) -> Stats: ...
    def playlist_tree(self) -> list[PlaylistNode]: ...
    def playlist_entries(self, node_id: str) -> list[str] | None: ...
    def playlist_tracks(self, node_id: str) -> list[Track] | None: ...
    def track_cues(self, track_id: str) -> TrackCues | None: ...

    # ---- commands: playlists --------------------------------------------
    def create_playlist(self, name: str, parent_id: str | None = None) -> str: ...
    # Folders are a separate verb rather than a flag on create_playlist: a
    # platform can support one and not the other, and `capabilities.playlists
    # .folders` is what the UI gates on.
    def create_folder(self, name: str, parent_id: str | None = None) -> str: ...
    def rename_playlist(self, node_id: str, name: str) -> None: ...
    def delete_playlist(self, node_id: str) -> None: ...
    def set_playlist_entries(self, node_id: str, track_ids: list[str]) -> int: ...

    # ---- commands: adding tracks ----------------------------------------
    # The ONLY command that creates a track rather than editing one, and the
    # one place the "retained native model" rule is stretched: everywhere else
    # the write target was parsed from a real file, which is what makes the
    # fidelity guarantee hold. A new entry has no parsed original, so the rule
    # becomes "an added entry must not perturb any existing one" — which is
    # what `test_save_fidelity.py` checks rather than taking on trust.
    def add_tracks(self, items: list["NewTrack"]) -> list[str]: ...

    # Remove tracks from the library — and so from every playlist, since an
    # entry naming a track the library no longer has is a dangling reference.
    # Never touches audio files. Returns how many were removed; gated on
    # `capabilities().tracks.removable`.
    def remove_tracks(self, track_ids: list[str]) -> int: ...

    # ---- commands: track metadata / art ---------------------------------
    def set_track_metadata(self, track_id: str, fields: dict) -> Track | None: ...
    def set_cover_art(self, track_id: str, data: bytes, mime: str) -> None: ...
    def cover_art(self, track_id: str) -> tuple[bytes, str] | None: ...

    # ---- commands: cues --------------------------------------------------
    def set_cue(
        self,
        track_id: str,
        *,
        slot: int,
        start_sec: float,
        cue_type: str,
        length_sec: float = 0.0,
        role: str = "hotcue",
        name: str | None = None,
    ) -> TrackCues: ...
    def set_cue_type(self, track_id: str, slot: int, cue_type: str) -> TrackCues: ...
    def delete_cue(self, track_id: str, slot: int) -> TrackCues: ...
    def place_cues(self, track_id: str, cues: list, *, overwrite: bool = False) -> TrackCues: ...

    # ---- commands: beatgrid ---------------------------------------------
    def add_grid_marker(self, track_id: str, start_sec: float, bpm: float | None = None) -> TrackCues: ...
    def move_grid_marker(self, track_id: str, index: int, start_sec: float) -> TrackCues: ...
    def set_grid_marker_bpm(self, track_id: str, index: int, bpm: float) -> TrackCues: ...
    def delete_grid_marker(self, track_id: str, index: int) -> TrackCues: ...
    def replace_grid(self, track_id: str, markers: list[tuple[float, float]]) -> TrackCues: ...
    def set_analysed_grid(self, track_id: str, markers: list[tuple[float, float]]) -> TrackCues: ...
    def delete_grid(self, track_id: str) -> TrackCues: ...
    def set_grid_lock(self, track_id: str, locked: bool) -> TrackCues: ...

    # ---- audio / paths ---------------------------------------------------
    def audio_path(self, track_id: str) -> Path | None: ...
    def set_path_mapping(self, mapping: PathMapping) -> None: ...
    def path_prefix_suggestions(self) -> dict: ...
    def remap_preview(self, mapping: PathMapping) -> dict: ...
    # {old track id: new track id}; ids may change because a Traktor id IS its path.
    def remap_locations(self, mapping: PathMapping) -> dict[str, str]: ...

    # ---- save -------------------------------------------------------------
    @property
    def dirty(self) -> bool: ...
    def save(self): ...
    def snapshot(self) -> bytes: ...


@runtime_checkable
class LibraryDriver(Protocol):
    """Opens libraries of one platform. Stateless; one instance per platform."""

    platform: str
    display_name: str
    suffixes: tuple[str, ...]
    # True when this platform's libraries live on plugged-in media rather than at
    # a fixed path — so `detect()` genuinely changes between calls, and the app
    # can offer them as import SOURCES. Optional, and False for a platform that
    # does not set it, because most keep one library in a known place.
    removable: bool
    # Whether a user picks a FILE or a DIRECTORY for this platform. Both exist:
    # Traktor's library is `collection.nml`, OneLibrary's is the drive, and
    # Serato's will be a `_Serato_` folder. The file browser and the open route
    # both read this rather than assuming everything is a file. Defaults to
    # "file" for a driver that does not set it.
    selects: str

    def can_open(self, path: Path) -> bool: ...
    def open(self, path: Path) -> LibraryAdapter: ...
    # OPTIONAL. What to call an opened library on screen. Only the adapter can
    # answer for a platform where one library has more than one valid path —
    # the filename is otherwise assumed, and that assumption is wrong for a
    # library that is a drive. Omitted means "the path's own name".
    def display_name_for(self, path: Path) -> str: ...
    def detect(self) -> list: ...
    def describe(self, path: Path) -> object: ...
