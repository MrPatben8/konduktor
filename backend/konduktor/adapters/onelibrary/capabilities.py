"""What a OneLibrary drive can persist: nothing, for now.

`writable` is False with cause `platform_incomplete`. That is the distinction the
capability system exists to draw — this is a ROADMAP GAP, not a refusal. A
cloud-synced Rekordbox library is refused permanently to protect the user's other
machines; a OneLibrary drive simply has no write path yet, and the UI must word
those two very differently.

The per-feature flags below still report what the FORMAT supports rather than
being blanked to false, because that is what `writable=False` is for: the UI can
show a drive's cues and grid honestly, and say once, in one place, that the whole
library is read-only. Blanking them would claim OneLibrary has no hot cues.

Two answers are worth recording now, while the evidence is in front of us, since
getting them wrong later would be a silent data bug rather than a missing
feature:

  * cue colour is **free RGB**, not a palette index. The `PCO2` tag stores three
    bytes per cue, and the reference drive held #4D00FF, #33FF00, #00C4FF and
    #FF8C00. This differs from the desktop Rekordbox adapter, which reports
    "palette" because `djmdCue` stores an index into a built-in table — the same
    vendor, two genuinely different representations.
  * a loop is a cue with an out-point, so ``loops="cue_type"`` — the same answer
    as Traktor and Rekordbox, and NOT the `separate_bank` the original
    multi-platform plan assumed.
"""
from __future__ import annotations

from ...core.capabilities import (
    Capabilities,
    CueCapabilities,
    GridCapabilities,
    PlaylistCapabilities,
    SaveCapabilities,
    TrackCapabilities,
)

# Pads A-H. Unlike `djmdCue.Kind`, the ANLZ slot number is a dense 1-based index,
# so the bank really is 1..8 (see `cues.py`).
HOTCUE_SLOTS = 8


def capabilities_for(device_name: str | None = None, version: str | None = None) -> Capabilities:
    return Capabilities(
        platform="onelibrary",
        version=version,
        # A roadmap gap, not a protective refusal — the UI words the two
        # differently, which is the whole reason `readonly_cause` exists.
        writable=False,
        readonly_cause="platform_incomplete",
        cues=CueCapabilities(
            editable=False,
            hotcue_slots=HOTCUE_SLOTS,
            slot_labels="letter",
            # OneLibrary carries memory cues, as Rekordbox does. They are
            # projected and preserved; nothing here is editable anyway.
            memory_cues=True,
            max_memory_cues=None,
            types=["cue", "loop"],
            color="free",  # PCO2 stores RGB per cue, not a palette index
            palette=[],
            named=True,  # PCO2 carries a per-cue comment
            loops="cue_type",
        ),
        # The grid is the ANLZ `PQTZ` tag, per beat, and multi-tempo grids do
        # survive an export — so the format is flexible even though Konduktor
        # does not write it here.
        grid=GridCapabilities(editable=False, flexible=True, lockable=False),
        tracks=TrackCapabilities(
            rating_max=5,  # stored 0-5 directly, as in master.db
            editable_fields=[],
            media_kinds=["audio"],
            # The drive has an `image` table and an Artwork folder, but nothing
            # is wired up to read them yet.
            artwork=False,
            artwork_note=None,
        ),
        playlists=PlaylistCapabilities(
            folders=True,  # `playlist.attribute` 1, nested via playlist_id_parent
            # A drive carries no smart playlists: rekordbox resolves them to
            # static lists on export, because a CDJ cannot evaluate rules.
            smart="none",
            reorder=False,
        ),
        save=SaveCapabilities(
            app_name="OneLibrary",
            library_label=device_name or "exportLibrary.db",
            # Nothing is written, so nothing can be overwritten by another app.
            overwrite_risk="none",
            # A drive is a database plus N analysis files plus the audio itself,
            # so there is no single blob that IS the library — the same reason
            # Rekordbox libraries are not versioned.
            history=False,
        ),
    )
