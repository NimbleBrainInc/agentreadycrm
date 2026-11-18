"""The notification that says People data changed, through a real MCP client.

Driven through a real MCP client connected in memory, so what is asserted is
what the server sends (how it travels over HTTP is ``nimblebrain-server-sdk``'s test):
every tool that changes stored data sends one
``notifications/resources/list_changed`` on the session that made the call, and
every read sends none — including a read that writes an audit row. That last one
is the loop guard: a view re-lists when told the list changed, so a read that
announced would make every view refresh itself forever.

The tools act as the local runtime's one principal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import mcp.types
import pytest
from fastmcp import Client
from fastmcp.client.messages import MessageHandler

from mcp_people.server import mcp as server

pytestmark = pytest.mark.asyncio

Ids = dict[str, str]
Case = tuple[str, Callable[[Ids], dict[str, Any]]]


class Announcements(MessageHandler):
    """Every ``notifications/resources/list_changed`` the client received."""

    def __init__(self) -> None:
        self.received: list[mcp.types.ResourceListChangedNotification] = []

    async def on_resource_list_changed(
        self, message: mcp.types.ResourceListChangedNotification
    ) -> None:
        self.received.append(message)


@pytest.fixture
def headers(app_engine) -> dict[str, str]:
    """The tools act as the local runtime's one principal; this suite needs no identity
    of its own, so the mapping is empty."""
    return {}


@pytest.fixture
def announcements() -> Announcements:
    return Announcements()


async def _call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """The structured result of a call; a refusal is an error result carrying its shape."""
    result = await client.call_tool(tool, args, raise_on_error=False)
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert result.is_error == isinstance(payload.get("error"), dict)
    return payload


async def _seed(client: Client) -> Ids:
    """One of each entity, a duplicate contact and organization to merge, one
    defined field, and three signal types, one of them carried by no signal."""
    org = await _call(
        client, "find_or_create_organization", {"name": "Acme", "domain": "acme.example"}
    )
    dup_org = await _call(
        client,
        "find_or_create_organization",
        {"name": "Acme Holdings", "domain": "acme-holdings.example"},
    )
    org_id = org["organization"]["id"]
    contact = await _call(
        client,
        "find_or_create_contact",
        {"name": "Alice", "email": "alice@acme.example", "organization_id": org_id},
    )
    dup_contact = await _call(
        client, "find_or_create_contact", {"name": "Alice B", "email": "alice.b@acme.example"}
    )
    opp = await _call(
        client, "create_opportunity", {"title": "Acme deal", "organization_id": org_id}
    )
    for key in ("expansion", "hiring", "partnership"):
        await _call(client, "define_signal_type", {"key": key, "label": key.title()})
    signal = await _call(
        client,
        "record_signal",
        {
            "organization_id": org_id,
            "claim": "Acme opened a second distribution centre in Reno.",
            "evidence_quote": "Acme today opened its second distribution centre, in Reno.",
            "source_url": "https://press.acme.example/reno-dc",
            "signal_type": "expansion",
        },
    )
    proof = await _call(
        client,
        "record_proof_point",
        {"claim": "Brought first-response time under an hour.", "organization_id": org_id},
    )
    await _call(
        client,
        "define_field",
        {
            "entity": "organization",
            "key": "fit",
            "label": "Fit",
            "type": "select",
            "options": ["strong", "moderate"],
        },
    )
    return {
        "org": org_id,
        "dup_org": dup_org["organization"]["id"],
        "contact": contact["contact"]["id"],
        "dup_contact": dup_contact["contact"]["id"],
        "opp": opp["opportunity"]["id"],
        "signal": signal["signal"]["id"],
        "proof": proof["proof_point"]["id"],
    }


WRITES: dict[str, Case] = {
    "find_or_create_organization/created": (
        "find_or_create_organization",
        lambda ids: {"name": "Initech", "domain": "initech.example"},
    ),
    "find_or_create_organization/matched": (
        "find_or_create_organization",
        lambda ids: {"name": "Acme", "domain": "acme.example"},
    ),
    "find_or_create_contact": (
        "find_or_create_contact",
        lambda ids: {"name": "Bob", "email": "bob@acme.example"},
    ),
    "update_contact": (
        "update_contact",
        lambda ids: {"contact_id": ids["contact"], "title": "VP"},
    ),
    "define_field/created": (
        "define_field",
        lambda ids: {"entity": "contact", "key": "audience", "label": "Audience", "type": "text"},
    ),
    "define_field/matched": (
        "define_field",
        lambda ids: {
            "entity": "organization",
            "key": "fit",
            "label": "Fit",
            "type": "select",
            "options": ["strong", "no"],
        },
    ),
    "update_field/retired": (
        "update_field",
        lambda ids: {"entity": "organization", "key": "fit", "retired": True},
    ),
    "update_field/unchanged": (
        "update_field",
        lambda ids: {"entity": "organization", "key": "fit"},
    ),
    "define_pipeline_stage/created": (
        "define_pipeline_stage",
        lambda ids: {"key": "contract_sent", "label": "Contract sent", "probability": 90},
    ),
    "define_pipeline_stage/matched": (
        "define_pipeline_stage",
        lambda ids: {"key": "proposal", "label": "Proposal"},
    ),
    "update_pipeline_stage": (
        "update_pipeline_stage",
        lambda ids: {"key": "proposal", "probability": 60},
    ),
    "define_signal_type/created": (
        "define_signal_type",
        lambda ids: {"key": "funding_round", "label": "Funding round"},
    ),
    "define_signal_type/matched": (
        "define_signal_type",
        lambda ids: {"key": "hiring", "label": "Hiring"},
    ),
    "update_signal_type": (
        "update_signal_type",
        lambda ids: {"key": "hiring", "description": "A role the account is filling."},
    ),
    "update_organization/fields": (
        "update_organization",
        lambda ids: {"organization_id": ids["org"], "fields": {"fit": "strong"}},
    ),
    "merge_contacts": (
        "merge_contacts",
        lambda ids: {"target_id": ids["contact"], "source_id": ids["dup_contact"]},
    ),
    "merge_organizations": (
        "merge_organizations",
        lambda ids: {"target_id": ids["org"], "source_id": ids["dup_org"]},
    ),
    "update_organization": (
        "update_organization",
        lambda ids: {"organization_id": ids["org"], "region": "EMEA"},
    ),
    "archive_contact": (
        "archive_contact",
        lambda ids: {"contact_id": ids["dup_contact"]},
    ),
    "archive_contact/restore": (
        "archive_contact",
        lambda ids: {"contact_id": ids["contact"], "archived": False},
    ),
    "archive_organization": (
        "archive_organization",
        lambda ids: {"organization_id": ids["dup_org"]},
    ),
    "delete_contact": (
        "delete_contact",
        lambda ids: {"contact_id": ids["dup_contact"], "confirm": ids["dup_contact"]},
    ),
    "delete_organization": (
        "delete_organization",
        lambda ids: {"organization_id": ids["dup_org"], "confirm": ids["dup_org"]},
    ),
    "log_interaction": (
        "log_interaction",
        lambda ids: {
            "contact_id": ids["contact"],
            "kind": "call",
            "summary": "Intro call",
            "direction": "outbound",
        },
    ),
    "update_contact/dossier": (
        "update_contact",
        lambda ids: {"contact_id": ids["contact"], "dossier": "notes"},
    ),
    "update_organization/dossier_append": (
        "update_organization",
        lambda ids: {"organization_id": ids["org"], "dossier_append": "notes"},
    ),
    "link_similar_organizations": (
        "link_similar_organizations",
        lambda ids: {
            "organization_id": ids["org"],
            "companies": [{"domain": "globex.example", "name": "Globex"}],
        },
    ),
    "promote_organization": (
        "promote_organization",
        lambda ids: {"organization_id": ids["org"]},
    ),
    "import_organizations": (
        "import_organizations",
        lambda ids: {"rows": [{"name": "Initech", "domain": "initech.example"}]},
    ),
    "import_contacts": (
        "import_contacts",
        lambda ids: {"rows": [{"email": "peter@initech.example", "organization_id": ids["org"]}]},
    ),
    "create_opportunity": (
        "create_opportunity",
        lambda ids: {"title": "Expansion deal", "organization_id": ids["org"]},
    ),
    "update_opportunity": (
        "update_opportunity",
        lambda ids: {"opportunity_id": ids["opp"], "value": 5000.0},
    ),
    "move_opportunity": (
        "move_opportunity",
        lambda ids: {"opportunity_id": ids["opp"], "stage": "proposal"},
    ),
    "update_opportunity/dossier": (
        "update_opportunity",
        lambda ids: {"opportunity_id": ids["opp"], "dossier": "deal notes"},
    ),
    "record_signal": (
        "record_signal",
        lambda ids: {
            "organization_id": ids["org"],
            "claim": "Acme is hiring a head of logistics.",
            "evidence_quote": "We are hiring a Head of Logistics.",
            "source_url": "https://careers.acme.example/head-of-logistics",
            "signal_type": "hiring",
        },
    ),
    "withdraw_signal": (
        "withdraw_signal",
        lambda ids: {"signal_id": ids["signal"], "reason": "the quote was misread"},
    ),
    "update_signal/add": (
        "update_signal",
        lambda ids: {"signal_id": ids["signal"], "add_evidence_for": ["workspace"]},
    ),
    "update_signal/remove": (
        "update_signal",
        lambda ids: {"signal_id": ids["signal"], "remove_evidence_for": ["workspace"]},
    ),
    "record_proof_point": (
        "record_proof_point",
        lambda ids: {"claim": "Cut onboarding from two weeks to two days."},
    ),
    "verify_proof_point": (
        "verify_proof_point",
        lambda ids: {
            "proof_point_id": ids["proof"],
            "verification": "verified",
            "attested_by": "the delivery lead",
        },
    ),
}

READS: dict[str, Case] = {
    "get_contact": ("get_contact", lambda ids: {"contact_id": ids["contact"]}),
    "get_organization": ("get_organization", lambda ids: {"organization_id": ids["org"]}),
    "list_contacts": ("list_contacts", lambda ids: {}),
    "list_organizations": ("list_organizations", lambda ids: {}),
    "list_contacts/query": ("list_contacts", lambda ids: {"query": "Alice"}),
    "list_interactions/contact": ("list_interactions", lambda ids: {"contact_id": ids["contact"]}),
    "list_interactions/organization": (
        "list_interactions",
        lambda ids: {"organization_id": ids["org"]},
    ),
    "list_similar_organizations": (
        "list_similar_organizations",
        lambda ids: {"organization_id": ids["org"]},
    ),
    "get_opportunity": ("get_opportunity", lambda ids: {"opportunity_id": ids["opp"]}),
    "list_opportunities": ("list_opportunities", lambda ids: {}),
    "list_interactions/opportunity": (
        "list_interactions",
        lambda ids: {"opportunity_id": ids["opp"]},
    ),
    "get_pipeline/organization": ("get_pipeline", lambda ids: {"organization_id": ids["org"]}),
    "get_signal": ("get_signal", lambda ids: {"signal_id": ids["signal"]}),
    "list_signals": ("list_signals", lambda ids: {"organization_id": ids["org"]}),
    "list_signals/changed_since": ("list_signals", lambda ids: {"changed_since": ""}),
    "get_proof_point": ("get_proof_point", lambda ids: {"proof_point_id": ids["proof"]}),
    "list_proof_points": ("list_proof_points", lambda ids: {}),
    "list_fields": ("list_fields", lambda ids: {}),
    "list_fields/include_retired": (
        "list_fields",
        lambda ids: {"include_retired": True},
    ),
    "get_pipeline": ("get_pipeline", lambda ids: {}),
    "list_signal_types": ("list_signal_types", lambda ids: {}),
    "count_records": ("count_records", lambda ids: {"entity": "organization", "field": "fit"}),
    "resolve_external_refs": (
        "resolve_external_refs",
        lambda ids: {"refs": ["vendor://companies/1"]},
    ),
    "list_contacts/filtered": (
        "list_contacts",
        lambda ids: {
            "filter": {"field": "organization.fit", "op": "is_set", "value": False},
            "sort": "name",
            "columns": ["name"],
        },
    ),
    "list_contacts/bad_filter": ("list_contacts", lambda ids: {"filter": {"field": "nope"}}),
    # Reads that fail write an audit row. An audit row is not view data.
    "get_contact/not_found": ("get_contact", lambda ids: {"contact_id": "ct_absent"}),
    "list_contacts/bad_cursor": ("list_contacts", lambda ids: {"cursor": "not-a-cursor"}),
}

FAILED_WRITES: dict[str, Case] = {
    "delete_contact/unconfirmed": (
        "delete_contact",
        lambda ids: {"contact_id": ids["contact"], "confirm": ids["dup_contact"]},
    ),
    "delete_contact/not_found": (
        "delete_contact",
        lambda ids: {"contact_id": "ct_absent", "confirm": "ct_absent"},
    ),
    "delete_organization/has_contacts": (
        "delete_organization",
        lambda ids: {"organization_id": ids["org"], "confirm": ids["org"]},
    ),
    "update_contact": ("update_contact", lambda ids: {"contact_id": "ct_absent", "title": "VP"}),
    "merge_organizations": (
        "merge_organizations",
        lambda ids: {"target_id": ids["org"], "source_id": "org_absent"},
    ),
    "move_opportunity": (
        "move_opportunity",
        lambda ids: {"opportunity_id": "op_absent", "stage": "proposal"},
    ),
    "withdraw_signal": ("withdraw_signal", lambda ids: {"signal_id": "sg_absent"}),
    "update_signal/not_found": (
        "update_signal",
        lambda ids: {"signal_id": "sg_absent", "add_evidence_for": ["workspace"]},
    ),
    "update_signal/reserved_word": (
        "update_signal",
        lambda ids: {"signal_id": ids["signal"], "remove_evidence_for": ["unstated"]},
    ),
    "update_signal/nothing": ("update_signal", lambda ids: {"signal_id": ids["signal"]}),
    "update_contact/both_dossiers": (
        "update_contact",
        lambda ids: {"contact_id": ids["contact"], "dossier": "a", "dossier_append": "b"},
    ),
    "update_organization/undefined_field": (
        "update_organization",
        lambda ids: {"organization_id": ids["org"], "fields": {"undefined": 1}},
    ),
    "define_field/type_change": (
        "define_field",
        lambda ids: {"entity": "organization", "key": "fit", "label": "Fit", "type": "text"},
    ),
    "update_field/undefined": (
        "update_field",
        lambda ids: {"entity": "organization", "key": "undefined", "retired": True},
    ),
    "update_field/self_replacement": (
        "update_field",
        lambda ids: {"entity": "organization", "key": "fit", "retired": True, "replaced_by": "fit"},
    ),
    "move_opportunity/unknown_stage": (
        "move_opportunity",
        lambda ids: {"opportunity_id": ids["opp"], "stage": "not_a_stage"},
    ),
    "update_pipeline_stage/undefined": (
        "update_pipeline_stage",
        lambda ids: {"key": "absent", "label": "Absent"},
    ),
    "update_signal_type/undefined": (
        "update_signal_type",
        lambda ids: {"key": "absent", "label": "Absent"},
    ),
    "record_signal/undefined_type": (
        "record_signal",
        lambda ids: {
            "organization_id": ids["org"],
            "claim": "Acme signed a partner.",
            "evidence_quote": "Acme signed a partner.",
            "source_url": "https://press.acme.example/partner",
            "signal_type": "not_a_type",
        },
    ),
    "verify_proof_point": (
        "verify_proof_point",
        lambda ids: {
            "proof_point_id": "pp_absent",
            "verification": "verified",
            "attested_by": "the delivery lead",
        },
    ),
    "record_signal/invalid": (
        "record_signal",
        lambda ids: {
            "organization_id": ids["org"],
            "claim": "A claim.",
            "evidence_quote": "A quote.",
            "source_url": "not a url",
            "signal_type": "hiring",
        },
    ),
}


async def test_every_tool_is_classified_and_exactly_the_writers_announce():
    """A new tool fails here until it is annotated and placed in WRITES or READS.

    Under the spec's defaults a tool with no annotation reads as a destructive write, so
    every tool says whether it writes. The writers by annotation are exactly the tools the
    write cases prove announce, and the rest are exactly the tools the read cases prove
    stay silent.
    """
    tools = {t.name: t for t in await server.list_tools()}
    silent = sorted(
        n for n, t in tools.items() if t.annotations is None or t.annotations.read_only_hint is None
    )
    assert not silent, f"tools that do not say whether they write: {silent}"

    writes = {tool for tool, _ in WRITES.values()}
    reads = {tool for tool, _ in READS.values()}
    assert not writes & reads
    assert set(tools) == writes | reads
    hints = {n: t.annotations for n, t in tools.items() if t.annotations is not None}
    assert {n for n, a in hints.items() if not a.read_only_hint} == writes
    # A write that leaves `destructive_hint` unset reads, by the spec's default, as destructive.
    undecided = sorted(n for n in writes if hints[n].destructive_hint is None)
    assert not undecided, f"writes that do not say whether they destroy: {undecided}"
    # Every tool acts on this server's database and nothing else, and says whether a repeat
    # changes anything; the spec's defaults are open-world and not idempotent.
    open_world = sorted(n for n, a in hints.items() if a.open_world_hint is not False)
    assert not open_world, f"tools that do not say they are closed-world: {open_world}"
    unsaid = sorted(n for n, a in hints.items() if a.idempotent_hint is None)
    assert not unsaid, f"tools that do not say whether a repeat changes anything: {unsaid}"


@pytest.mark.parametrize("case", list(WRITES), ids=list(WRITES))
async def test_a_write_announces_once(case, headers, announcements):
    tool, args = WRITES[case]
    async with Client(server, message_handler=announcements) as client:
        ids = await _seed(client)
        announcements.received.clear()

        payload = await _call(client, tool, args(ids))

    assert "error" not in payload, payload
    assert len(announcements.received) == 1


@pytest.mark.parametrize("case", list(READS), ids=list(READS))
async def test_a_read_announces_nothing(case, headers, announcements):
    tool, args = READS[case]
    async with Client(server, message_handler=announcements) as client:
        ids = await _seed(client)
        announcements.received.clear()

        await _call(client, tool, args(ids))

    assert announcements.received == []


@pytest.mark.parametrize("case", list(FAILED_WRITES), ids=list(FAILED_WRITES))
async def test_a_failed_write_announces_nothing(case, headers, announcements):
    tool, args = FAILED_WRITES[case]
    async with Client(server, message_handler=announcements) as client:
        ids = await _seed(client)
        announcements.received.clear()

        payload = await _call(client, tool, args(ids))

    assert payload["error"]["code"] in {"not_found", "invalid_request", "conflict"}, payload
    assert announcements.received == []
