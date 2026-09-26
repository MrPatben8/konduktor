"""Platform-independent core: the generic model, the query/projection layer and
the format-agnostic helpers.

Nothing in here may import ``traktor_nml_utils`` or ``konduktor.adapters`` —
``test_layering.py`` enforces that. This package is what a Rekordbox or Serato
adapter will be written against.
"""
