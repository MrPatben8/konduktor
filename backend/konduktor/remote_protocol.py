"""The wire contract between a Konduktor SERVER and the app that opens it.

A library held by a server (`konduktor.server`, a container on the user's NAS)
is opened on a laptop through `adapters/remote`'s `RemoteAdapter`, which speaks
THIS protocol to the server. Both sides import this module, so the method list,
which commands need the session, and the version they speak cannot drift apart.

The protocol is the `LibraryAdapter` protocol itself, not the UI's `/api`
routes: commands in, projections out. Every member of `LibraryAdapter` is
either in `RPC` or in `EXCLUDED` with the reason it does not travel —
`test_remote_protocol.py` fails the moment a new protocol member is neither, so
a command added later cannot silently be missing from remote libraries.

Arguments and results are (de)serialised with pydantic `TypeAdapter`s built
from the protocol's own type hints; the model classes are pydantic already and
the dataclasses convert. Bytes never travel as JSON — art, audio and uploads
have routes of their own.
"""
from __future__ import annotations

import base64
import inspect
import io
import typing
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .core.adapter import LibraryAdapter
from .core.model import AutoHotcue, Track, TrackCues

#: (major, minor). A client speaks to a server of the SAME major whose minor is
#: at least its own: minors only add. Bump the minor for an added route or
#: method, the major for anything an older client would misread.
API_VERSION: tuple[int, int] = (1, 0)

#: How long a session survives without a heartbeat, and how often the holder
#: sends one. Long enough to ride out a Wi-Fi blip or a switch to the fallback
#: address; short enough that moving to another computer after closing a lid
#: rarely needs a takeover (decided 2026-10-02).
LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 15.0

#: The header that carries the session token on every command.
SESSION_HEADER = "X-Konduktor-Session"


def compatible(client: tuple[int, int], server: tuple[int, int]) -> str | None:
    """None when `client` can talk to `server`; otherwise which side to update."""
    if client[0] != server[0]:
        return "app" if server[0] > client[0] else "server"
    if server[1] < client[1]:
        return "server"
    return None


@dataclass(frozen=True)
class Method:
    name: str
    #: Changes the library — needs the session token, bumps the revision.
    mutating: bool
    #: A property on the protocol rather than a method (`tracks`, `dirty`).
    prop: bool = False
    #: Parameter types where the protocol's hint is too loose to validate.
    overrides: tuple[tuple[str, Any], ...] = ()


_READ = (
    "capabilities", "track", "query_tracks", "facets", "stats", "playlist_tree",
    "playlist_count", "playlist_entries", "playlist_tracks", "track_cues", "audio_facts",
)
_MUTATING = (
    "create_playlist", "create_folder", "rename_playlist", "delete_playlist",
    "move_playlist", "set_playlist_entries", "remove_tracks", "set_track_metadata", "set_key", "set_cue",
    "set_cue_type", "set_cue_color", "delete_cue", "place_cues", "add_grid_marker",
    "move_grid_marker", "set_grid_marker_bpm", "delete_grid_marker", "replace_grid",
    "set_analysed_grid", "delete_grid", "set_grid_lock", "add_tracks",
)

RPC: dict[str, Method] = {
    **{n: Method(n, False) for n in _READ},
    "tracks": Method("tracks", False, prop=True),
    "dirty": Method("dirty", False, prop=True),
    **{n: Method(n, True) for n in _MUTATING},
}
RPC["place_cues"] = Method("place_cues", True, overrides=(("cues", list[AutoHotcue]),))

#: Protocol members that do not travel as RPC, and why.
EXCLUDED: dict[str, str] = {
    "reload": "POST /v1/discard — a discard on the server is what drops its edits",
    "save": "POST /v1/save — the server saves, versions and backs up beside the library",
    "snapshot": "version history lives on the server",
    "audio_path": "GET /v1/audio — a server path names no file on this computer",
    "cover_art": "GET /v1/art — bytes",
    "set_cover_art": "PUT /v1/art — bytes",
    "apply_stem_swaps": "the server applies stem swaps itself (POST /v1/stems/publish)",
    "set_path_mapping": "the server's path mapping is its own configuration",
    "set_session_mappings": "the missing-files check searches THIS computer's drives",
    "unresolved_path_groups": "the missing-files check searches THIS computer's drives",
    "path_prefix_suggestions": "paths.remappable is false for a remote library",
    "remap_preview": "paths.remappable is false for a remote library",
    "remap_locations": "paths.remappable is false for a remote library",
}


def protocol_members() -> set[str]:
    """Every method and property `LibraryAdapter` declares (not its attributes)."""
    out = set()
    for name, value in vars(LibraryAdapter).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(value) or isinstance(value, property):
            out.add(name)
    return out


def _callable(name: str):
    member = getattr(LibraryAdapter, name)
    return member.fget if isinstance(member, property) else member


@lru_cache(maxsize=None)
def _hints(name: str) -> dict[str, Any]:
    hints = typing.get_type_hints(_callable(name))
    method = RPC[name]
    hints.update(dict(method.overrides))
    return hints


