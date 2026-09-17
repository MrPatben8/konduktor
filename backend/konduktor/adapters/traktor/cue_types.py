"""Traktor's CUE_V2 TYPE encoding <-> the generic cue vocabulary.

The only place these integers are given meaning. Type 4 is a grid marker, not a
cue, so it has no generic name — grid markers are projected as beatgrid markers
and never appear in a track's cue list.

Lives in its own module so the projection and the adapter can both use it
without importing each other.
"""
from __future__ import annotations

CUE_TYPE_TO_NATIVE = {"cue": 0, "fade_in": 1, "fade_out": 2, "load": 3, "loop": 5}
NATIVE_TO_CUE_TYPE = {v: k for k, v in CUE_TYPE_TO_NATIVE.items()}
