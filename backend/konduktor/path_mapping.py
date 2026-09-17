"""Compatibility shim — this module was split.

``PathMapping``/``common_dir_prefix`` are format-agnostic and moved to
``konduktor.core.pathmap``; ``os_path_to_location`` is the inverse of Traktor's
``resolve_path`` and moved to ``konduktor.adapters.traktor.locations``. Kept so
the existing test suite keeps importing the old path unchanged; removed in the
final cleanup step.
"""
from .adapters.traktor.locations import os_path_to_location  # noqa: F401
from .core.pathmap import PathMapping, common_dir_prefix  # noqa: F401
