"""What a OneLibrary drive can persist.

Opened as THE library, a drive is writable, and the per-feature flags say which
edits have landed: track metadata and playlists so far; cues, the grid, adding
and removing tracks are still False, so the UI never offers an edit the adapter
would refuse. Opened `read_only` — the sidebar's Devices, browsing beside the
loaded library — `writable` is False with cause `browsing`, which the UI words
as "open it for editing", not as a missing feature.

With `writable=False` the per-feature flags still report what the FORMAT
supports rather than being blanked, because that is what the library-level flag
is for: the UI can show a drive's cues and grid honestly and say once, in one
place, that the library is read-only. Blanking them would claim OneLibrary has no
hot cues.

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


def capabilities_for(
    device_name: str | None = None,
    version: str | None = None,
    *,
    read_only: bool = False,
    editable_fields: list[str] | None = None,
) -> Capabilities:
    return Capabilities(
        platform="onelibrary",
        version=version,
        writable=not read_only,
        readonly_cause="browsing" if read_only else None,
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
            # The Rekordbox adapter's set: `producer`/`mix` have no column.
            editable_fields=sorted(editable_fields or []),
            media_kinds=["audio", "stem"],
            # `artwork` gates EDITING, which Rekordbox does not do yet either
            # (parity). Reading works: `store.cover_art` follows
            # `content.image_id` -> `image.path`.
            artwork=False,
            artwork_note=None,
        ),
        playlists=PlaylistCapabilities(
            folders=True,  # `playlist.attribute` 1, nested via playlist_id_parent
            # A drive carries no smart playlists: rekordbox resolves them to
            # static lists on export, because a CDJ cannot evaluate rules.
            smart="none",
            reorder=True,
        ),
        save=SaveCapabilities(
            # Named for the DRIVE ("Save to Goober"): a person has several
            # sticks, and "Save to OneLibrary" would not say which.
            app_name=device_name or "OneLibrary",
            library_label="exportLibrary.db",
            # Another app writing the drive between open and save is CAUGHT,
            # not risked: the store fingerprints the database at open and
            # refuses to save over a change it did not make.
            overwrite_risk="none",
            # A drive is a database plus N analysis files plus the audio itself,
            # so there is no single blob that IS the library — the same reason
            # Rekordbox libraries are not versioned.
            history=False,
        ),
    )
