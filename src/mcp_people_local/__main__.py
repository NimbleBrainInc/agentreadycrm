"""``agentreadycrm``: serve Agent Ready CRM on the local runtime, over stdio or loopback HTTP.

    agentreadycrm                # stdio
    agentreadycrm --http         # http://127.0.0.1:$PEOPLE_LOCAL_PORT/mcp
    agentreadycrm --print-token  # the bearer token local HTTP requires, then exit

This is the local runtime's own command, so it selects that runtime: it sets
``PEOPLE_RUNTIME=local`` for its process when the variable is unset, and refuses to start,
before constructing any runtime, when it names another. ``python -m mcp_people.server`` stays the generic entry point, which
serves whichever runtime ``PEOPLE_RUNTIME`` names and refuses when it is unset.

``--print-token`` reads (or first makes) the token for the database ``PEOPLE_LOCAL_DB`` names,
so an installer can register an HTTP client before the server first starts. It serves nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

PROG = "agentreadycrm"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Serve Agent Ready CRM on this machine (stdio by default)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--http", action="store_true", help="serve HTTP on 127.0.0.1 instead of stdio"
    )
    mode.add_argument(
        "--print-token",
        action="store_true",
        help="print the bearer token local HTTP requires for the configured database, and exit",
    )
    args = parser.parse_args(argv)
    if args.print_token:
        from mcp_people_local.auth import load_or_create, token_path
        from mcp_people_local.config import database_path

        print(load_or_create(token_path(database_path())))
        return
    # Logs go to stderr only; on stdio, stdout carries nothing but JSON-RPC.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from mcp_people.runtime import RUNTIME_ENV, RuntimeSelectionError

    named = (os.environ.get(RUNTIME_ENV) or "").strip()
    if named and named != "local":
        # Refused before any runtime is constructed, so another runtime's code never runs here.
        print(
            f"{PROG}: serves the local runtime only; {RUNTIME_ENV} selects {named!r}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    os.environ[RUNTIME_ENV] = "local"

    from mcp_people import server

    try:
        server.installed()
    except RuntimeSelectionError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not args.http:
        server.mcp.run()
        return

    import uvicorn

    from mcp_people_local.config import LOOPBACK_HOST, http_port

    uvicorn.run(server.app, host=LOOPBACK_HOST, port=http_port())


if __name__ == "__main__":
    main()
