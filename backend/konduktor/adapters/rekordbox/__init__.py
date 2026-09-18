"""The Rekordbox adapter: ``master.db`` (SQLCipher) via ``pyrekordbox``.

Importing this package registers the platform, so ``core.registry`` can find it
without knowing it exists.

Read-only in this milestone: the library is projected, browsed and searched, but
every command is refused. See `capabilities.py` for what that reports and the
handoff's Findings section for the research the write path will be built on.
"""
from ...core import registry
from .driver import RekordboxDriver

REKORDBOX = RekordboxDriver()
registry.register(REKORDBOX)
