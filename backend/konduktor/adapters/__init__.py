"""Per-platform adapters. Each owns a native library model and translates the
generic commands onto it; nothing outside an adapter package sees a native type.

Importing this package registers every adapter.
"""
from . import traktor  # noqa: F401  — registers the Traktor driver
