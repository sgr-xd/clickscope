"""Entrypoint: `clickscope`, `python -m clickscope`, or `uvx clickscope`."""

from __future__ import annotations

import sys

from .config import ConfigError
from .server import create_server


def main() -> int:
    try:
        mcp, registry = create_server()
    except ConfigError as exc:
        # stderr, because stdout is the MCP transport — anything written there that is
        # not a protocol message corrupts the session.
        print(f"clickscope: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        mcp.run(transport="stdio")
    finally:
        registry.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
