"""What a merge moves beyond contacts, interactions and deals: the evidence.

``merge_organizations`` moves the duplicate's signals and proof points to the record kept, and
makes every signal filed as evidence for the duplicate evidence for the record kept. A signal
both hold (the ``uq_signal_source`` key: organization, source URL, type) is kept once, with
the purposes of both. ``merge_contacts`` moves the signals about the duplicate and the deals
naming it as their contact. Reads of the record kept then see all of it.

Fixture companies are fictional.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import define_signal_types, make_ctx, session_for
from sqlalchemy import select

from mcp_people import server
from mcp_people.db import store
from mcp_people.db.tables import signal, signal_evidence_for

pytestmark = pytest.mark.asyncio

POSTING = "https://careers.northwind.example/ae"


@pytest.fixture
def ctx(backend, app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _org(ctx, name: str, domain: str) -> str:
    async with session_for(ctx) as conn:
        org, _action, _promoted = await store.find_or_create_organization(
            conn, ctx, name=name, domain=domain
        )
    return org.id


async def _signal(ctx, org_id: str, *, url: str = POSTING, evidence_for=None, contact_id=None):
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
        recorded, _action, _stored = await store.record_signal(
            conn,
            ctx,
            organization_id=org_id,
            contact_id=contact_id,
            claim="Northwind is hiring an Account Executive.",
            evidence_quote="We are hiring an Account Executive for the western region.",
            source_url=url,
            signal_type="hiring",
            evidence_for=evidence_for,
        )
    return recorded


async def _merge_orgs(ctx, target_id: str, source_id: str) -> dict:
    async with session_for(ctx) as conn:
        _org_kept, summary = await store.merge_organizations(
            conn, ctx, target_id=target_id, source_id=source_id
        )
    return summary


async def _signals(ctx, **kwargs):
    async with session_for(ctx) as conn:
        rows, _cursor = await store.list_signals(conn, ctx, **kwargs)
    return rows


async def _edges(ctx, signal_id: str) -> dict[str | None, dict]:
    async with session_for(ctx) as conn:
        rows = (
            (
                await conn.execute(
                    select(signal_evidence_for).where(signal_evidence_for.c.signal_id == signal_id)
                )
            )
            .mappings()
            .all()
        )
    return {r["for_organization_id"]: dict(r) for r in rows}


async def _signal_row(ctx, signal_id: str) -> dict:
    async with session_for(ctx) as conn:
        row = (await conn.execute(select(signal).where(signal.c.id == signal_id))).mappings().one()
    return dict(row)


@pytest.fixture
async def dupes(ctx):
    """Northwind (kept), its duplicate Northwind Inc, and Contoso, a client researched for."""
    keep = await _org(ctx, "Northwind", "northwind.example")
    dupe = await _org(ctx, "Northwind Inc", "northwind-inc.example")
    contoso = await _org(ctx, "Contoso", "contoso.example")
    return keep, dupe, contoso


# --------------------------------------------------------------------------- #
# merge_organizations                                                          #
# --------------------------------------------------------------------------- #


async def test_the_duplicates_signals_and_proof_points_move_to_the_record_kept(ctx, dupes):
    keep, dupe, _contoso = dupes
    fact = await _signal(ctx, dupe)
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(
            conn, ctx, claim="Cut Northwind's onboarding time in half.", organization_id=dupe
        )

    summary = await _merge_orgs(ctx, keep, dupe)

    assert (summary["signals_moved"], summary["proof_points_moved"]) == (1, 1)
    assert [s.id for s in await _signals(ctx, organization_id=keep)] == [fact.id]
    assert await _signals(ctx, organization_id=dupe) == []
    async with session_for(ctx) as conn:
        kept, _ = await store.list_proof_points(conn, ctx, organization_id=keep)
        left, _ = await store.list_proof_points(conn, ctx, organization_id=dupe)
    assert ([p.id for p in kept], left) == ([proof.id], [])


async def test_evidence_for_the_duplicate_becomes_evidence_for_the_record_kept(ctx, dupes):
    keep, dupe, contoso = dupes
    # A fact about Contoso, researched for the duplicate.
    fact = await _signal(ctx, contoso, evidence_for=[dupe])

    summary = await _merge_orgs(ctx, keep, dupe)

    assert summary["evidence_moved"] == 1
    assert [s.evidence_for for s in await _signals(ctx, organization_id=contoso)] == [[keep]]
    assert [s.id for s in await _signals(ctx, evidence_for=keep)] == [fact.id]
    assert await _signals(ctx, evidence_for=dupe) == []
    # The duplicate's edge is kept as removed, so a reader of its changes sees it leave.
    edges = await _edges(ctx, fact.id)
    assert edges[dupe]["removed_at"] is not None
    assert edges[keep]["removed_at"] is None


async def test_a_signal_already_evidence_for_both_keeps_one_edge_to_the_record_kept(ctx, dupes):
    keep, dupe, contoso = dupes
    fact = await _signal(ctx, contoso, evidence_for=[keep, dupe])

    await _merge_orgs(ctx, keep, dupe)

    [read] = await _signals(ctx, organization_id=contoso)
    assert read.evidence_for == [keep]
    assert set(await _edges(ctx, fact.id)) == {keep, dupe}


async def test_a_signal_both_hold_is_kept_once_with_the_purposes_of_both(ctx, dupes):
    keep, dupe, contoso = dupes
    kept = await _signal(ctx, keep, evidence_for=["workspace"])
    folded = await _signal(ctx, dupe, evidence_for=[contoso])

    summary = await _merge_orgs(ctx, keep, dupe)

    assert (summary["signals_dropped"], summary["signals_moved"]) == (1, 0)
    [read] = await _signals(ctx, organization_id=keep)
    assert read.id == kept.id
    assert read.evidence_for == ["workspace", contoso]
    # The duplicate's copy is marked, never removed, and keeps the organization it was on.
    row = await _signal_row(ctx, folded.id)
    assert row["deleted_at"] is not None
    assert row["organization_id"] == dupe


async def test_an_active_signal_is_kept_over_a_withdrawn_copy_of_it(ctx, dupes):
    keep, dupe, _contoso = dupes
    withdrawn = await _signal(ctx, keep, evidence_for=["workspace"])
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=withdrawn.id, reason="stale")
    active = await _signal(ctx, dupe)

    summary = await _merge_orgs(ctx, keep, dupe)

    assert (summary["signals_dropped"], summary["signals_moved"]) == (1, 1)
    [read] = await _signals(ctx, organization_id=keep)
    assert read.id == active.id
    assert read.evidence_for == ["workspace"]
    assert (await _signal_row(ctx, withdrawn.id))["deleted_at"] is not None


async def test_distinct_signals_both_survive(ctx, dupes):
    keep, dupe, _contoso = dupes
    await _signal(ctx, keep)
    await _signal(ctx, dupe, url="https://news.northwind.example/expansion")

    summary = await _merge_orgs(ctx, keep, dupe)

    assert (summary["signals_dropped"], summary["signals_moved"]) == (0, 1)
    assert len(await _signals(ctx, organization_id=keep)) == 2


async def test_the_tool_reports_what_the_evidence_merge_moved(ctx, dupes):
    keep, dupe, contoso = dupes
    await _signal(ctx, dupe)
    await _signal(ctx, contoso, evidence_for=[dupe])

    result = await server.merge_organizations(target_id=keep, source_id=dupe)

    assert isinstance(result, dict)
    assert {k: result[k] for k in ("signals_moved", "signals_dropped", "evidence_moved")} == {
        "signals_moved": 1,
        "signals_dropped": 0,
        "evidence_moved": 1,
    }
    assert result["proof_points_moved"] == 0


# --------------------------------------------------------------------------- #
# merge_contacts                                                               #
# --------------------------------------------------------------------------- #


async def test_signals_about_the_duplicate_and_its_deals_move_to_the_person_kept(ctx, dupes):
    keep_org, _dupe_org, _contoso = dupes
    async with session_for(ctx) as conn:
        dana, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana Reyes", email="dana@northwind.example", organization_id=keep_org
        )
        stub, _, _ = await store.find_or_create_contact(conn, ctx, name="D. Reyes")
        deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Northwind pilot", organization_id=keep_org, primary_contact_id=stub.id
        )
    fact = await _signal(ctx, keep_org, contact_id=stub.id)

    result = await server.merge_contacts(target_id=dana.id, source_id=stub.id)

    assert isinstance(result, dict)
    assert (result["signals_moved"], result["opportunities_moved"]) == (1, 1)
    assert [s.id for s in await _signals(ctx, contact_id=dana.id)] == [fact.id]
    assert await _signals(ctx, contact_id=stub.id) == []
    async with session_for(ctx) as conn:
        read, _counts = await store.get_opportunity(conn, ctx, opportunity_id=deal.id)
    assert read.primary_contact_id == dana.id
