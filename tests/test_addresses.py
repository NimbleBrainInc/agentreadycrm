"""A contact, an organization and an opportunity each have one address, readable, on every record.

The agent learns a record's address from a tool result and hands it to the host, which opens
People's view at it; the UI resolves it because it reports the same value in its trail. So the
address on a record, the resource served at it, and the shape the UI parses must be one value.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastmcp import Client

import mcp_people.server as server
from mcp_people.addresses import record_uri

pytestmark = pytest.mark.asyncio


@pytest.fixture
def headers(app_engine) -> dict[str, str]:
    """The tools act as the local runtime's one principal; this suite needs no identity
    of its own, so the mapping is empty."""
    return {}


async def test_the_shapes_the_ui_parses():
    assert record_uri("ct_01ABC") == "people://contacts/ct_01ABC"
    assert record_uri("org_01ABC") == "people://organizations/org_01ABC"
    assert record_uri("op_01ABC") == "people://opportunities/op_01ABC"
    # Records another tool cites: no view, but an address and a resource.
    assert record_uri("sg_01ABC") == "people://signals/sg_01ABC"
    assert record_uri("pp_01ABC") == "people://proof-points/pp_01ABC"
    # Nothing cites an interaction, so it has no address.
    assert record_uri("ix_01ABC") is None
    assert record_uri("ct_") is None


async def _seed(headers: dict[str, str]) -> tuple[str, str, str]:
    org = await server.find_or_create_organization(name="Larkfield", domain="larkfield.example")
    org_id = org["organization"]["id"]
    contact = await server.find_or_create_contact(
        name="Dana Reyes", email="dana@larkfield.example", organization_id=org_id
    )
    opp = await server.create_opportunity(title="Larkfield pilot", organization_id=org_id)
    return contact["contact"]["id"], org_id, opp["opportunity"]["id"]


async def test_every_record_a_tool_returns_carries_its_address(headers):
    contact_id, org_id, opp_id = await _seed(headers)

    assert (await server.get_contact(contact_id=contact_id))["contact"]["uri"] == record_uri(
        contact_id
    )
    assert (await server.get_organization(organization_id=org_id))["organization"][
        "uri"
    ] == record_uri(org_id)
    assert (await server.get_opportunity(opportunity_id=opp_id))["opportunity"][
        "uri"
    ] == record_uri(opp_id)

    # A full row and a row projected to the asked-for columns both carry it.
    [full] = (await server.list_contacts())["contacts"]
    [projected] = (await server.list_contacts(columns=["name"]))["contacts"]
    assert full["uri"] == projected["uri"] == record_uri(contact_id)

    found = (await server.list_contacts(query="Dana"))["contacts"]
    assert [r["uri"] for r in found] == [record_uri(contact_id)]


async def test_each_address_reads_as_the_tool_that_returns_the_record(headers):
    contact_id, org_id, opp_id = await _seed(headers)
    reads = {
        record_uri(contact_id): await server.get_contact(contact_id=contact_id),
        record_uri(org_id): await server.get_organization(organization_id=org_id),
        record_uri(opp_id): await server.get_opportunity(opportunity_id=opp_id),
    }

    async with Client(server.mcp) as client:
        for uri, tool_result in reads.items():
            assert uri is not None
            [content] = await client.read_resource(uri)
            assert content.mime_type == "application/json"
            assert json.loads(getattr(content, "text", "")) == json.loads(
                json.dumps(tool_result, default=str)
            )


async def _seed_evidence(headers: dict[str, str]) -> tuple[str, str]:
    _, org_id, _ = await _seed(headers)
    await server.define_signal_type(key="hiring", label="Hiring")
    signal = await server.record_signal(
        organization_id=org_id,
        claim="Larkfield is hiring a finance systems lead.",
        evidence_quote="We are hiring a Finance Systems Lead.",
        source_url="https://larkfield.example/careers/1",
        signal_type="hiring",
        source_date=date(2026, 5, 4),
        event_date=date(2026, 2, 1),
    )
    proof = await server.record_proof_point(claim="Cut month-end close from 9 days to 3.")
    return signal["signal"]["id"], proof["proof_point"]["id"]


async def test_a_signal_and_a_proof_point_carry_an_address_that_reads_as_their_tool(headers):
    signal_id, proof_id = await _seed_evidence(headers)
    signal = await server.get_signal(signal_id=signal_id)
    proof = await server.get_proof_point(proof_point_id=proof_id)
    assert signal["signal"]["uri"] == record_uri(signal_id)
    assert proof["proof_point"]["uri"] == record_uri(proof_id)
    [listed] = (await server.list_signals())["signals"]
    assert listed["uri"] == record_uri(signal_id)
    [listed] = (await server.list_proof_points())["proof_points"]
    assert listed["uri"] == record_uri(proof_id)

    async with Client(server.mcp) as client:
        for uri, tool_result in ((record_uri(signal_id), signal), (record_uri(proof_id), proof)):
            assert uri is not None
            [content] = await client.read_resource(uri)
            assert json.loads(getattr(content, "text", "")) == json.loads(
                json.dumps(tool_result, default=str)
            )
        # The signal's address carries both dates, each as recorded.
        [content] = await client.read_resource(record_uri(signal_id) or "")
        payload = json.loads(getattr(content, "text", ""))["signal"]
        assert (payload["source_date"], payload["event_date"]) == ("2026-05-04", "2026-02-01")
