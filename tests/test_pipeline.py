"""The pipeline: a workspace's stages, what a deal may move to, and what it weighs.

Every stage write and every stage move goes through the store.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import select, update

from mcp_people.db import store
from mcp_people.db.tables import opportunity, people_audit
from mcp_people.fields import FieldError
from mcp_people.pipeline import DEFAULT_STAGES

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


async def _describe(ctx) -> list[dict]:
    async with session_for(ctx) as conn:
        return (await store.get_pipeline(conn, ctx))["stages"]


# --------------------------------------------------------------------------- #
# The default set                                                              #
# --------------------------------------------------------------------------- #


async def test_a_workspace_starts_with_the_default_stages(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    stages = await _describe(ctx)
    assert [(s["key"], s["label"], s["probability"], s["outcome"]) for s in stages] == list(
        DEFAULT_STAGES
    )
    assert [s["position"] for s in stages] == list(range(len(DEFAULT_STAGES)))
    assert all(s["deals"] == 0 for s in stages)
    # A second read seeds nothing more.
    assert [s["id"] for s in await _describe(ctx)] == [s["id"] for s in stages]


async def test_each_workspace_has_its_own_pipeline(app_engine) -> None:
    a = make_ctx(_tid(), workspace_id="ws_a")
    b = make_ctx(_tid(), workspace_id="ws_b")
    async with session_for(a) as conn:
        await store.define_pipeline_stage(
            conn, a, key="contract_sent", label="Contract sent", probability=90
        )
        await store.define_pipeline_stage(conn, a, key="proposal", label="Proposal out")
    assert "contract_sent" in {s["key"] for s in await _describe(a)}
    keys_b = {s["key"]: s for s in await _describe(b)}
    assert "contract_sent" not in keys_b
    assert keys_b["proposal"]["label"] == "Proposal"
    # The other workspace cannot move a deal into a stage it does not have.
    async with session_for(b) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, b, title="B deal")
        with pytest.raises(FieldError, match="not in this workspace's pipeline"):
            await store.set_opportunity_stage(conn, b, opportunity_id=opp.id, stage="contract_sent")


# --------------------------------------------------------------------------- #
# Defining and changing stages                                                 #
# --------------------------------------------------------------------------- #


async def test_define_adds_and_update_changes_only_what_is_given(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        stage, action = await store.define_pipeline_stage(
            conn, ctx, key="Contract_Sent", label="Contract sent", probability=90
        )
        assert (action, stage.key, stage.probability, stage.outcome, stage.position) == (
            "created",
            "contract_sent",
            90,
            "open",
            len(DEFAULT_STAGES),
        )
        # Defining a key the pipeline holds changes nothing.
        same, action = await store.define_pipeline_stage(
            conn, ctx, key="contract_sent", label="Other", probability=10
        )
        assert (action, same.id, same.label, same.probability) == (
            "matched",
            stage.id,
            "Contract sent",
            90,
        )
        moved, action = await store.update_pipeline_stage(
            conn, ctx, key="contract_sent", position=4, probability=85
        )
        assert (action, moved.label, moved.position, moved.probability) == (
            "updated",
            "Contract sent",
            4,
            85,
        )
        _, action = await store.update_pipeline_stage(
            conn, ctx, key="contract_sent", position=4, probability=85
        )
        assert action == "unchanged"
        with pytest.raises(FieldError, match="not in this workspace's pipeline") as caught:
            await store.update_pipeline_stage(conn, ctx, key="absent", label="X")
        assert "define_pipeline_stage" in caught.value.next_step
    keys = [s["key"] for s in await _describe(ctx)]
    assert keys.index("contract_sent") < keys.index("closed_won")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"key": "Bad Key", "label": "x"}, "lowercase letters"),
        ({"key": "fresh", "label": "x", "outcome": "maybe"}, "outcome must be one of"),
        ({"key": "fresh", "label": "x", "probability": 101}, "between 0 and 100"),
        ({"key": "fresh", "label": "x", "position": -1}, "position must be"),
    ],
)
async def test_define_refuses_a_bad_stage(app_engine, kwargs, message) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match=message):
            await store.define_pipeline_stage(conn, ctx, **kwargs)


async def test_the_pipeline_keeps_an_open_stage(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        for key in ("qualified", "proposal", "negotiation"):
            await store.update_pipeline_stage(conn, ctx, key=key, outcome="lost")
        with pytest.raises(FieldError, match="only open stage"):
            await store.update_pipeline_stage(conn, ctx, key="lead", outcome="won")
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="New")
    assert (opp.stage, opp.outcome) == ("lead", "open")


async def test_a_read_grant_does_not_borrow_another_workspaces_pipeline(app_engine) -> None:
    tid = _tid()
    ctx_a = make_ctx(tid, workspace_id="ws_a")
    async with session_for(ctx_a) as conn:
        await store.define_pipeline_stage(conn, ctx_a, key="contract_sent", label="Contract sent")
    ctx_b = make_ctx(tid, workspace_id="ws_b", allowed_workspaces={"ws_a"})
    stages = await _describe(ctx_b)
    assert [s["key"] for s in stages] == [key for key, *_ in DEFAULT_STAGES]
    async with session_for(ctx_b) as conn:
        with pytest.raises(FieldError, match="not in this workspace's pipeline"):
            await store.find_or_create_opportunity(conn, ctx_b, title="X", stage="contract_sent")


# --------------------------------------------------------------------------- #
# What a deal may move to, and what it weighs                                  #
# --------------------------------------------------------------------------- #


async def test_a_deal_takes_the_stages_probability_unless_it_has_its_own(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Weighted", value=1000)
        assert (opp.stage, opp.probability, opp.probability_is_own, opp.weighted_value) == (
            "lead",
            10,
            False,
            100.0,
        )
        opp = await store.set_opportunity_stage(conn, ctx, opportunity_id=opp.id, stage="proposal")
        assert (opp.probability, opp.probability_is_own, opp.weighted_value) == (50, False, 500.0)
        # A number set on the deal is its own, and a move clears it unless given again.
        opp = await store.update_opportunity(conn, ctx, opportunity_id=opp.id, probability=80)
        assert (opp.probability, opp.probability_is_own, opp.weighted_value) == (80, True, 800.0)
        opp = await store.set_opportunity_stage(
            conn, ctx, opportunity_id=opp.id, stage="negotiation"
        )
        assert (opp.probability, opp.probability_is_own) == (75, False)
        opp = await store.set_opportunity_stage(
            conn, ctx, opportunity_id=opp.id, stage="negotiation", probability=95
        )
        assert (opp.probability, opp.probability_is_own, opp.weighted_value) == (95, True, 950.0)
        # Reweighting the stage reaches the deals that take its default.
        other, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Default", value=200, stage="negotiation"
        )
        await store.update_pipeline_stage(conn, ctx, key="negotiation", probability=60)
        other, _ = await store.get_opportunity(conn, ctx, opportunity_id=other.id)
        again, _ = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
        assert (other.probability, other.weighted_value) == (60, 120.0)
        assert again.probability == 95


async def test_a_stage_outside_the_pipeline_is_refused_with_the_ones_inside(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(FieldError, match="not in this workspace's pipeline") as caught:
            await store.find_or_create_opportunity(conn, ctx, title="Nope", stage="won_big")
        assert "lead, qualified, proposal" in caught.value.next_step
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Ok", stage=" Proposal ")
        assert opp.stage == "proposal"
        with pytest.raises(FieldError, match="not in this workspace's pipeline"):
            await store.set_opportunity_stage(conn, ctx, opportunity_id=opp.id, stage="won_big")


async def test_the_outcome_and_close_time_come_from_the_stage(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Closing")
        assert (opp.outcome, opp.closed_at) == ("open", None)
        opp = await store.set_opportunity_stage(
            conn, ctx, opportunity_id=opp.id, stage="closed_won"
        )
        assert opp.outcome == "won" and opp.closed_at is not None
        opp = await store.set_opportunity_stage(conn, ctx, opportunity_id=opp.id, stage="qualified")
        assert (opp.outcome, opp.closed_at) == ("open", None)
        # A workspace's own closing stage closes too, and changing a stage's
        # outcome moves the close time of the deals it holds.
        await store.define_pipeline_stage(
            conn, ctx, key="signed", label="Signed", probability=100, outcome="won"
        )
        opp = await store.set_opportunity_stage(conn, ctx, opportunity_id=opp.id, stage="signed")
        assert opp.outcome == "won" and opp.closed_at is not None
        await store.update_pipeline_stage(conn, ctx, key="signed", outcome="open")
        opp, _ = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
        assert (opp.outcome, opp.closed_at) == ("open", None)
        await store.update_pipeline_stage(conn, ctx, key="signed", outcome="lost")
        opp, _ = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
        assert opp.outcome == "lost" and opp.closed_at is not None
        listed, _ = await store.list_opportunities(conn, ctx, outcome="lost")
        assert [o.id for o in listed] == [opp.id]


async def test_a_stage_no_row_holds_reads_open(app_engine) -> None:
    """Closing is data: a deal on a key the pipeline lacks reads open, even a key that
    sounds closed, in both the returned field and the outcome filter."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="Unranked")
        await conn.execute(
            update(opportunity).where(opportunity.c.id == opp.id).values(stage="won")
        )
        opp, _ = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
        assert (opp.stage, opp.outcome) == ("won", "open")
        open_ids = [o.id for o in (await store.list_opportunities(conn, ctx, outcome="open"))[0]]
        assert opp.id in open_ids
        won, _ = await store.list_opportunities(conn, ctx, outcome="won")
        assert won == []


