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
# `not_in_library` is a folder of loose files being browsed: nothing there is a
# library to write to, and the way to edit one is to add it to the collection.
# `browsing` is an editable library opened only to READ alongside the loaded one
# (a OneLibrary stick in the sidebar's Devices): the way to edit it is to open it
# as THE library, which one SaveBar and one dirty state can then follow.
# `taken_over` is a library held by a server that another computer has taken
# over; `offline` one whose server cannot be reached. Both are temporary, and
# both mean "nothing you do here can be saved" until the session is back.
ReadonlyCause = Literal[
    "platform_incomplete", "cloud_synced", "not_in_library", "browsing", "taken_over", "offline",
]

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
    # Tracks can be removed from the library (and so from its playlists). The
    # audio file is never touched — except where `places_audio`: a library that
    # owns where its audio lives (a stick) deletes it with the track, at Save.
    removable: bool = False
    # Tracks can be added to the library (`add_tracks`): an import from a
    # device, or browsed files added to the collection.
    addable: bool = False
    # The LIBRARY decides where added audio goes (`place_audio`), so the user is
    # neither asked "copy or leave in place?" nor for a destination: a OneLibrary
    # stick can only point at files on itself, and lays them out as rekordbox
    # does. False: the user chooses, as for a desktop collection.
    places_audio: bool = False
    # Tracks can be converted to native-instruments STEM files, which this
    # platform plays as stems (`apply_stem_swaps`).
    stem_convertible: bool = False
    # The musical key can be set (`set_key`) — what key analysis writes. Not
    # in `editable_fields`: a key is a wheel position, not free text.
    key_writable: bool = False
    # The audio files this library can hold, as lower-case suffixes. What a
    # browsed folder offers to add — a file the library would refuse, or that
    # the DJ app cannot play, is not worth listing as addable.
    audio_formats: list[str] = [".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a"]
    artwork: bool = False
    artwork_note: str | None = None
    # Whose filesystem an import destination is on. "host": this computer's,
    # browsed with the ordinary file browser. "library": the machine that holds
    # the library (a server), whose folders are listed through the library —
    # added audio is UPLOADED there.
    audio_destination: Literal["host", "library"] = "host"
    # Whether added files can be left where they are ("reference") rather than
    # copied. False where the library cannot see this computer's files at all.
    reference: bool = True


class PlaylistCapabilities(BaseModel):
    folders: bool = False
    smart: Literal["none", "read_only"] = "none"
    reorder: bool = False
    # A playlist can hold the same track more than once. Where it cannot, a
    # drop of tracks already there skips them rather than asking.
    duplicates: bool = False


class PathCapabilities(BaseModel):
    # Path mapping, path rewriting and the open-time missing-files check apply.
    # False for a library held by a server: its mapping is the server's own
    # configuration, and this computer's drives are not where its files are.
    remappable: bool = True


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
    # The library was written by one of Konduktor's EXPORT sets, whose next run
    # replaces it — so an edit made here directly is undone by the next export.
    managed_by_export: bool = False


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
    paths: PathCapabilities = PathCapabilities()
    save: SaveCapabilities
