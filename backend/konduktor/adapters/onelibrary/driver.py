"""Opening OneLibrary drives: recognition and discovery."""
from __future__ import annotations

import logging
from pathlib import Path

from . import discovery
from .adapter import OneLibraryAdapter
from .layout import DriveLayout

log = logging.getLogger(__name__)


class OneLibraryDriver:
    platform = "onelibrary"
    display_name = "OneLibrary"
    suffixes = (".db",)
    library_label = "exportLibrary.db"

    def can_open(self, path: Path) -> bool:
        """Recognise a OneLibrary drive, by layout and then by schema.

        Both halves are needed. The layout check alone would accept any file
        called ``exportLibrary.db``; the schema check alone cannot run on a
        directory, and a drive is what a user actually picks.

        It must also not collide with the Rekordbox driver, which probes for
        `master.db`. It cannot: both files are SQLCipher databases with no
        readable header, but they use DIFFERENT keys, so each driver's decrypt
        simply fails on the other's file. The table probe then makes it explicit
        rather than leaving the two relying on a key mismatch.
        """
        layout = DriveLayout.locate(Path(path))
        if layout is None:
            return False
        return self._has_onelibrary_schema(layout.database)

    @staticmethod
    def _has_onelibrary_schema(path: Path) -> bool:
        try:
            import sqlcipher3.dbapi2 as sqlcipher
            from pyrekordbox.devicelib_plus.database import BLOB
            from pyrekordbox.utils import deobfuscate
        except ImportError:  # pragma: no cover — dependency missing
            return False
        con = None
        try:
            con = sqlcipher.connect(str(path))
            con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
            # `content` is too generic a name to trust on its own, so the probe
            # asks for a pair only this schema has together.
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('content','hotCueBankList')"
            ).fetchall()
            return len(rows) == 2
        except Exception:  # noqa: BLE001 — wrong key or not a database
            return False
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

    def open(self, path: Path) -> OneLibraryAdapter:
        return OneLibraryAdapter(path)

    def detect(self) -> list[dict]:
        return discovery.detect_libraries()

    def describe(self, path: Path) -> dict:
        return discovery.describe(path)

    def restore(self, path: Path, data: bytes) -> None:
        """Not supported: a drive is a database plus analysis files plus audio.

        `capabilities.save.history` is False for the same reason, so nothing
        should reach this.
        """
        raise NotImplementedError(
            "Konduktor does not version OneLibrary drives: the library is a "
            "database, N analysis files and the audio itself."
        )