# --------------------------------------------------------------------------- #
# Retiring a stage                                                             #
# --------------------------------------------------------------------------- #


async def test_a_retired_stage_leaves_the_pipeline_and_its_deals_keep_it(app_engine) -> None:
    """A lost stage retires with its deals: they keep the key and still read its outcome,
    the stage is listed only under include_retired, and the totals count its deals."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.define_pipeline_stage(conn, ctx, key="ghosted", label="Ghosted", outcome="lost")
        deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Gone quiet", stage="ghosted"
        )
        stage, action = await store.update_pipeline_stage(
            conn, ctx, key="ghosted", retired=True, replaced_by="closed_lost"
        )
        assert action == "retired"
        assert (stage.retired_at is not None, stage.replaced_by) == (True, "closed_lost")
        live = await store.get_pipeline(conn, ctx)
        every = await store.get_pipeline(conn, ctx, include_retired=True)
        held, _ = await store.get_opportunity(conn, ctx, opportunity_id=deal.id)
        lost, _ = await store.list_opportunities(conn, ctx, stage="ghosted")
    assert "ghosted" not in [s["key"] for s in live["stages"]]
    assert live["lost_count"] == 1
    assert [s["key"] for s in every["stages"]][-1] == "ghosted"
    assert every["stages"][-1]["deals"] == 1
    assert every["stages"][-1]["replaced_by"] == "closed_lost"
    assert (held.stage, held.outcome) == ("ghosted", "lost")
    assert [o.id for o in lost] == [deal.id]
    # The live stages keep their board positions 0..n-1; the retired one sorts after.
    assert [s["position"] for s in live["stages"]] == list(range(len(live["stages"])))
    assert every["stages"][-1]["position"] == len(live["stages"])


async def test_no_deal_enters_a_retired_stage_and_the_refusal_names_its_replacement(
    app_engine,
) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        deal, _ = await store.find_or_create_opportunity(conn, ctx, title="Moving")
        await store.update_pipeline_stage(
            conn, ctx, key="negotiation", retired=True, replaced_by="proposal"
        )
        with pytest.raises(FieldError, match="is retired") as caught:
            await store.set_opportunity_stage(
                conn, ctx, opportunity_id=deal.id, stage="negotiation"
            )
        assert "Use 'proposal', which replaces it." in caught.value.next_step
        assert "update_pipeline_stage(key='negotiation', retired=false)" in caught.value.next_step
        with pytest.raises(FieldError, match="is retired"):
            await store.find_or_create_opportunity(conn, ctx, title="New", stage="negotiation")
        # A replacement that retires later is followed to the live stage at the chain's end.
        await store.update_pipeline_stage(
            conn, ctx, key="proposal", retired=True, replaced_by="qualified"
        )
        with pytest.raises(FieldError) as caught:
            await store.set_opportunity_stage(
                conn, ctx, opportunity_id=deal.id, stage="negotiation"
            )
        assert "Use 'qualified'" in caught.value.next_step


async def test_a_retired_key_stays_taken_and_restoring_brings_it_back(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        first, _ = await store.update_pipeline_stage(conn, ctx, key="qualified", retired=True)
        with pytest.raises(FieldError, match="stays taken") as caught:
            await store.define_pipeline_stage(conn, ctx, key="qualified", label="Qualified")
        assert "update_pipeline_stage(key='qualified', retired=false)" in caught.value.next_step
        # Retiring again keeps the first stamp.
        again, action = await store.update_pipeline_stage(conn, ctx, key="qualified", retired=True)
        assert (again.retired_at, action) == (first.retired_at, "unchanged")
        restored, action = await store.update_pipeline_stage(
            conn, ctx, key="qualified", retired=False, position=1
        )
        assert (restored.retired_at, restored.replaced_by, action) == (None, None, "restored")
        stages = (await store.get_pipeline(conn, ctx))["stages"]
    assert [s["key"] for s in stages][:3] == ["lead", "qualified", "proposal"]


async def test_an_open_stage_holding_deals_cannot_retire(app_engine) -> None:
    """A board column would vanish under live work; once its deals move, it retires."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        deal, _ = await store.find_or_create_opportunity(conn, ctx, title="Live", stage="proposal")
        with pytest.raises(FieldError, match="holds 1 open deal") as caught:
            await store.update_pipeline_stage(conn, ctx, key="proposal", retired=True)
        assert "move_opportunity" in caught.value.next_step
        await store.set_opportunity_stage(conn, ctx, opportunity_id=deal.id, stage="closed_won")
        _, action = await store.update_pipeline_stage(conn, ctx, key="proposal", retired=True)
    assert action == "retired"


