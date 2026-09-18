"""What Traktor can persist.

Derived from the NML version where one is declared, which is the accurate way to
report capabilities rather than assuming a floor that denies users features their
install actually has. NML v19 (Traktor 3) and v20 (Traktor 4) are equivalent for
everything Konduktor edits; flexible beatgrids arrived in Traktor 3.4, which is
older than any NML version we can open, so the flag is unconditional.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...core.capabilities import (
    Capabilities,
    CueCapabilities,
    GridCapabilities,
    PlaylistCapabilities,
    SaveCapabilities,
    TrackCapabilities,
)

_VERSION_RE = re.compile(rb'<NML\s+VERSION="(\d+)"')

# Traktor's cue vocabulary, in generic terms. The integer encoding lives in
# projection.py / store.py and never leaves this package.
CUE_TYPES = ["cue", "fade_in", "fade_out", "load", "loop"]


def nml_version(path: Path) -> str | None:
    """The NML VERSION attribute, read from the file head without parsing it."""
    try:
        with path.open("rb") as fh:
            m = _VERSION_RE.search(fh.read(4096))
        return m.group(1).decode() if m else None
    except OSError:
        return None


def capabilities_for(path: Path, editable_fields: list[str]) -> Capabilities:
    return Capabilities(
        platform="traktor",
        version=nml_version(path),
        writable=True,
        cues=CueCapabilities(
            hotcue_slots=8,
            slot_labels="number",
            # Traktor has no memory cues — every cue occupies a bank slot.
            memory_cues=False,
            types=CUE_TYPES,
            color="free",
            named=True,
            # A saved loop is a cue TYPE in Traktor, not a separate bank.
            loops="cue_type",
        ),
        grid=GridCapabilities(editable=True, flexible=True, lockable=True),
        tracks=TrackCapabilities(
            rating_max=5,
            editable_fields=sorted(editable_fields),
            media_kinds=["audio", "stem"],
            artwork=True,
            artwork_note=(
                "Traktor caches its own cover thumbnail, so a replaced image may "
                "need a manual Import Cover Art to show up."
            ),
        ),
        playlists=PlaylistCapabilities(folders=True, smart="read_only", reorder=True),
        save=SaveCapabilities(
            app_name="Traktor",
            library_label="collection.nml",
            # Traktor rewrites collection.nml when it quits, so an edit made
            # while it is running is lost on exit.
            overwrite_risk="on_exit",
            history=True,
        ),
    )
