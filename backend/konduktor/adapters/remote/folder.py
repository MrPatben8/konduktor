"""The server's content folder, browsed from this computer like a drive.

The sidebar's Files trees read folders on THIS computer (`core/folder.py`). On a
remote library the music lives in the server's content folder, which this
computer cannot see — so the server lists it (`/v1/folder/tracks`: tags, file
facts, and which files the library already holds) and this module holds the
answer as a read-only SOURCE, the shape the table, the deck and the importer
already read.

Adding one of these files never uploads anything: it is already where the
library can reach it, so it is added IN PLACE (`on_library_host`), and the
importer skips both the copy and the copy-or-reference question.

Ids are the file's path ON THE SERVER — a string, never a `Path` on this side
until the importer needs one, because a server path is not a path here (on a
Windows laptop `Path("/music/x.mp3")` is `\\music\\x.mp3`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.adapter import AudioFacts
from ...core.model import PlaylistNode, Track, TrackCues


@dataclass
class ServerFile:
    folder: str
    track: Track
    facts: AudioFacts
    #: The library's id for this file, when it already points at it.
    held: str | None = None


class ServerFiles:
    """Files in the content folder, as the read half of a source."""

    #: The importer's cue that these are already on the machine holding the
    #: library: added where they are, neither copied nor uploaded.
    on_library_host = True

    def __init__(self, adapter, path: str, files: list[ServerFile]):
        self.adapter = adapter
        self.path = path
        self._files = {f.track.id: f for f in files}

    @classmethod
    def from_wire(cls, adapter, body: dict) -> "ServerFiles":
        folder = body["path"]
        facts = body.get("files") or {}
        held = body.get("held") or {}
        files = []
        for raw in body.get("tracks") or []:
            track = Track.model_validate(raw)
            fact = facts.get(track.id)
            if fact is None:
                continue
            files.append(ServerFile(
                folder=folder, track=track,
                facts=AudioFacts(key=track.id, size=int(fact["size"]), mtime_ns=int(fact["mtime_ns"])),
                held=held.get(track.id),
            ))
        return cls(adapter, folder, files)

    @property
    def files(self) -> dict[str, ServerFile]:
        return self._files

    @property
    def tracks(self) -> list[Track]:
        return [f.track for f in self._files.values()]

    def track(self, track_id: str) -> Track | None:
        found = self._files.get(track_id)
        return found.track if found else None

    def held(self, track_id: str) -> str | None:
        found = self._files.get(track_id)
        return found.held if found else None

    def size(self, track_id: str) -> int:
        found = self._files.get(track_id)
        return found.facts.size if found else 0

    def track_cues(self, track_id: str) -> TrackCues | None:
        return TrackCues(cues=[], grid_markers=[]) if track_id in self._files else None

    def audio_facts(self, track_ids) -> dict[str, AudioFacts]:
        return {t: self._files[t].facts for t in track_ids if t in self._files}

    def audio_path(self, track_id: str, *, progress=None) -> Path | None:
        """A local copy (downloaded into the shared cache, like a library track)."""
        if track_id not in self._files:
            return None
        return self.adapter.folder_audio(track_id, progress=progress)

    def playlist_tree(self) -> list[PlaylistNode]:
        return []

    def playlist_tracks(self, node_id: str) -> list[Track] | None:
        return None
