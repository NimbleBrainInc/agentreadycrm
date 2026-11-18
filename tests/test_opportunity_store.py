"""Opportunity store behavior — the happy paths.

Covers: idempotent create,
the frozen account edge, the stage transition + ``closed_at`` side effect and the
derived ``outcome``, that ``update_opportunity`` never moves the stage, the
interaction↔opportunity link and timeline, list filters, and ``pipeline_summary``.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import make_ctx, session_for

from mcp_people.db import store


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


async def test_create_and_get_defaults(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, action = await store.find_or_create_opportunity(conn, ctx, title="Acme renewal")
        got, counts = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
    assert action == "created"
    assert opp.id.startswith("op_")
    assert opp.stage == "lead"  # server_default
    assert opp.outcome == "open"
    assert got.title == "Acme renewal"
    assert counts == {"interactions": 0}


async def test_idempotency_key_converges(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        first, a1 = await store.find_or_create_opportunity(
            conn, ctx, title="Deal", idempotency_key="legacy_dl_1"
        )
        second, a2 = await store.find_or_create_opportunity(
            conn, ctx, title="Deal (retry)", idempotency_key="legacy_dl_1"
        )
    assert (a1, a2) == ("created", "matched")
    assert first.id == second.id  # one row, retry/import converges
    assert second.title == "Deal"  # the winner's fields, not the retry's


async def test_no_key_creates_distinct_rows(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        a, _ = await store.find_or_create_opportunity(conn, ctx, title="A")
        b, _ = await store.find_or_create_opportunity(conn, ctx, title="A")
    assert a.id != b.id  # no key → every call is a new opportunity


async def test_account_frozen_from_primary_contact(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        org, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example"
        )
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Buyer", email="buyer@acme.example", organization_id=org.id
        )
        opp, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Acme deal", primary_contact_id=contact.id
        )
    # No organization_id was passed — it is frozen from the contact's org.
    assert opp.primary_contact_id == contact.id
    assert opp.organization_id == org.id


async def test_set_stage_stamps_and_clears_closed_at_and_outcome(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Pipeline deal")
        won = await store.set_opportunity_stage(
            conn, ctx, opportunity_id=opp.id, stage="closed_won"
        )
        reopened = await store.set_opportunity_stage(
            conn, ctx, opportunity_id=opp.id, stage="negotiation"
        )
    assert won.stage == "closed_won"
    assert won.outcome == "won"
    assert won.closed_at is not None  # terminal stage stamps the close time
    assert reopened.outcome == "open"
    assert reopened.closed_at is None  # leaving a terminal stage clears it


async def test_update_does_not_touch_stage(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Deal", stage="qualified")
        updated = await store.update_opportunity(
            conn, ctx, opportunity_id=opp.id, value=5000.0, currency="USD"
        )
    assert updated.stage == "qualified"  # unchanged by update
    assert updated.value == 5000.0
    assert updated.currency == "USD"


async def test_interaction_links_to_opportunity_and_timeline(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Lead", email="lead@ex.example"
        )
        opp, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Deal", primary_contact_id=contact.id
        )
        await store.log_interaction(
            conn,
            ctx,
            contact_id=contact.id,
            kind="call",
            summary="Discovery call",
            opportunity_id=opp.id,
        )
        interactions, _ = await store.list_interactions(conn, ctx, opportunity_id=opp.id)
        _, counts = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
    assert len(interactions) == 1
    assert interactions[0].summary == "Discovery call"
    assert counts == {"interactions": 1}


async def test_list_filters_by_stage_outcome_and_org(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        org, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example"
        )
        await store.find_or_create_opportunity(conn, ctx, title="Lead deal", stage="lead")
        won, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Won deal", stage="closed_won", organization_id=org.id
        )
        by_stage, _ = await store.list_opportunities(conn, ctx, stage="lead")
        by_outcome, _ = await store.list_opportunities(conn, ctx, outcome="won")
        by_org, _ = await store.list_opportunities(conn, ctx, organization_id=org.id)
        by_org_won, _ = await store.list_opportunities(
            conn, ctx, organization_id=org.id, outcome="won"
        )
    assert [o.title for o in by_stage] == ["Lead deal"]
    assert [o.id for o in by_outcome] == [won.id]
    # Open by default: the account's won deal shows once the outcome is named.
    assert by_org == []
    assert [o.id for o in by_org_won] == [won.id]


async def test_pipeline_summary(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.find_or_create_opportunity(conn, ctx, title="L1", stage="lead")
        await store.find_or_create_opportunity(conn, ctx, title="L2", stage="lead", value=1000.0)
        await store.find_or_create_opportunity(
            conn, ctx, title="W", stage="closed_won", value=5000.0
        )
        await store.find_or_create_opportunity(conn, ctx, title="X", stage="closed_lost")
        summary = await store.pipeline_summary(conn, ctx)
    assert summary["by_stage"]["lead"]["count"] == 2
    assert summary["by_stage"]["lead"]["value_sum"] == 1000.0
    assert summary["open_count"] == 2
    assert summary["won_count"] == 1
    assert summary["lost_count"] == 1
    assert summary["total_value"] == 6000.0


async def test_out_of_range_numbers_rejected(app_engine) -> None:
    """probability (0-100) and value (>= 0) are bounded quantities — a bad figure is
    rejected at the write seam as a ValueError (→ invalid_request), not stored."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError):
            await store.find_or_create_opportunity(conn, ctx, title="Bad", probability=150)
        with pytest.raises(ValueError):
            await store.find_or_create_opportunity(conn, ctx, title="Bad", value=-1.0)
        # A valid boundary value is accepted.
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Good", probability=100)
        assert opp.probability == 100
        with pytest.raises(ValueError):
            await store.update_opportunity(conn, ctx, opportunity_id=opp.id, probability=-5)
