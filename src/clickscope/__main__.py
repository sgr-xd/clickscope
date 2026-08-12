"""Entrypoint: `clickscope`, `python -m clickscope`, or `uvx clickscope`."""

from __future__ import annotations

import sys

from .config import ConfigError
from .server import create_server, version

USAGE = """\
clickscope — a read-only ClickHouse MCP server.

Usage:
  clickscope              start the server on stdio (how an MCP client runs it)
  clickscope --version    print the version and exit
  clickscope --help       print this message and exit

Configuration comes from the environment:
  CLICKHOUSE_HOST         host, host:port, or a URL such as https://host:8443
  CLICKHOUSE_USER         database user; prefer one granted only SELECT
  CLICKHOUSE_PASSWORD     or CLICKHOUSE_PASSWORD_FILE, which is preferred
  CLICKHOUSE_DATABASE     default database for tool calls
  CLICKSCOPE_PROFILES     path to a JSON profiles file, for several clusters
  CLICKSCOPE_AUDIT_LOG    path to record every query decision as JSONL

Full documentation: https://github.com/sgr-xd/clickscope
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # Answered before any configuration is read, so `--version` works on a broken
    # install — which is exactly when you need to know which version is running.
    if "--version" in args or "-V" in args:
        print(f"clickscope {version()}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE, end="")
        return 0
    if args:
        print(f"clickscope: unrecognised argument {args[0]!r}\n", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

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