async def test_a_retired_stage_holding_closed_deals_cannot_reopen(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.find_or_create_opportunity(conn, ctx, title="Won", stage="closed_won")
        await store.update_pipeline_stage(conn, ctx, key="closed_won", retired=True)
        with pytest.raises(FieldError, match="open deal"):
            await store.update_pipeline_stage(conn, ctx, key="closed_won", outcome="open")
        with pytest.raises(FieldError, match="no place on the board"):
            await store.update_pipeline_stage(conn, ctx, key="closed_won", position=0)
        # Its label still changes, and its deals read the new one.
        _, action = await store.update_pipeline_stage(
            conn, ctx, key="closed_won", label="Won (old)"
        )
    assert action == "updated"


async def test_the_pipeline_keeps_a_live_open_stage(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        for key in ("qualified", "proposal", "negotiation"):
            await store.update_pipeline_stage(conn, ctx, key=key, retired=True)
        with pytest.raises(FieldError, match="only open stage"):
            await store.update_pipeline_stage(conn, ctx, key="lead", retired=True)
        # A new deal enters the first live open stage, never a retired one.
        await store.update_pipeline_stage(conn, ctx, key="qualified", retired=False, position=0)
        opp, _ = await store.find_or_create_opportunity(conn, ctx, title="New")
        assert opp.stage == "qualified"
        _, action = await store.update_pipeline_stage(conn, ctx, key="lead", retired=True)
    assert action == "retired"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"replaced_by": "lead"}, "only to a retired stage"),
        ({"retired": True, "replaced_by": "proposal"}, "cannot replace itself"),
        ({"retired": True, "replaced_by": "nowhere"}, "is not in this workspace's pipeline"),
        ({"retired": True, "replaced_by": "negotiation"}, "is retired"),
    ],
)
async def test_a_replacement_must_be_another_live_stage(app_engine, kwargs, message) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.update_pipeline_stage(conn, ctx, key="negotiation", retired=True)
        with pytest.raises(FieldError, match=message):
            await store.update_pipeline_stage(conn, ctx, key="proposal", **kwargs)


