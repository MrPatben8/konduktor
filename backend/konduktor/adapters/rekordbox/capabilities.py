"""What a Rekordbox library can persist.

**Milestone 1 reports everything as read-only.** The spike verified that
Rekordbox accepts writes that maintain its USN columns (see the handoff's
Findings), but the write path is not built yet, and the capability system exists
precisely so the UI never offers an edit that will not persist. Every flag here
is therefore false/empty until milestone 2 turns them on one at a time.

Two facts are already known and encoded, because getting them wrong later would
be a silent data bug rather than a missing feature:

  * a saved loop is a cue with an out-point, NOT a separate bank, so
    ``loops="cue_type"`` — the same answer as Traktor, and NOT the
    ``separate_bank`` the original plan assumed;
  * cue colour is a PALETTE index (``djmdCue.ColorTableIndex``), not free RGB,
    and the palette is a built-in table — `djmdColor` is the *track* colour
    palette and is unrelated.
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
from .cue_types import CUE_TYPES

# Rekordbox's addressable hot cue bank. The pads are labelled A-H; the stored
# slot number is NOT a plain letter index (see cue_types), so the adapter keeps
# slots numeric and lets the UI label them.
HOTCUE_SLOTS = 8


def capabilities_for(
    path, version: str | None = None, *, cloud_synced: bool = False
) -> Capabilities:
    # A cloud-synced library is refused PERMANENTLY, not pending a milestone:
    # a local edit the server did not issue could propagate a broken sync state
    # to the user's other machines, which version history cannot undo.
    return Capabilities(
        platform="rekordbox",
        version=version,
        writable=False,
        readonly_cause="cloud_synced" if cloud_synced else "platform_incomplete",
        cues=CueCapabilities(
            hotcue_slots=HOTCUE_SLOTS,
            slot_labels="letter",
            # Rekordbox is the only platform with memory cues, so they are
            # projected and PRESERVED but not editable (two-platform rule).
            memory_cues=True,
            max_memory_cues=None,  # unlimited
            types=CUE_TYPES,
            color="palette",
            palette=[],  # TODO milestone 2: the built-in cue colour table
            named=True,  # djmdCue.Comment
            loops="cue_type",
        ),
        grid=GridCapabilities(editable=False, flexible=True, lockable=False),
        tracks=TrackCapabilities(
            rating_max=5,  # Rekordbox stores 0-5 directly, unlike Traktor's /51
            editable_fields=[],  # read-only in this milestone
            media_kinds=["audio"],
            artwork=False,
            artwork_note=None,
        ),
        playlists=PlaylistCapabilities(folders=True, smart="read_only", reorder=False),
        save=SaveCapabilities(
            app_name="Rekordbox",
            library_label="master.db",
            # Rekordbox holds master.db open while running and cloud-syncs it,
            # so a concurrent write is riskier than Traktor's overwrite-on-exit.
            overwrite_risk="while_running",
            # The artefact set is master.db + N ANLZ files + masterPlaylists6.xml,
            # and history.py tracks a single blob. Also, restoring master.db
            # wholesale would roll back Rekordbox's auth token and sampler state.
            history=False,
        ),
    )
