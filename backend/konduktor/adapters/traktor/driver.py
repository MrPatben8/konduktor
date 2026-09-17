"""Opening Traktor libraries: recognition, discovery and restore."""
from __future__ import annotations

from pathlib import Path

from . import discovery
from .adapter import TraktorAdapter


class TraktorDriver:
    platform = "traktor"
    display_name = "Traktor"
    suffixes = (".nml",)
    library_label = "collection.nml"

    def can_open(self, path: Path) -> bool:
        """Recognise an NML without parsing 14 MB of it."""
        if not path.is_file() or path.suffix.lower() != ".nml":
            return False
        try:
            with path.open("rb") as fh:
                head = fh.read(4096)
        except OSError:
            return False
        return b"<NML" in head and b"VERSION=" in head

    def open(self, path: Path) -> TraktorAdapter:
        return TraktorAdapter(path)

    def detect(self) -> list[dict]:
        return discovery.detect_collections()

    def describe(self, path: Path) -> dict:
        return discovery.describe(path)

    def restore(self, path: Path, data: bytes) -> None:
        """Write a history snapshot back over the library, atomically."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
