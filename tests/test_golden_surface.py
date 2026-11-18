"""Golden: the MCP surface this server advertises.

Every tool (name, title, description, input and output schemas, annotations, meta), every
resource and resource template (URI, name, description, MIME type, meta), the server's
capabilities and instructions, and a digest of each skill it serves. This is what agents and
hosts rely on, and the parity target for every runtime: a runtime may change where identity
comes from, never what a client sees.

``tests/golden/surface.json`` is frozen output with no regenerate path. A deliberate surface
change edits it in the same PR, where review sees the diff. The shape is built in
``surface_shape``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from surface_shape import GOLDEN, collect, normalized

from mcp_people import server


async def surface() -> dict[str, Any]:
    async with Client(server.mcp) as client:
        return await collect(client, server.mcp.instructions)


@pytest.mark.asyncio
async def test_surface_matches_golden() -> None:
    assert normalized(await surface()) == json.loads(GOLDEN.read_text())
