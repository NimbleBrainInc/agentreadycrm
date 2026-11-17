"""Start the server the way a client would, and check it serves over stdio.

Usage: python scripts/smoke.py [--runtime-env] <command> [<arg> ...]

For example, the built wheel through uvx, or the MCP bundle's own command:

    python scripts/smoke.py uvx --from dist/agentreadycrm-0.1.0-py3-none-any.whl agentreadycrm
    python scripts/smoke.py --runtime-env \
        uv run --directory mcpb --frozen --no-dev python -m mcp_people_local

The server runs on a database in a fresh temporary directory, with ``PEOPLE_RUNTIME=local`` set
only when ``--runtime-env`` comes first (the bundle's manifest sets it; ``agentreadycrm``
selects the local runtime itself). It must list the 46 tools, create an organization and list
it back, and serve the built UI under each view's URI.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

TOOLS = 46
VIEWS = ("contacts", "organizations", "opportunities", "settings")


async def smoke(command: list[str], env: dict[str, str]) -> None:
    transport = StdioTransport(command=command[0], args=command[1:], env=env)
    async with Client(transport) as client:
        info = client.server_info
        assert info is not None, "no serverInfo"
        print(f"server: {info.name} {info.version}")

        tools = await client.list_tools()
        assert len(tools) == TOOLS, f"expected {TOOLS} tools, got {len(tools)}"
        print(f"tools: {len(tools)}")

        created = await client.call_tool(
            "find_or_create_organization", {"name": "Acme Corp", "domain": "acme.example"}
        )
        org = (created.structured_content or {})["organization"]
        listed = await client.call_tool("list_organizations", {})
        ids = [o["id"] for o in (listed.structured_content or {})["organizations"]]
        assert ids == [org["id"]], f"listed {ids}, created {org['id']}"
        print(f"created and listed: {org['id']}")

        for view in VIEWS:
            uri = f"ui://people/{view}"
            [ui] = await client.read_resource(uri)
            html = getattr(ui, "text", "")
            assert ui.mime_type == "text/html;profile=mcp-app", ui.mime_type
            assert "<html" in html.lower() and "not built" not in html.lower(), "UI is not built"
            print(f"ui: {uri} ({len(html)} bytes)")


def main(argv: list[str]) -> int:
    runtime_env = bool(argv) and argv[0] == "--runtime-env"
    command = argv[1:] if runtime_env else argv
    if not command:
        print(__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PEOPLE_")}
        env["PEOPLE_LOCAL_DB"] = str(Path(tmp) / "crm.db")
        if runtime_env:
            env["PEOPLE_RUNTIME"] = "local"
        asyncio.run(smoke(command, env))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
