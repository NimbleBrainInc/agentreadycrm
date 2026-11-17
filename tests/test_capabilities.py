"""The capabilities every runtime declares, in each protocol era a client may negotiate.

``golden/surface.json`` holds what a 2026-07-28-era client reads. A handshake-era client
(protocol 2025-11-25 and earlier) reads the server's notification options instead, so this
holds that era apart: the tool and prompt sets never change, and a write announces
``resources/list_changed``.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from surface_shape import GOLDEN, dump

from mcp_people import server

pytestmark = pytest.mark.asyncio


async def test_a_handshake_era_client_reads_only_the_resource_list_as_changing() -> None:
    async with Client(server.mcp, mode="legacy") as client:
        capabilities = dump(client.server_capabilities)
    assert capabilities == {
        "logging": {},
        "prompts": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": True},
        "tools": {"listChanged": False},
    }


async def test_a_current_era_client_reads_the_golden_capabilities() -> None:
    async with Client(server.mcp, mode="auto") as client:
        capabilities = dump(client.server_capabilities)
    assert capabilities == json.loads(GOLDEN.read_text())["capabilities"]
    assert capabilities["tools"] == {"listChanged": False}
