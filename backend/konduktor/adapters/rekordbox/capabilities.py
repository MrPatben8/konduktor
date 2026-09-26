"""What a Rekordbox library can persist.

**Track metadata and playlists are writable; cues and the beatgrid are not.**
The spike verified that Rekordbox accepts writes which maintain its USN columns
(see the handoff's Findings), and those two write paths use `pyrekordbox`, which
maintains them. Cues and the grid are separate hand-written stores and stay
false until milestone 3 — the capability system exists precisely so the UI never
offers an edit that will not persist.

A **cloud-synced** library reports `writable=False` regardless: Konduktor will
not write one at all.

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
from .cue_types import WRITABLE_CUE_TYPES

# Rekordbox's addressable hot cue bank. The pads are labelled A-H; the stored
# slot number is NOT a plain letter index (see cue_types), so the adapter keeps
# slots numeric and lets the UI label them.
HOTCUE_SLOTS = 8


def capabilities_for(
    path,
    version: str | None = None,
    *,
    cloud_synced: bool = False,
    editable_fields: list[str] | None = None,
) -> Capabilities:
    # A cloud-synced library is refused PERMANENTLY, not pending a milestone:
    # a local edit the server did not issue could propagate a broken sync state
    # to the user's other machines, which version history cannot undo.
    return Capabilities(
        platform="rekordbox",
        version=version,
        writable=not cloud_synced,
        readonly_cause="cloud_synced" if cloud_synced else None,
        cues=CueCapabilities(
            # Hot cues are writable: djmdCue rows plus the contentCue JSON
            # mirror, kept in step. MEMORY cues stay preserved-but-uneditable —
            # Rekordbox is the only platform that has them, and the two-platform
            # promotion rule says a one-platform concept does not become an
            # editing feature.
            editable=True,
            hotcue_slots=HOTCUE_SLOTS,
            slot_labels="letter",
            # Rekordbox is the only platform with memory cues, so they are
            # projected and PRESERVED but not editable (two-platform rule).
            memory_cues=True,
            max_memory_cues=None,  # unlimited
            # Both cue types write; Rekordbox has no fade/load types.
            types=WRITABLE_CUE_TYPES,
            color="palette",
            palette=[],  # TODO milestone 2: the built-in cue colour table
            named=True,  # djmdCue.Comment
            loops="cue_type",
        ),
        # The grid is written into the track's ANLZ .DAT (PQTZ). Verified:
        # Rekordbox reads the grid and its BPM readout from there, and leaves a
        # Konduktor-written file alone. It has no per-track grid lock.
        grid=GridCapabilities(editable=True, flexible=True, lockable=False),
        tracks=TrackCapabilities(
            rating_max=5,  # Rekordbox stores 0-5 directly, unlike Traktor's /51
            # Narrower than Traktor's: `producer` and `mix` have no Rekordbox
            # column (its Composer is a different field), so they are absent
            # rather than mapped onto something approximate.
            editable_fields=sorted(editable_fields or []),
            media_kinds=["audio"],
            artwork=False,
            artwork_note=None,
        ),
        playlists=PlaylistCapabilities(folders=True, smart="read_only", reorder=True),
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
