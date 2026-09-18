"""What a loaded library can actually do.

Capabilities are ``f(platform, version)``: the UI reads them and disables or
hides the controls the loaded platform cannot persist, so a user is never
offered an edit that will silently vanish on save. No component branches on the
platform name — that is the whole point.

With a single adapter most of these are constants, and several keys exist purely
so the second adapter has somewhere to answer. That is deliberate: the shape is
cheap now and expensive to retrofit once components depend on it.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Why a library cannot be edited. A FACT, not a sentence — the UI composes the
# wording (lib/platformCopy.ts), and the two causes need very different ones:
# "Konduktor cannot write this platform YET" is a roadmap gap the user should
# expect to close, whereas "this library is synced with Rekordbox Cloud" is a
# permanent refusal protecting their other machines.
ReadonlyCause = Literal["platform_incomplete", "cloud_synced"]

CueType = Literal["cue", "fade_in", "fade_out", "load", "loop"]
CueRole = Literal["hotcue", "memory"]
MediaKind = Literal["audio", "stem", "video"]
PlaylistKind = Literal["folder", "playlist", "smart"]


class CueCapabilities(BaseModel):
    # Whether cues can be CREATED/CHANGED. Symmetric with GridCapabilities —
    # a platform can be writable overall while its cue store is not yet
    # implemented, which is exactly the Rekordbox milestone-2 state.
    editable: bool = False
    hotcue_slots: int = 8  # size of the addressable bank; 0 = no bank
    slot_labels: Literal["number", "letter"] = "number"
    # Memory cues are Rekordbox-only today, so they are modelled and PRESERVED
    # but not editable anywhere yet (the two-platform promotion rule).
    memory_cues: bool = False
    max_memory_cues: int | None = None
    types: list[CueType] = ["cue"]
    color: Literal["none", "free", "palette"] = "none"
    palette: list[str] = []
    named: bool = False
    # Where a saved loop lives: a cue type (Traktor) or its own bank (Serato).
    loops: Literal["none", "cue_type", "separate_bank"] = "none"
    loop_slots: int | None = None


class GridCapabilities(BaseModel):
    editable: bool = False
    flexible: bool = False  # multi-marker (variable-tempo) grids can be written
    lockable: bool = False  # a per-track grid/analysis lock exists


class TrackCapabilities(BaseModel):
    rating_max: int = 5
    editable_fields: list[str] = []
    media_kinds: list[MediaKind] = ["audio"]
    artwork: bool = False
    artwork_note: str | None = None


class PlaylistCapabilities(BaseModel):
    folders: bool = False
    smart: Literal["none", "read_only"] = "none"
    reorder: bool = False


class SaveCapabilities(BaseModel):
    """Structured facts, never finished sentences.

    The warning a user needs before saving is genuinely per-platform, so the
    adapter supplies the facts and the UI composes the wording — which keeps the
    copy (and any future translation) in the UI without making it vague.
    """

    app_name: str
    library_label: str
    overwrite_risk: Literal["none", "on_exit", "while_running"] = "none"
    history: bool = True


class Capabilities(BaseModel):
    platform: str
    version: str | None = None
    # Whether ANY edit can be persisted to this library.
    #
    # This is deliberately NOT the same as every individual capability being
    # false. "The platform has no such feature" and "this library cannot be
    # written at all" look identical to a UI that only sees the per-feature
    # flags, and the second one needs saying out loud — silently inert controls
    # read as a bug. When this is false the per-feature flags are still reported
    # honestly, so the UI can show what the platform *would* support.
    writable: bool = True
    readonly_cause: ReadonlyCause | None = None
    cues: CueCapabilities = CueCapabilities()
    grid: GridCapabilities = GridCapabilities()
    tracks: TrackCapabilities = TrackCapabilities()
    playlists: PlaylistCapabilities = PlaylistCapabilities()
    save: SaveCapabilities
