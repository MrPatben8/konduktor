"""`python -m konduktor.server` — what the container runs.

Configuration is the environment (see `config.py`); a bad setting stops here
with one clear line instead of serving a library that half-works.
"""
from __future__ import annotations

import logging
import sys

import uvicorn

from .config import ConfigError, from_env


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = from_env()
    except ConfigError as ex:
        print(f"konduktor-server: {ex}", file=sys.stderr)
        return 2
    from .app import Server, create_app

    server = Server(config)
    try:
        server.open()
    except ConfigError as ex:
        print(f"konduktor-server: {ex}", file=sys.stderr)
        return 2
    uvicorn.run(create_app(server), host=config.host, port=config.port,
                log_level="info", proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
