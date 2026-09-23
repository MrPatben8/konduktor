"""Writing a BRAND NEW library for another platform.

The contract an exporter is written against, and the payload it consumes. Both
are strictly generic: an exporter is handed `Track`s, `TrackCues` and playlist
names, never a native type from whatever library the user happens to have open.
That is what makes "the source is whatever is loaded" free — a Rekordbox
collection exports to Traktor with no exporter knowing Rekordbox exists.

## Why this is not `LibraryDriver`

A driver OPENS a library that already exists; an exporter CREATES one where
nothing does. Keeping them apart keeps the edit path — the one carrying the
byte-fidelity guarantees — out of reach of a lossy-by-design export.

An exporter also declares **static** capabilities. It has no instance and no
path, because the library it describes does not exist yet, which is exactly the
question "what can the target represent?" needs answering before any of it is
written.

## Who does what

`core` plans and copies; the exporter only writes the library file. Layout,
collision and dedup rules are platform-independent and must not drift between
targets — and the exporter could not write a Traktor `<LOCATION>` before the
destination path is known anyway, so every `ExportTrack` arrives carrying its
FINAL audio path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .capabilities import Capabilities
from .model import Track, TrackCues


@dataclass
class ExportTrack:
    """One track, with its prep, and where its audio now lives."""

    track: Track
    #: Where the copy ALREADY IS. The exporter never copies; by the time it runs
    #: the file is on disk, because a half-copied file is not something a library
    #: write should be discovering.
    destination: Path
    cues: TrackCues | None = None

    @property
    def source_id(self) -> str:
        """Its id in the SOURCE library, for joining playlists to tracks."""
        return self.track.id


@dataclass
class ExportPlaylist:
    """A playlist to recreate in the exported library.

    `folders` is the path of folder names to nest it under, outermost first, so
    a target that supports folders can preserve the user's tree and one that
    cannot may flatten the same information into a name.
    """

    name: str
    track_ids: list[str] = field(default_factory=list)   # SOURCE ids
    folders: list[str] = field(default_factory=list)


@dataclass
class ExportPayload:
    """Everything an exporter needs, and nothing platform-specific."""

    tracks: list[ExportTrack] = field(default_factory=list)
    playlists: list[ExportPlaylist] = field(default_factory=list)
    #: The export's name, used for the folder its playlists are nested under.
    name: str = "Export"


@runtime_checkable
class LibraryExporter(Protocol):
    """Writes one platform's library, from nothing. Stateless."""

    platform: str
    #: What the written library file is called, e.g. "collection.nml".
    library_filename: str

    def capabilities(self) -> Capabilities:
        """What this target can represent — STATIC, with no library to read."""
        ...

    def write(self, payload: ExportPayload, destination: Path) -> Path:
        """Write a complete library into `destination`; return the file written.

        The destination exists and already holds the copied audio. Raises rather
        than writing something partial: a library file is the last thing an
        export produces, so its absence is what marks an export unfinished.
        """
        ...


_EXPORTERS: list[LibraryExporter] = []


def register(exporter: LibraryExporter) -> None:
    """Register a target. Called once per adapter package, at import."""
    if not any(e.platform == exporter.platform for e in _EXPORTERS):
        _EXPORTERS.append(exporter)


def exporters() -> list[LibraryExporter]:
    return list(_EXPORTERS)


def for_platform(platform: str) -> LibraryExporter | None:
    """The exporter for a platform, or None when it cannot be a target.

    Deliberately narrower than `registry.driver_for`: reading a library and
    creating one from nothing are different capabilities, and a platform can
    have the first without the second.
    """
    return next((e for e in _EXPORTERS if e.platform == platform), None)


def targets() -> set[str]:
    return {e.platform for e in _EXPORTERS}
