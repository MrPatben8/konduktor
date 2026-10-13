"""A folder of loose audio files, read as if it were a (read-only) library.

The sidebar's Files trees browse any drive, and selecting a folder shows the
audio in it in the ordinary track table and deck. Projecting the folder into the
generic `Track` model is what makes that cheap: the table, the deck, the
read-only gating and the importer all already speak it, so none of them needs to
learn what a folder is.

Deliberately NOT a registered driver. `can_open()` on "any directory" would make
every folder a library to the picker, and the collection picker offering
`~/Downloads` as a library is exactly the confusion the registry exists to avoid.
This is opened by the folder routes and nothing else.

Three rules:

  * **This folder only.** Subfolders are not scanned — the tree is how you get to
    them — so a click on a drive root never walks the whole drive. A folder
    DROPPED onto the window is the exception (`FolderScanner.dropped`): the user
    asked for everything in it, so it recurses, capped at `MAX_DROPPED`.
  * **A track id is the file's absolute path.** It is the one identity a loose
    file has, and it is what the audio route checks a request against.
  * **Tags are read once per folder state.** Reading tags opens every file, which
    on a slow stick is the whole cost; the result is cached until the folder's
    own mtime changes (a file added, removed or renamed).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import Capabilities, SaveCapabilities, TrackCapabilities
from .model import PlaylistNode, Track, TrackCues
from .places import is_hidden
from .stem_file import is_stem_file

log = logging.getLogger(__name__)


def _first(tags, key: str) -> str | None:
    try:
        values = tags.get(key)
    except (KeyError, ValueError):
        return None
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


def _bpm(tags) -> float | None:
    raw = _first(tags, "bpm")
    if raw is None:
        return None
    try:
        bpm = float(raw)
    except ValueError:
        return None
    return bpm if 20 <= bpm <= 400 else None


def read_track(path: Path) -> Track:
    """Project one audio file. Never raises: an unreadable tag is an untitled track."""
    track = Track(id=str(path), title=path.stem, filepath=str(path),
                  media_kind="stem" if is_stem_file(path) else "audio")
    try:
        import mutagen

        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 — a corrupt file must not hide the folder
        log.debug("could not read tags from %s", path, exc_info=True)
        return track
    if audio is None:
        return track
    tags = audio.tags or {}
    track.title = _first(tags, "title") or path.stem
    track.artist = _first(tags, "artist")
    track.album = _first(tags, "album")
    track.genre = _first(tags, "genre")
    track.label = _first(tags, "organization")
    track.bpm = _bpm(tags)
    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        track.length = int(round(length)) if length else None
        bitrate = getattr(info, "bitrate", None)
        track.bitrate = int(bitrate) if bitrate else None
    # Key is left out on purpose: tagged keys arrive in every notation there is
    # ("Am", "8A", "1m", "A minor"), and passing one through to a library would
    # write a string its own analysis never would.
    return track


def audio_files(folder: Path, formats: list[str]) -> list[Path]:
    """The visible audio files directly inside `folder`, sorted by name."""
    wanted = {f.lower() for f in formats}
    out: list[Path] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file() or is_hidden(entry):
                        continue
                except OSError:
                    continue
                if Path(entry.name).suffix.lower() in wanted:
                    out.append(Path(entry.path))
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name.lower())


def capabilities_for(folder: Path) -> Capabilities:
    return Capabilities(
        platform="folder",
        writable=False,
        readonly_cause="not_in_library",
        tracks=TrackCapabilities(editable_fields=[], artwork=False, media_kinds=["audio", "stem"]),
        save=SaveCapabilities(
            app_name="Files",
            library_label=folder.name or str(folder),
            overwrite_risk="none",
            history=False,
        ),
    )


class FolderSource:
    """The read half of the adapter protocol, over one folder's audio files.

    Only what the table, the deck and the importer ask a source for. There are
    no commands at all: nothing here can be written, so nothing here pretends
    it could.
    """

    def __init__(self, folder: Path, tracks: list[Track]):
        self.path = folder
        self._tracks = tracks
        self._by_id = {t.id: t for t in tracks}

    @property
    def tracks(self) -> list[Track]:
        return self._tracks

    def track(self, track_id: str) -> Track | None:
        return self._by_id.get(track_id)

    def track_cues(self, track_id: str) -> TrackCues | None:
        return TrackCues(cues=[], grid_markers=[]) if track_id in self._by_id else None

    def audio_path(self, track_id: str) -> Path | None:
        return Path(track_id) if track_id in self._by_id else None

    def audio_facts(self, track_ids: list[str]):
        from .adapter import local_audio_facts

        return local_audio_facts(self, track_ids)

    def playlist_tree(self) -> list[PlaylistNode]:
        return []

    def playlist_tracks(self, node_id: str) -> list[Track] | None:
        return None

    def capabilities(self) -> Capabilities:
        return capabilities_for(self.path)


# A drop of a whole drive must not read tags for hours: past this many audio
# files the rest are left out and the answer says so.
MAX_DROPPED = 20_000


@dataclass
class Dropped:
    """What a drop onto the window resolved to."""

    tracks: list[Track] = field(default_factory=list)
    # Dropped FILES that are not audio this library can hold (or are gone).
    # Non-audio inside a dropped folder is not counted: an album folder's
    # cover and cue sheet are not something the user asked to add.
    skipped: int = 0
    # More than MAX_DROPPED audio files: the rest were left out.
    truncated: bool = False


class FolderScanner:
    """Scans folders on request, caching each until its contents change.

    Also the gatekeeper for streaming: `source_for_file` answers only for a file
    found by a scan — or dropped onto the window by the user — so the audio
    route cannot be used to read an arbitrary path off the disk, only audio the
    user has browsed to or handed over.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, tuple[str, ...], FolderSource]] = {}
        # Files dropped one by one: their folder was never scanned (and is not
        # cached as if it had been — a partial listing would hide its other
        # files from the sidebar), so each is held on its own.
        self._files: dict[str, Track] = {}

    def dropped(self, paths: list[str], formats: list[str], limit: int = MAX_DROPPED) -> Dropped:
        """Resolve paths dropped onto the window: folders RECURSIVELY (hidden
        and OS-housekeeping folders left out, as the tree leaves them out),
        files as themselves. Every audio file found becomes addable and
        playable through the folder routes, exactly as if it had been browsed."""
        wanted = {f.lower() for f in formats}
        out = Dropped()
        seen: set[str] = set()

        def take(tracks: list[Track]) -> bool:
            for t in tracks:
                if t.id in seen:
                    continue
                if len(out.tracks) >= limit:
                    out.truncated = True
                    return False
                seen.add(t.id)
                out.tracks.append(t)
            return True

        for raw in paths:
            path = Path(raw).expanduser()
            if path.is_dir():
                for folder in _walk(path):
                    if not audio_files(folder, formats):
                        continue
                    if not take(self.scan(folder, formats).tracks):
                        return out
            elif path.is_file() and path.suffix.lower() in wanted and not is_hidden(path):
                track = self._file_track(path)
                if not take([track]):
                    return out
            else:
                out.skipped += 1
        return out

    def _file_track(self, path: Path) -> Track:
        key = str(path)
        with self._lock:
            hit = self._cache.get(str(path.parent))
            known = hit[2].track(key) if hit is not None else None
        if known is not None:
            return known
        track = read_track(path)
        with self._lock:
            self._files[key] = track
        return track

    def scan(self, folder: Path, formats: list[str]) -> FolderSource:
        folder = Path(folder)
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            mtime = -1.0
        key = str(folder)
        fmts = tuple(sorted(f.lower() for f in formats))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == mtime and hit[1] == fmts:
                return hit[2]
        source = FolderSource(folder, [read_track(p) for p in audio_files(folder, formats)])
        with self._lock:
            self._cache[key] = (mtime, fmts, source)
        return source

    def source_for_file(self, track_id: str) -> FolderSource | None:
        folder = str(Path(track_id).parent)
        with self._lock:
            hit = self._cache.get(folder)
            loose = self._files.get(track_id)
        if hit is not None and hit[2].track(track_id) is not None:
            return hit[2]
        if loose is not None:
            return FolderSource(Path(folder), [loose])
        return None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._files.clear()


def _walk(root: Path):
    """`root` and every visible folder under it, depth first, sorted by name."""
    yield root
    try:
        with os.scandir(root) as it:
            subs = []
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not is_hidden(entry):
                        subs.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return
    for sub in sorted(subs, key=lambda p: p.name.lower()):
        yield from _walk(sub)
