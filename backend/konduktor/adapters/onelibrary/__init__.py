"""The OneLibrary adapter: a USB drive's ``exportLibrary.db``, via ``pyrekordbox``.

OneLibrary is the cross-vendor USB export format agreed between AlphaTheta,
Algoriddim and Native Instruments, and read by CDJ-class hardware as well as by
rekordbox, djay Pro and Traktor. Unlike every other adapter, its library is a
REMOVABLE DRIVE rather than a file in a known place — see `layout.py`.

Importing this package registers the platform, so ``core.registry`` can find it
without knowing it exists.

Read-only: a drive is projected, browsed and searched, and every command is
refused. See `adapter.py` for why that scope is deliberate.
"""
from ...core import registry
from .driver import OneLibraryDriver

ONELIBRARY = OneLibraryDriver()
registry.register(ONELIBRARY)
