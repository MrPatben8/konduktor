"""Compatibility shim — this module moved to ``konduktor.adapters.traktor.store``.

Kept so the existing test suite keeps importing the old path unchanged while the
adapter refactor lands; removed in the final cleanup step. Replacing the entry in
``sys.modules`` makes this module *be* the new one, so private names resolve too.
"""
import sys

from .adapters.traktor import store as _moved

sys.modules[__name__] = _moved
