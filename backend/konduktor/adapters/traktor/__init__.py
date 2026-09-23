"""The Traktor adapter: NML v20 (Traktor Pro 4) via ``traktor-nml-utils``.

Importing this package registers the platform, so ``core.registry`` can find it
without knowing it exists — and registers it as an export TARGET, which is a
separate registry because opening a library and creating one from nothing are
different capabilities: Rekordbox has the first and not the second.
"""
from ...core import export as core_export
from ...core import registry
from .driver import TraktorDriver
from .export import TraktorExporter

TRAKTOR = TraktorDriver()
registry.register(TRAKTOR)

TRAKTOR_EXPORTER = TraktorExporter()
core_export.register(TRAKTOR_EXPORTER)
