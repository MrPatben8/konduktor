"""The Traktor adapter: NML v20 (Traktor Pro 4) via ``traktor-nml-utils``.

Importing this package registers the platform, so ``core.registry`` can find it
without knowing it exists.
"""
from ...core import registry
from .driver import TraktorDriver

TRAKTOR = TraktorDriver()
registry.register(TRAKTOR)