# --------------------------------------------------------------------------- #
# Custom fields on deals                                                       #
# --------------------------------------------------------------------------- #


async def test_a_deal_holds_custom_fields_like_a_contact(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.define_field(
            conn,
            ctx,
            entity="opportunity",
            key="champion",
            label="Champion identified",
            field_type="select",
            options=["Yes", "No"],
        )
        await store.define_field(
            conn,
            ctx,
            entity="opportunity",
            key="source_db",
            label="Source database",
            field_type="text",
        )
        with pytest.raises(FieldError, match="no opportunity field named 'undefined'"):
            await store.find_or_create_opportunity(conn, ctx, title="Deal", fields={"undefined": 1})
        opp, _ = await store.find_or_create_opportunity(
            conn,
            ctx,
            title="Deal",
            idempotency_key="deal-1",
            fields={"champion": "yes"},
            fields_by="research",
        )
        assert (opp.fields, opp.field_details) == ({"champion": "Yes"}, None)
        fetched, _ = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
        assert fetched.field_details is not None
        assert fetched.field_details["champion"]["by"] == "research"
        # A match fills only what the row lacks.
        again, action = await store.find_or_create_opportunity(
            conn,
            ctx,
            title="Deal",
            idempotency_key="deal-1",
            fields={"champion": "No", "source_db": "Atlas"},
        )
        assert (action, again.fields) == ("matched", {"champion": "Yes", "source_db": "Atlas"})
        # An update overwrites key by key and clears with null.
        opp = await store.update_opportunity(
            conn, ctx, opportunity_id=opp.id, fields={"champion": "No", "source_db": None}
        )
        assert opp.fields == {"champion": "No"}
        described = await store.describe_fields(conn, ctx)
        assert [f.key for f in described["opportunity"]] == ["champion", "source_db"]
        # The audit row names the keys, never the values.
        detail = (
            await conn.execute(
                select(people_audit.c.detail)
                .where(people_audit.c.entity_id == opp.id, people_audit.c.action == "update")
                .order_by(people_audit.c.id.desc())
            )
        ).scalar_one()
        assert detail == {"fields": ["champion", "source_db"]}


# --------------------------------------------------------------------------- #
# The summary a board draws                                                    #
# --------------------------------------------------------------------------- #


async def test_pipeline_summary_walks_the_stages_in_order_with_weighted_sums(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.find_or_create_opportunity(conn, ctx, title="a", stage="proposal", value=1000)
        await store.find_or_create_opportunity(
            conn, ctx, title="b", stage="proposal", value=500, probability=20
        )
        await store.find_or_create_opportunity(conn, ctx, title="c", stage="proposal")
        await store.find_or_create_opportunity(conn, ctx, title="d", stage="closed_won", value=300)
        summary = await store.pipeline_summary(conn, ctx)
    assert list(summary["by_stage"]) == [key for key, *_ in DEFAULT_STAGES]
    assert summary["by_stage"]["proposal"] == {
        "label": "Proposal",
        "outcome": "open",
        "count": 3,
        "value_sum": 1500.0,
        "weighted_sum": 600.0,
    }
    assert summary["by_stage"]["lead"] == {"label": "Lead", "outcome": "open", "count": 0}
    assert (summary["open_count"], summary["won_count"], summary["lost_count"]) == (3, 1, 0)
    assert (summary["total_value"], summary["total_weighted"]) == (1800.0, 900.0)


# --------------------------------------------------------------------------- #
# Retiring against concurrent writes                                           #
# --------------------------------------------------------------------------- #


async def _held(ctx, write):
    """Run ``write(conn)`` in a transaction held open until released."""
    done = asyncio.Event()
    release = asyncio.Event()

    async def run() -> None:
        async with session_for(ctx) as conn:
            await write(conn)
            done.set()
            await release.wait()

    task = asyncio.create_task(run())
    await done.wait()
    return task, release


async def _create(ctx, **kwargs):
    async with session_for(ctx) as conn:
        deal, _ = await store.find_or_create_opportunity(conn, ctx, **kwargs)
    return deal


# --------------------------------------------------------------------------- #
# Row-level security                                                           #
# --------------------------------------------------------------------------- #
