"""Per-platform adapters. Each owns a native library model and translates the
generic commands onto it; nothing outside an adapter package sees a native type.

Importing this package registers every adapter. Order matters only for the
registry's ``can_open()`` probe, and the probes are mutually exclusive: Traktor
requires an NML header, Rekordbox an encrypted database with its own schema, and
OneLibrary a drive laid out around ``PIONEER/rekordbox/exportLibrary.db``. The
two encrypted databases cannot be confused for one another — they use different
SQLCipher keys, so each driver's decrypt fails outright on the other's file.
"""
from . import onelibrary  # noqa: F401  — registers the OneLibrary driver
from . import rekordbox  # noqa: F401  — registers the Rekordbox driver
from . import traktor  # noqa: F401  — registers the Traktor driver
