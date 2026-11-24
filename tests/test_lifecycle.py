"""Lifecycle floor projection — ``effective_status``.

Three things are proven here:

1. the pure-Python **oracle** (:func:`lifecycle.effective_status`) on representative
   cases — each floor rule, "raise never lower", off-ladder passthrough;
2. that the production SQL ``CASE`` agrees with the oracle across the **full input
   matrix** (every status × every combination of the two fact flags) — the lockstep
   that lets the returned field and the ``list_contacts`` filter share one expression;
3. the end-to-end behavior through the store: an outbound touch ⇒ ``contacted``, an inbound touch ⇒ ``engaged``, ``note``/``research`` are not
   touches, ``archived`` is refused as a stage, and the worklist filter follows.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import literal, select

from mcp_people import lifecycle
from mcp_people.db import store
from mcp_people.fields import FieldError

_LADDER_STATUSES = ["sourced", "enriched", "contacted", "engaged"]
_STATUSES = [*_LADDER_STATUSES, "nurturing"]  # + engine-invented
_BOOLS = [False, True]


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# 1. Pure Python oracle                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "has_outbound", "has_inbound", "expected"),
    [
        ("sourced", False, False, "sourced"),  # no facts → unchanged
        ("sourced", True, False, "contacted"),  # outbound touch floor
        ("sourced", False, True, "engaged"),  # inbound touch floor
        ("sourced", True, True, "engaged"),  # highest applicable wins
        ("enriched", True, False, "contacted"),
        ("engaged", True, False, "engaged"),  # floor RAISES, never lowers
        ("nurturing", True, True, "nurturing"),  # engine-invented → passthrough
    ],
)
def test_effective_status_oracle(
    status: str, has_outbound: bool, has_inbound: bool, expected: str
) -> None:
    assert (
        lifecycle.effective_status(status, has_outbound=has_outbound, has_inbound=has_inbound)
        == expected
    )


# --------------------------------------------------------------------------- #
# 2. Production SQL == oracle, across the full input matrix                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", _STATUSES)
async def test_sql_matches_oracle(status: str, app_engine) -> None:
    """For every (status, outbound?, inbound?), the SQL ``CASE`` and the
    Python oracle produce the same stage — so the SELECT column and the filter
    (the same expression) are provably correct."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        for has_outbound in _BOOLS:
            for has_inbound in _BOOLS:
                sql_val = (
                    await conn.execute(
                        select(
                            lifecycle.effective_status_sql(
                                literal(status), literal(has_outbound), literal(has_inbound)
                            )
                        )
                    )
                ).scalar_one()
                oracle = lifecycle.effective_status(
                    status, has_outbound=has_outbound, has_inbound=has_inbound
                )
                assert sql_val == oracle, (
                    f"{status} o={has_outbound} i={has_inbound}: "
                    f"SQL {sql_val!r} != oracle {oracle!r}"
                )


# --------------------------------------------------------------------------- #
# 3. End-to-end through the store                                             #
# --------------------------------------------------------------------------- #


async def test_touches_floor_to_contacted_then_engaged_and_move_worklist(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        c, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Lead", email="lead@ex.example"
        )
    assert (c.status, c.effective_status) == ("sourced", "sourced")

    # Outbound email → contacted, though nobody wrote status.
    async with session_for(ctx) as conn:
        await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="email", summary="Reached out", direction="outbound"
        )
        got, _ = await store.get_contact(conn, ctx, contact_id=c.id)
    assert got.status == "sourced"
    assert got.effective_status == "contacted"

    # Their reply (inbound) → engaged.
    async with session_for(ctx) as conn:
        await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="email", summary="They replied", direction="inbound"
        )
        got, _ = await store.get_contact(conn, ctx, contact_id=c.id)
    assert got.effective_status == "engaged"

    # The worklist filters on the effective value.
    async with session_for(ctx) as conn:
        engaged, _ = await store.list_contacts(conn, ctx, status="engaged")
        sourced, _ = await store.list_contacts(conn, ctx, status="sourced")
    assert c.id in {x.id for x in engaged}
    assert c.id not in {x.id for x in sourced}


async def test_note_and_research_are_not_touches(app_engine) -> None:
    """Only email/call/meeting count — internal note/research never floor."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        c, _, _ = await store.find_or_create_contact(conn, ctx, name="N", email="n@ex.example")
        await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="note", summary="internal", direction="outbound"
        )
        await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="research", summary="prep", direction="inbound"
        )
        got, _ = await store.get_contact(conn, ctx, contact_id=c.id)
    assert got.effective_status == "sourced"


async def test_archived_is_not_a_stage(app_engine) -> None:
    """Archiving is its own flag, so ``archived`` is refused as a stage, on create and update."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(FieldError, match="archived is not a stage") as caught:
            await store.find_or_create_contact(
                conn, ctx, name="Dupe", email="dupe@ex.example", status="Archived"
            )
        assert "archive_contact" in caught.value.next_step
        c, _, _ = await store.find_or_create_contact(conn, ctx, name="Dupe", email="d@ex.example")
        with pytest.raises(FieldError, match="archived is not a stage"):
            await store.update_contact(conn, ctx, contact_id=c.id, status="archived")
