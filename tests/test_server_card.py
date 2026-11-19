"""The Server Card a host reads before it connects, at ``<mcp-url>/server-card``.

The card is built from ``FastMCP(...)`` and ``add_server_card(...)``, so its name, version and
website are what ``serverInfo`` reports. Its placements name the views the server lists.
"""

from __future__ import annotations

import httpx2
from nimblebrain_server_sdk import HOST_META_KEY, SERVER_CARD_MEDIA_TYPE

from mcp_people import server


async def served_card() -> dict:
    """Fetch the card the way a host does, from the streamable-HTTP app, in process."""
    transport = httpx2.ASGITransport(app=server.mcp.http_app())
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.get("/mcp/server-card", headers={"accept": SERVER_CARD_MEDIA_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == SERVER_CARD_MEDIA_TYPE
    return response.json()


async def test_the_card_names_the_server_as_serverinfo_does():
    card = await served_card()
    assert card["name"] == server.mcp.name == "com.agentreadycrm/agentreadycrm"
    assert card["version"] == server.mcp.version == server.package_version()
    assert card["websiteUrl"] == "https://agentreadycrm.com"
    assert card["title"] == "Agent Ready CRM"
    assert 1 <= len(card["description"]) <= 100


async def test_the_placements_name_the_listed_views():
    card = await served_card()
    placements = card["_meta"][HOST_META_KEY]["placements"]
    assert placements == server.PLACEMENTS
    listed = {str(r.uri) for r in await server.mcp.list_resources()}
    assert (
        {p["resourceUri"] for p in placements}
        == {server.view_uri(view) for view in server.VIEWS}
        <= listed
    )
    # A sidebar entry is reachable only through its route; settings takes none.
    for placement in placements:
        assert ("route" in placement) == placement["slot"].startswith("sidebar.")
