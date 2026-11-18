"""A contact or organization read carries its open deals, so one read shows the record whole.

``get_contact`` holds the newest open deals naming the person as their contact, and
``get_organization`` the newest open deals on the account: at most ``RECORD_OPEN_DEALS``, each
with its id, address, title, stage, value and currency, and ``open_opportunities_total`` counts
every open one, which ``list_opportunities`` lists. Won and lost deals are history and are left
out, as ``list_opportunities`` leaves them out by default.

Fixture companies are fictional.
"""

from __future__ import annotations

import pytest
from conftest import session_for

from mcp_people import server
from mcp_people.db import store
from mcp_people_local.runtime import LOCAL_SCOPE

pytestmark = pytest.mark.asyncio


async def _seed() -> tuple[str, str]:
    """Larkfield, with Dana Reyes, who is the contact on every deal."""
    async with session_for(LOCAL_SCOPE) as conn:
        org, _, _ = await store.find_or_create_organization(
            conn, LOCAL_SCOPE, name="Larkfield", domain="larkfield.example"
        )
        dana, _, _ = await store.find_or_create_contact(
            conn, LOCAL_SCOPE, name="Dana Reyes", organization_id=org.id
        )
    return org.id, dana.id


async def _deal(org_id: str, contact_id: str, title: str, **kwargs) -> str:
    async with session_for(LOCAL_SCOPE) as conn:
        deal, _ = await store.find_or_create_opportunity(
            conn,
            LOCAL_SCOPE,
            title=title,
            organization_id=org_id,
            primary_contact_id=contact_id,
            **kwargs,
        )
    return deal.id


async def test_a_contact_read_carries_its_open_deals(backend) -> None:
    org_id, dana = await _seed()
    deal = await _deal(org_id, dana, "Larkfield pilot", value=12000, currency="USD")
    await _deal(org_id, dana, "Larkfield 2025 renewal", stage="closed_won")

    result = await server.get_contact(contact_id=dana)

    assert isinstance(result, dict)
    assert result["open_opportunities"] == [
        {
            "id": deal,
            "uri": f"people://opportunities/{deal}",
            "title": "Larkfield pilot",
            "stage": "lead",
            "value": 12000,
            "currency": "USD",
        }
    ]
    assert result["open_opportunities_total"] == 1


async def test_an_organization_read_carries_its_open_deals(backend) -> None:
    org_id, dana = await _seed()
    deal = await _deal(org_id, dana, "Larkfield pilot")

    result = await server.get_organization(organization_id=org_id)

    assert isinstance(result, dict)
    assert [d["id"] for d in result["open_opportunities"]] == [deal]
    assert result["open_opportunities_total"] == 1


async def test_a_read_carries_the_newest_few_and_counts_them_all(backend) -> None:
    org_id, dana = await _seed()
    made = [await _deal(org_id, dana, f"Larkfield deal {n}") for n in range(7)]

    result = await server.get_contact(contact_id=dana)

    assert isinstance(result, dict)
    shown = [d["id"] for d in result["open_opportunities"]]
    assert len(shown) == server.RECORD_OPEN_DEALS == 5
    assert set(shown) <= set(made)
    assert result["open_opportunities_total"] == 7
    listed = await server.list_opportunities(primary_contact_id=dana, limit=5)
    assert isinstance(listed, dict)
    assert shown == [d["id"] for d in listed["opportunities"]]


async def test_a_record_with_no_deals_carries_none(backend) -> None:
    org_id, dana = await _seed()
    contact = await server.get_contact(contact_id=dana)
    org = await server.get_organization(organization_id=org_id)
    for result in (contact, org):
        assert isinstance(result, dict)
        assert (result["open_opportunities"], result["open_opportunities_total"]) == ([], 0)