@lru_cache(maxsize=None)
def _params(name: str) -> tuple[tuple[str, Any, Any], ...]:
    """(name, type, default) for each parameter the RPC takes."""
    if name == "query_tracks":
        # `**kw` on the protocol: the index's own signature is the real one.
        from .core.query import TrackIndex

        sig = inspect.signature(TrackIndex.query_tracks)
        hints = typing.get_type_hints(TrackIndex.query_tracks)
    else:
        sig = inspect.signature(_callable(name))
        hints = _hints(name)
    out = []
    for p in sig.parameters.values():
        if p.name == "self" or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        if name == "add_tracks" and p.name == "checkpoint":
            continue  # a callable; the server reports progress instead
        hint = hints.get(p.name, Any)
        if name == "add_tracks" and p.name == "items":
            hint = list[WireNewTrack]
        out.append((p.name, hint, p.default))
    return tuple(out)


@lru_cache(maxsize=None)
def _adapter(hint) -> TypeAdapter:
    return TypeAdapter(hint)


def dump_args(name: str, args: dict) -> dict:
    """Arguments → JSON-able, by the protocol's own types."""
    out = {}
    for pname, hint, _default in _params(name):
        if pname in args:
            out[pname] = _adapter(hint).dump_python(args[pname], mode="json")
    return out


def load_args(name: str, raw: dict) -> dict:
    """JSON → validated arguments. Unknown names are refused rather than
    ignored: a misspelt argument silently taking its default is a wrong edit."""
    params = {p: (h, d) for p, h, d in _params(name)}
    unknown = set(raw) - set(params)
    if unknown:
        raise ValueError(f"{name}: unknown argument(s) {sorted(unknown)}")
    out = {}
    for pname, (hint, default) in params.items():
        if pname in raw:
            out[pname] = _adapter(hint).validate_python(raw[pname])
        elif default is inspect.Parameter.empty:
            raise ValueError(f"{name}: missing argument {pname!r}")
    return out


def _result_hint(name: str):
    if name == "add_tracks":
        return list[str]
    return _hints(name).get("return", Any)


def dump_result(name: str, value) -> Any:
    return _adapter(_result_hint(name)).dump_python(value, mode="json")


def load_result(name: str, raw) -> Any:
    return _adapter(_result_hint(name)).validate_python(raw)


# ---- adding tracks -------------------------------------------------------------


class WireArt(BaseModel):
    data: str  # base64
    mime: str


class WireNewTrack(BaseModel):
    """`NewTrack` as it crosses the wire. `audio_path` is a path ON THE SERVER —
    the client uploaded the file first (`POST /v1/upload`), exactly as a local
    import copies it first. The analysis is the client's (the server never
    decodes audio)."""

    track: Track
    audio_path: str
    cues: TrackCues | None = None
    art: WireArt | None = None
    analysis: str | None = None  # base64 `.npz`, see `pack_analysis`


def pack_analysis(prepared) -> str | None:
    """A `PreparedAnalysis` → base64 `.npz` (float32: what the writers use)."""
    if prepared is None or prepared.measured is None:
        return None
    import numpy as np

    m = prepared.measured
    buf = io.BytesIO()
    markers = np.array([[g.start, g.bpm] for g in prepared.markers], dtype=np.float64).reshape(-1, 2)
    np.savez(
        buf,
        rms=np.asarray(m.columns.rms, dtype=np.float32),
        brightness=np.asarray(m.columns.brightness, dtype=np.float32),
        columns_duration=np.float64(m.columns.duration),
        bands=np.asarray(m.frames.bands, dtype=np.float32),
        frames_duration=np.float64(m.frames.duration),
        markers=markers,
        **({} if prepared.key is None else {
            "key": np.array([prepared.key.pitch_class, prepared.key.wheel,
                             prepared.key.mode == "minor", prepared.key.confidence], dtype=np.float64)}),
    )
    return base64.b64encode(buf.getvalue()).decode("ascii")


def unpack_analysis(text: str | None):
    """The inverse of `pack_analysis` → `PreparedAnalysis` (or None)."""
    if not text:
        return None
    import numpy as np

    from .core import waveform
    from .core.adapter import PreparedAnalysis
    from .core.model import GridMarker

    with np.load(io.BytesIO(base64.b64decode(text))) as z:
        measured = waveform.Analysis(
            columns=waveform.Columns(rms=z["rms"], brightness=z["brightness"],
                                     duration=float(z["columns_duration"])),
            frames=waveform.Frames(bands=z["bands"], duration=float(z["frames_duration"])),
        )
        markers = [GridMarker(start=float(s), bpm=float(b)) for s, b in z["markers"]]
        key = None
        if "key" in z:
            from .core.key_detect import KeyResult

            pitch, wheel, minor, confidence = z["key"]
            key = KeyResult(pitch_class=int(pitch), mode="minor" if minor else "major",
                            wheel=int(wheel), confidence=float(confidence))
    return PreparedAnalysis(measured=measured, markers=markers, key=key)
