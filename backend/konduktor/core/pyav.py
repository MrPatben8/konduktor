"""Opening a file for reading with PyAV, on every PyAV version the app ships.

A file whose tags are not valid UTF-8 must still open: it is still audio.
PyAV 18 decodes tags strictly by default and raises UnicodeDecodeError at open
unless given `metadata_errors="ignore"`; PyAV 19 REMOVED that keyword (passing
it is a TypeError) and opens such a file as it is, the bad bytes as surrogates.
Measured on both with a generated M4A and MP3 whose title is invalid UTF-8.

The keyword once went straight into `av.open`, which worked on the dev venv's
18.1 while CI's fresh install took 19.0.1: every stem file then failed to open
in the released app, and the deck played stem tracks as the mix. Every read
goes through `open_input`, so the difference lives in one place.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path


@cache
def _takes_metadata_errors() -> bool:
    import av

    return int(av.__version__.split(".")[0]) < 19


def open_input(path: Path | str):
    """`av.open(path)` for reading, tolerant of tags that are not UTF-8."""
    import av

    if _takes_metadata_errors():
        return av.open(str(path), metadata_errors="ignore")
    return av.open(str(path))
