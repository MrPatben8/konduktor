"""What normalising does to ONE file's level, as one pure function.

Shared by Normalize's preview, its run and (later) the export's "Normalise
audio", as `grid_plan` is shared by Analyze — so the dialog's counts and what
is written cannot disagree. Decided 2026-10-19 (the gain-normalisation
discussion):

  * The level is the user's pick of integrated **LUFS** or loud-section
    **RMS** (`core.loudness`), brought to `target`.
  * Up AND down, but a raise stops at the TRUE-PEAK `ceiling` (default
    -1 dBTP): nothing Konduktor writes ever clips. A track that falls short
    is `capped` and says by how much.
  * A track whose peak is already over the ceiling and which is at or below
    target is left alone (`hot_peak_left`) — normalising is about loudness,
    not peak repair.
  * Lossy files change only by the LOSSLESS frame-gain edit (`level_edit`):
    whole steps of 2^(1/4) in amplitude, 1.505 dB. A lowering rounds to the
    nearest step; a raise to the nearest step that stays under the ceiling.
    Lossless files take the gain exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .loudness import Loudness

#: One `global_gain` step: amplitude x 2^(1/4).
STEP_DB = 20.0 * math.log10(2.0 ** 0.25)

#: Rewritten exactly (PCM scaled, same bit depth).
EXACT = frozenset({".wav", ".wave", ".aif", ".aiff", ".flac"})
#: Rewritten by the lossless frame-gain edit, in `STEP_DB` steps.
STEPPED = frozenset({".mp3"})

#: Closer than this to the target (lossless) is "already there".
_EXACT_TOLERANCE_DB = 0.05

Measure = Literal["lufs", "rms"]
Outcome = Literal["apply", "capped", "at_target", "hot_peak_left", "unsupported", "unmeasured"]


@dataclass(frozen=True)
class GainStep:
    """`gain_db` to apply (0: nothing to write) and why. `short_by` is how far
    below target a `capped` track stays."""

    gain_db: float
    outcome: Outcome
    short_by: float = 0.0

    @property
    def writes(self) -> bool:
        return self.gain_db != 0.0


def supported(suffix: str) -> bool:
    return suffix.lower() in EXACT or suffix.lower() in STEPPED


def plan_gain(loudness: Loudness | None, suffix: str, *, measure: Measure,
              target: float, ceiling: float) -> GainStep:
    """The gain for one file. `suffix` decides exact vs stepped (".mp3")."""
    suffix = suffix.lower()
    if not supported(suffix):
        return GainStep(0.0, "unsupported")
    if loudness is None:
        return GainStep(0.0, "unmeasured")
    level = loudness.lufs if measure == "lufs" else loudness.rms
    want = target - level
    stepped = suffix in STEPPED
    tolerance = STEP_DB / 2 if stepped else _EXACT_TOLERANCE_DB
    if abs(want) < tolerance:
        return GainStep(0.0, "at_target")
    if want < 0:  # lowering never clips
        gain = round(want / STEP_DB) * STEP_DB if stepped else want
        return GainStep(_clean(gain), "apply")
    headroom = ceiling - loudness.true_peak
    if headroom <= 0:
        return GainStep(0.0, "hot_peak_left")
    gain = min(want, headroom)
    if stepped:
        n = round(gain / STEP_DB)
        if n * STEP_DB > headroom + 1e-9:
            n -= 1
        gain = n * STEP_DB
    gain = _clean(gain)
    if want - gain >= tolerance:
        return GainStep(gain if gain > 0 else 0.0, "capped", short_by=_clean(want - gain))
    return GainStep(gain, "apply")


def shifted(loudness: Loudness, gain_db: float) -> Loudness:
    """A file's loudness after `gain_db`: LUFS, RMS and true peak move by it,
    LRA and PLR do not (a gain changes no dynamics). Exact for both edits:
    `level_edit` scales the decoded samples by exactly 10^(g/20)."""
    return Loudness(lufs=loudness.lufs + gain_db, rms=loudness.rms + gain_db,
                    true_peak=loudness.true_peak + gain_db, lra=loudness.lra, plr=loudness.plr)


def _clean(x: float) -> float:
    return round(x, 6) + 0.0  # no -0.0, no float noise in a stored value
