"""Opening Rekordbox libraries: recognition, discovery and restore."""
from __future__ import annotations

import logging
from pathlib import Path

from . import discovery
from .adapter import RekordboxAdapter

log = logging.getLogger(__name__)

_SQLITE_MAGIC = b"SQLite format 3\x00"


class RekordboxDriver:
    platform = "rekordbox"
    display_name = "Rekordbox"
    suffixes = (".db",)
    library_label = "master.db"
    selects = "file"

    def can_open(self, path: Path) -> bool:
        """Recognise an encrypted Rekordbox database.

        Extension is not enough — plenty of things are called ``.db`` — and a
        SQLCipher file has no readable header at all (its first page is
        encrypted, so it does NOT start with the SQLite magic). So the probe
        decrypts just far enough to look for a table only Rekordbox has. That is
        one page read, and it is the only way to answer honestly.
        """
        try:
            if not path.is_file() or path.suffix.lower() != ".db":
                return False
            with path.open("rb") as fh:
                if fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC:
                    return False  # a plain, unencrypted SQLite file
        except OSError:
            return False
        return self._has_rekordbox_schema(path)

    @staticmethod
    def _has_rekordbox_schema(path: Path) -> bool:
        try:
            import sqlcipher3.dbapi2 as sqlcipher
            from pyrekordbox.masterdb.database import BLOB
            from pyrekordbox.utils import deobfuscate
        except ImportError:  # pragma: no cover — dependency missing
            return False
        con = None
        try:
            con = sqlcipher.connect(str(path))
            con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='djmdContent'"
            ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001 — wrong key or not a database
            return False
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

    def open(self, path: Path) -> RekordboxAdapter:
        return RekordboxAdapter(path)

    def detect(self) -> list[dict]:
        return discovery.detect_libraries()

    def describe(self, path: Path) -> dict:
        return discovery.describe(path)

    def restore(self, path: Path, data: bytes) -> None:
        """Not supported: a Rekordbox library is more than one file.

        `master.db` carries neither the beatgrids (ANLZ files) nor the playlist
        XML, and putting an old database back would also revert Rekordbox's auth
        token and sampler state. `capabilities.save.history` is False for the
        same reason, so nothing should reach this.
        """
        raise NotImplementedError(
            "Konduktor does not version Rekordbox libraries: a restore would need "
            "the analysis files and would roll back Rekordbox's own app state."
        )
