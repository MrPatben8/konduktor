"""The OneLibrary adapter: the cross-vendor USB export format.

Importing this package registers the platform for READING (a plugged-in drive)
and as an export TARGET (writing a fresh drive). Two registries, because opening
a library and creating one from nothing are different capabilities.
"""
from ...core import export as core_export
from ...core import registry
from .driver import OneLibraryDriver
from .export import OneLibraryExporter

ONELIBRARY = OneLibraryDriver()
registry.register(ONELIBRARY)

ONELIBRARY_EXPORTER = OneLibraryExporter()
core_export.register(ONELIBRARY_EXPORTER)
