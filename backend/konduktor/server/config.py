"""The server's configuration: the container's environment (its `.env`).

Everything is read once at start and checked before the library is opened, and
a bad setting stops the container with ONE clear line rather than a library
that half-works: a missing content folder would otherwise show up as every
track "missing", and an empty password as an open door.

  KONDUKTOR_PLATFORM   traktor | rekordbox — checked against the library file
  KONDUKTOR_LIBRARY    the library file (collection.nml / master.db)
  KONDUKTOR_CONTENT    the folder holding the audio; uploads and the folder
                       browser are confined to it
  KONDUKTOR_USERNAME   the one account
  KONDUKTOR_PASSWORD
  KONDUKTOR_PATH_MAP   optional: "stored => here", e.g.
                       "/Users/ben/Music => /music"; several separated by ";".
                       Applied for the session only — the library keeps its
                       stored paths, so it still opens on the computer it
                       was made on.
  KONDUKTOR_NAME       optional: what the library is called on screen
  KONDUKTOR_DATA_DIR   where history, export sets and the ledger live
                       (read by `paths.app_data_dir`)
  KONDUKTOR_HOST / KONDUKTOR_PORT   where to listen (0.0.0.0:8765)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core.pathmap import PathMapping

PLATFORMS = ("traktor", "rekordbox")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServerConfig:
    platform: str
    library: Path
    content: Path
    username: str
    password: str
    path_maps: tuple[PathMapping, ...] = field(default_factory=tuple)
    name: str = ""
    host: str = "0.0.0.0"
    port: int = 8765


def parse_path_maps(text: str | None) -> tuple[PathMapping, ...]:
    """`"a => b; c => d"` → mappings. Raises `ConfigError` on a malformed one."""
    out = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=>" not in part:
            raise ConfigError(f"KONDUKTOR_PATH_MAP: expected 'stored => here', got {part!r}")
        src, dst = (s.strip() for s in part.split("=>", 1))
        mapping = PathMapping.make(src, dst)
        if mapping.empty:
            raise ConfigError(f"KONDUKTOR_PATH_MAP: both sides are needed in {part!r}")
        out.append(mapping)
    return tuple(out)


def from_env(env=None) -> ServerConfig:
    env = os.environ if env is None else env

    def need(key: str) -> str:
        value = (env.get(key) or "").strip()
        if not value:
            raise ConfigError(f"{key} is not set")
        return value

    platform = need("KONDUKTOR_PLATFORM").lower()
    if platform not in PLATFORMS:
        raise ConfigError(f"KONDUKTOR_PLATFORM must be one of {', '.join(PLATFORMS)}, not {platform!r}")
    library = Path(need("KONDUKTOR_LIBRARY"))
    if not library.exists():
        raise ConfigError(f"KONDUKTOR_LIBRARY: {library} does not exist")
    content = Path(need("KONDUKTOR_CONTENT"))
    if not content.is_dir():
        raise ConfigError(f"KONDUKTOR_CONTENT: {content} is not a folder")
    username = need("KONDUKTOR_USERNAME")
    password = env.get("KONDUKTOR_PASSWORD") or ""
    if not password:
        raise ConfigError("KONDUKTOR_PASSWORD is not set")
    try:
        port = int(env.get("KONDUKTOR_PORT") or 8765)
    except ValueError:
        raise ConfigError("KONDUKTOR_PORT must be a number")
    return ServerConfig(
        platform=platform, library=library, content=content.resolve(),
        username=username, password=password,
        path_maps=parse_path_maps(env.get("KONDUKTOR_PATH_MAP")),
        name=(env.get("KONDUKTOR_NAME") or "").strip(),
        host=(env.get("KONDUKTOR_HOST") or "0.0.0.0").strip(), port=port,
    )
