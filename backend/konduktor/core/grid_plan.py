"""What an analysis does to one track's grid, given the BPM / Grid ticks.

One pure function, so the batch, the dialog's counts and the tests cannot come
to mean different things. Decided with the user (2026-10-04, see
`.claude/discussions/discuss-key-analysis-2026-10-04.md`):

  - BPM + Grid: a full analysis. A track that already has a grid is replaced
    only when the user ticked "Replace" — a grid may hold hand edits.
  - BPM alone: re-detect the tempo, KEEP the anchor (the first marker).
  - Grid alone: KEEP the BPM, find only where the beats fall.
  - With one tick, adjusting the existing grid is the point, so "Replace" does
    not apply; a track with nothing to keep (no grid / no BPM) is analysed in
    full rather than skipped — one tick never leaves a track worse off.
  - A multi-marker (flexible) grid is skipped by BPM alone and Grid alone: its
    tempo changes are exactly what one constant tempo would flatten.
  - A locked grid is skipped whatever is ticked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

Action = Literal["none", "full", "bpm", "phase", "skip"]
SkipReason = Literal["locked", "existing", "flexible"]


@dataclass(frozen=True)
class GridStep:
    action: Action
    reason: SkipReason | None = None
    hold_bpm: float | None = None      # "phase": the tempo kept
    hold_anchor: float | None = None   # "bpm": the anchor kept (seconds)


def plan_grid(
    *,
    bpm: bool,
    grid: bool,
    markers: Sequence,              # the track's grid markers (`.start`, `.bpm`), any order
    track_bpm: float | None,
    locked: bool,
    replace_existing: bool,
) -> GridStep:
    if not (bpm or grid):
        return GridStep("none")
    if locked:
        return GridStep("skip", "locked")
    ordered = sorted(markers, key=lambda m: m.start)
    if bpm and grid:
        if ordered and not replace_existing:
            return GridStep("skip", "existing")
        return GridStep("full")
    if len(ordered) > 1:
        return GridStep("skip", "flexible")
    if bpm:  # BPM alone
        if not ordered:
            return GridStep("full")
        return GridStep("bpm", hold_anchor=float(ordered[0].start))
    # Grid alone
    held = float(ordered[0].bpm) if ordered else (float(track_bpm) if track_bpm else None)
    if not held or held <= 0:
        return GridStep("full")
    return GridStep("phase", hold_bpm=held)
