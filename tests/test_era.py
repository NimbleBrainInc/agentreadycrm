"""The server serves both protocol eras from one process.

A v2 client in its default mode negotiates the sessionless 2026-07-28 era; ``mode="legacy"``
takes the session handshake a 2025-era host still speaks. Both see the same tools and
resources, and every ``skill://`` and ``ui://`` resource still reads under FastMCP's
templated-resource path screening. Nothing here touches the database.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_people.server import mcp

ERAS = [("auto", "2026-07-28"), ("legacy", "2025-11-25")]
SERVED = ["skill://people/SKILL.md", "skill://people-evidence/SKILL.md", "ui://people/contacts"]


@pytest.mark.parametrize(("mode", "era"), ERAS, ids=[m for m, _ in ERAS])
async def test_both_eras_list_and_read_the_same_surface(mode: str, era: str):
    async with Client(mcp, mode=mode) as client:
        assert client.protocol_version == era
        tools = await client.list_tools()
        uris = {str(r.uri) for r in await client.list_resources()}
        contents = {uri: await client.read_resource(uri) for uri in SERVED}

    assert "find_or_create_contact" in {t.name for t in tools}
    assert set(SERVED) <= uris
    for uri, [content] in contents.items():
        assert str(content.uri) == uri
        assert getattr(content, "text", "")


async def test_both_eras_see_the_same_tools():
    names = {}
    for mode, _ in ERAS:
        async with Client(mcp, mode=mode) as client:
            names[mode] = {t.name for t in await client.list_tools()}
    assert names["auto"] == names["legacy"]
