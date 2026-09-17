"""Compatibility shim — this module was split.

The mutagen read/write half is format-agnostic and moved to
``konduktor.core.audio_tags``; ``resolve_path`` encodes Traktor's LOCATION
convention and moved to ``konduktor.adapters.traktor.locations``. Kept so the
existing test suite keeps importing the old path unchanged; removed in the final
cleanup step.
"""
from .adapters.traktor.locations import resolve_path  # noqa: F401
from .core.audio_tags import (  # noqa: F401
    DEFAULT_POPM_EMAIL,
    TagMeta,
    TagResult,
    read_cover,
    write_cover,
    write_tags,
)
