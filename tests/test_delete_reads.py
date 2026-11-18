"""No read finds a deleted record — every read tool and every record
address, through a real MCP client.

A person and a company are recorded with values that only they hold, each carrying a canary,
then deleted. The values stay in the database; what this pins is that no read returns them.
Every read tool the server declares is called with arguments that would surface them (their
ids, their names, the broad listings, the change read): no result carries a canary or a
deleted record's id, a by-id read is not found, and a record that stays reads its link to a
deleted one as absent. The set of tools called is checked against the server's read-only
tools, so a read added later fails here until it is covered.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from mcp_people.server import mcp as server

pytestmark = pytest.mark.asyncio

CANARIES = ("canary", "555-0199")


@pytest.fixture
def headers(app_engine) -> dict[str, str]:
    """The tools act as the local runtime's one principal; this suite needs no identity
    of its own, so the mapping is empty."""
    return {}


async def _call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args, raise_on_error=False)
    payload = result.structured_content
    assert isinstance(payload, dict), (tool, result)
    return payload


async def _seed(client: Client) -> dict[str, Any]:
    """A person at a company that stays, and a company of their own to delete, with every
    kind of record either can hold."""
    await _call(client, "define_signal_type", key="hiring", label="Hiring")
    await _call(
        client, "define_field", entity="contact", key="nickname", label="Nickname", type="text"
    )
    canary = (
        await _call(
            client,
            "find_or_create_organization",
            name="Canary Corp",
            domain="canarycorp.example",
            industry="Canary widgets",
            external_refs=["vendor://companies/canary"],
        )
    )["organization"]["id"]
    globex = (
        await _call(client, "find_or_create_organization", name="Globex", domain="globex.example")
    )["organization"]["id"]
    await _call(
        client,
        "link_similar_organizations",
        organization_id=globex,
        companies=[{"domain": "canarycorp.example", "name": "Canary Corp", "similarity": 0.9}],
    )
    await _call(
        client,
        "link_similar_organizations",
        organization_id=canary,
        companies=[{"domain": "initech.example", "name": "Initech", "similarity": 0.5}],
    )
    jane = (
        await _call(
            client,
            "find_or_create_contact",
            name="Janeway Canary",
            email="janeway.canary@globex.example",
            phone="555-0199",
            title="Canary wrangler",
            organization_id=globex,
            external_refs=["vendor://people/canary"],
        )
    )["contact"]["id"]
    await _call(client, "update_contact", contact_id=jane, fields={"nickname": "canary-nick"})
    await _call(client, "update_contact", contact_id=jane, dossier="Canary likes tea.")
    deal = (
        await _call(
            client,
            "create_opportunity",
            title="Globex rollout",
            organization_id=globex,
            primary_contact_id=jane,
        )
    )["opportunity"]["id"]
    canary_deal = (
        await _call(client, "create_opportunity", title="Canary deal", organization_id=canary)
    )["opportunity"]["id"]
    touch = (
        await _call(
            client,
            "log_interaction",
            contact_id=jane,
            kind="email",
            direction="outbound",
            summary="Canary intro",
            body="Asked Canary about the rollout.",
            opportunity_id=deal,
        )
    )["interaction"]["id"]
    about_jane = (
        await _call(
            client,
            "record_signal",
            organization_id=globex,
            contact_id=jane,
            claim="Janeway Canary joined Globex.",
            evidence_quote="Globex hires Janeway Canary.",
            source_url="https://news.example/canary-joins",
            signal_type="hiring",
        )
    )["signal"]["id"]
    about_canary = (
        await _call(
            client,
            "record_signal",
            organization_id=canary,
            claim="Canary Corp is hiring.",
            evidence_quote="Canary Corp is hiring.",
            source_url="https://news.example/canary-hiring",
            signal_type="hiring",
        )
    )["signal"]["id"]
    for_canary = (
        await _call(
            client,
            "record_signal",
            organization_id=globex,
            claim="Globex opened an office.",
            evidence_quote="Globex opened an office.",
            source_url="https://news.example/globex-office",
            signal_type="hiring",
            evidence_for=[canary],
        )
    )["signal"]["id"]
    proof = (
        await _call(
            client,
            "record_proof_point",
            claim="Halved Canary Corp's onboarding.",
            organization_id=canary,
            engagement="Canary Corp",
        )
    )["proof_point"]["id"]
    return {
        "canary": canary,
        "globex": globex,
        "jane": jane,
        "deal": deal,
        "canary_deal": canary_deal,
        "touch": touch,
        "about_jane": about_jane,
        "about_canary": about_canary,
        "for_canary": for_canary,
        "proof": proof,
    }


async def test_no_read_tool_or_address_finds_a_deleted_record(headers) -> None:
    async with Client(server) as client:
        ids = await _seed(client)
        deleted_ids = [
            ids[k]
            for k in (
                "canary",
                "jane",
                "canary_deal",
                "touch",
                "about_jane",
                "about_canary",
                "proof",
            )
        ]
        done = await _call(client, "delete_contact", contact_id=ids["jane"], confirm=ids["jane"])
        assert "error" not in done, done
        done = await _call(
            client,
            "delete_organization",
            organization_id=ids["canary"],
            confirm=ids["canary"],
            delete_opportunities=True,
        )
        assert "error" not in done, done

        called: set[str] = set()
        results: list[tuple[str, dict[str, Any]]] = []

        async def read(tool: str, **args: Any) -> dict[str, Any]:
            called.add(tool)
            payload = await _call(client, tool, **args)
            results.append((tool, payload))
            return payload

        # By id: each deleted record is not found.
        for tool, arg, key in (
            ("get_contact", "contact_id", "jane"),
            ("list_interactions", "contact_id", "jane"),
            ("get_organization", "organization_id", "canary"),
            ("list_interactions", "organization_id", "canary"),
            ("get_pipeline", "organization_id", "canary"),
            ("list_similar_organizations", "organization_id", "canary"),
            ("get_opportunity", "opportunity_id", "canary_deal"),
            ("list_interactions", "opportunity_id", "canary_deal"),
            ("get_signal", "signal_id", "about_jane"),
            ("get_signal", "signal_id", "about_canary"),
            ("get_proof_point", "proof_point_id", "proof"),
        ):
            payload = await read(tool, **{arg: ids[key]})
            assert payload.get("error", {}).get("code") == "not_found", (tool, payload)

        # The records that stay, read through every path that reached the deleted ones.
        await read("get_organization", organization_id=ids["globex"])
        await read("list_interactions", organization_id=ids["globex"])
        await read("list_contacts", organization_id=ids["globex"], query="Jane")
        await read("list_similar_organizations", organization_id=ids["globex"])
        deal = await read("get_opportunity", opportunity_id=ids["deal"])
        assert deal["opportunity"].get("primary_contact_id") is None
        await read("list_interactions", opportunity_id=ids["deal"])
        survivor = await read("get_signal", signal_id=ids["for_canary"])
        assert survivor["signal"].get("evidence_for", []) == []

        # Listings, searches and counts, with every filter that names them.
        for status in (None, "sourced"):
            await read("list_contacts", **({"status": status} if status else {}))
        await read("list_contacts", include_archived=True)
        await read("list_contacts", organization_id=ids["globex"])
        await read("list_contacts", filter={"field": "nickname", "op": "is_set", "value": True})
        for status in (None, "active", "discovered"):
            await read("list_organizations", **({"status": status} if status else {}))
        await read("list_organizations", include_archived=True)
        for query in ("Canary", "Janeway", "555-0199", "canarycorp"):
            await read("list_contacts", query=query)
            await read("list_organizations", query=query)
            await read("list_organizations", query=query, status="discovered")
            await read("list_opportunities", query=query, outcome="open")
        # Rollups, read as a column (a per-row count) and as a filter (a joined aggregate):
        # Globex holds one live signal, and the deal no live interaction.
        orgs = await read(
            "list_organizations", columns=["name", "signals.active", "contacts.count"]
        )
        [globex] = [o for o in orgs["organizations"] if o["id"] == ids["globex"]]
        assert (globex["signals.active"], globex["contacts.count"]) == (1, 0)
        orgs = await read(
            "list_organizations", filter={"field": "signals.active", "op": "eq", "value": 1}
        )
        assert [o["id"] for o in orgs["organizations"]] == [ids["globex"]]
        deals = await read("list_opportunities", columns=["title", "interactions.count"])
        assert [d["interactions.count"] for d in deals["opportunities"]] == [0]
        deals = await read(
            "list_opportunities", filter={"field": "interactions.count", "op": "gt", "value": 0}
        )
        assert deals["opportunities"] == []
        await read("list_opportunities")
        await read("list_opportunities", organization_id=ids["canary"])
        await read("list_opportunities", primary_contact_id=ids["jane"])

        await read("list_signals", status="any")
        await read("list_signals", contact_id=ids["jane"], status="any")
        await read("list_signals", organization_id=ids["canary"], status="any")
        await read("list_signals", evidence_for=ids["canary"], status="any")
        await read("list_signals", changed_since="", status="any")
        await read("list_proof_points")
        await read("list_proof_points", organization_id=ids["canary"])
        for entity, field in (
            ("contact", "title"),
            ("contact", "nickname"),
            ("organization", "industry"),
            ("opportunity", "stage"),
        ):
            counted = await read("count_records", entity=entity, field=field)
            assert "error" not in counted, counted
        await read("list_fields")
        await read("list_fields", include_retired=True)
        pipeline = await read("get_pipeline")
        assert sum(s["deals"] for s in pipeline["stages"]) == 1
        types = await read("list_signal_types")
        assert {t["key"]: t["signal_count"] for t in types["signal_types"]} == {"hiring": 1}
        pipeline = await read("get_pipeline", include_retired=True)
        assert sum(s["deals"] for s in pipeline["stages"]) == 1
        await read("list_signal_types", include_retired=True)
        found = await read(
            "resolve_external_refs", refs=["vendor://companies/canary", "vendor://people/canary"]
        )
        assert found["found"] == []

        # Every record address of a deleted record is unreadable.
        for uri in (
            f"people://contacts/{ids['jane']}",
            f"people://organizations/{ids['canary']}",
            f"people://opportunities/{ids['canary_deal']}",
            f"people://signals/{ids['about_jane']}",
            f"people://proof-points/{ids['proof']}",
        ):
            with pytest.raises(MCPError, match="No .* found"):
                await client.read_resource(uri)

        reads = {t.name for t in await client.list_tools() if t.annotations.readOnlyHint}

    assert called == reads, f"read tools not covered: {sorted(reads - called)}"
    for tool, payload in results:
        if payload.get("error", {}).get("code") == "not_found":
            continue
        # A lookup names back what it was asked and did not find.
        text = json.dumps({k: v for k, v in payload.items() if k != "missing"}).lower()
        leaked = [c for c in CANARIES if c in text] + [i for i in deleted_ids if i.lower() in text]
        assert not leaked, (tool, leaked, payload)


async def test_no_read_finds_a_detached_reference_or_an_edge_a_merge_dropped(headers) -> None:
    """The link rows that go away are marked like records: a detached reference and the
    lookalike edges a merge drops stay in the database, and no read returns them."""
    detached = "vendor://companies/canary-detached"
    async with Client(server) as client:
        seed = (
            await _call(client, "find_or_create_organization", name="Seed", domain="seed.example")
        )["organization"]["id"]
        keep = (
            await _call(
                client,
                "find_or_create_organization",
                name="Keep",
                domain="keep.example",
                external_refs=[detached],
            )
        )["organization"]["id"]
        jane = (
            await _call(
                client,
                "find_or_create_contact",
                name="Jane Keep",
                email="jane@keep.example",
                external_refs=["vendor://people/canary-detached"],
            )
        )["contact"]["id"]
        await _call(
            client,
            "link_similar_organizations",
            organization_id=seed,
            companies=[
                {"domain": "keep.example", "name": "Keep", "similarity": 0.4},
                {"domain": "canary-dup.example", "name": "Canary Dup", "similarity": 0.9},
            ],
        )
        similar = await _call(client, "list_similar_organizations", organization_id=seed)
        dup = next(s["organization"]["id"] for s in similar["similar"] if s["similarity"] == 0.9)

        for tool, arg, record in (
            ("update_organization", "organization_id", keep),
            ("update_contact", "contact_id", jane),
        ):
            refs = (
                [detached] if tool == "update_organization" else ["vendor://people/canary-detached"]
            )
            done = await _call(client, tool, **{arg: record}, remove_external_refs=refs)
            assert "error" not in done, done
        merged = await _call(client, "merge_organizations", target_id=keep, source_id=dup)
        assert merged["edges_dropped"] == 1, merged

        org = await _call(client, "get_organization", organization_id=keep)
        person = await _call(client, "get_contact", contact_id=jane)
        found = await _call(
            client,
            "resolve_external_refs",
            refs=[detached, "vendor://people/canary-detached"],
        )
        similar = await _call(client, "list_similar_organizations", organization_id=seed)
        from_dup = await _call(client, "list_similar_organizations", organization_id=dup)

    assert org.get("external_refs", []) == [] and org.get("external_refs_total", 0) == 0
    assert person.get("external_refs", []) == []
    assert found["found"] == []
    # The seed lists the record kept once: its own edge stays, the source's is marked.
    assert [s["organization"]["id"] for s in similar["similar"]] == [keep]
    # The archived source keeps only its marked edges.
    assert from_dup["similar"] == []
    for payload in (org, person, similar):
        assert "canary" not in json.dumps(payload).lower(), payload
