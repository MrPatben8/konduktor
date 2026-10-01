"""The Rekordbox adapter: ``master.db`` (SQLCipher) via ``pyrekordbox``.

Importing this package registers the platform for READING and editing a library
in place, and as an export TARGET for writing a fresh one. Two registries,
because opening a library and creating one from nothing are different
capabilities.
"""
from ...core import export as core_export
from ...core import registry
from .driver import RekordboxDriver
from .device_export import RekordboxDeviceExporter
from .export import RekordboxExporter

REKORDBOX = RekordboxDriver()
registry.register(REKORDBOX)

REKORDBOX_EXPORTER = RekordboxExporter()
core_export.register(REKORDBOX_EXPORTER)

# The stick's legacy Device Library (`export.pdb`): a separate TARGET with no
# reader of its own — Konduktor reads a stick through its OneLibrary database.
REKORDBOX_DEVICE_EXPORTER = RekordboxDeviceExporter()
core_export.register(REKORDBOX_DEVICE_EXPORTER)
