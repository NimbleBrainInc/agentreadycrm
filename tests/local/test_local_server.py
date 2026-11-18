"""The local runtime end to end: every tool over stdio and over loopback HTTP, the four UI
views, and the surface a client of a separate process sees held to the golden one.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from local_harness import (
    UI_VIEWS,
    call,
    exercise_every_tool,
    http_client,
    http_server,
    read_views,
    stdio_client,
    token,
)
from surface_shape import GOLDEN, collect, normalized

ALL_TOOLS = {t["name"] for t in json.loads(GOLDEN.read_text())["tools"]}


def test_the_scenario_covers_every_tool() -> None:
    assert len(ALL_TOOLS) == 46


async def test_every_tool_works_over_stdio(tmp_path: Path) -> None:
    async with stdio_client(tmp_path / "crm.db") as client:
        called = await exercise_every_tool(client)
    assert called == ALL_TOOLS


async def test_every_tool_works_over_the_generic_entry_point(tmp_path: Path) -> None:
    # python -m mcp_people.server with PEOPLE_RUNTIME=local serves the same runtime.
    async with stdio_client(tmp_path / "crm.db", generic=True) as client:
        called = await exercise_every_tool(client)
    assert called == ALL_TOOLS


async def test_every_tool_works_over_http(tmp_path: Path) -> None:
    db = tmp_path / "crm.db"
    with http_server(db) as port:
        async with http_client(port, db) as client:
            called = await exercise_every_tool(client)
    assert called == ALL_TOOLS


async def test_stdio_and_http_share_one_database(tmp_path: Path) -> None:
    db = tmp_path / "crm.db"
    with http_server(db) as port:
        async with http_client(port, db) as client:
            await call(client, "find_or_create_organization", name="From HTTP")
        async with stdio_client(db) as client:
            listed = await call(client, "list_organizations")
    assert [o["name"] for o in listed["organizations"]] == ["From HTTP"]


async def test_identity_headers_a_request_carries_change_nothing(tmp_path: Path) -> None:
    # A multi-principal deployment may read identity from request headers; the local runtime
    # has one principal and ignores them, so a caller cannot reach other rows or hide its own.
    db = tmp_path / "crm.db"
    with http_server(db) as port:
        async with http_client(port, db) as client:
            await call(client, "find_or_create_organization", name="Mine")
        spoofed = Client(
            StreamableHttpTransport(
                f"http://127.0.0.1:{port}/mcp",
                headers={"x-account-id": "acme-corp", "x-user-id": "someone-else"},
                auth=token(db),
            )
        )
        async with spoofed as client:
            listed = await call(client, "list_organizations")
            await call(client, "find_or_create_organization", name="Also mine")
        async with stdio_client(db) as client:
            everything = await call(client, "list_organizations")
    assert [o["name"] for o in listed["organizations"]] == ["Mine"]
    assert sorted(o["name"] for o in everything["organizations"]) == ["Also mine", "Mine"]


async def test_the_views_serve_the_built_ui_with_no_setting(tmp_path: Path) -> None:
    async with stdio_client(tmp_path / "crm.db") as client:
        views = await read_views(client)
    assert set(views) == set(UI_VIEWS)
    for view, html in views.items():
        assert "not built" not in html, f"ui://people/{view} served the placeholder"
        assert f'<div id="root" data-view="{view}">' in html


async def test_a_stdio_client_sees_the_golden_surface(tmp_path: Path) -> None:
    async with stdio_client(tmp_path / "crm.db") as client:
        local = await collect(client, client.instructions)
    assert normalized(local) == json.loads(GOLDEN.read_text())
