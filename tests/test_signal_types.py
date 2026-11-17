"""The signal vocabulary: the types a workspace records about an account.

Every type write and every signal write goes through the store. Every fixture
company here is fictional.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import define_signal_types, make_ctx, session_for
from fastmcp import Client
from sqlalchemy import select, text

import mcp_people.server as server
from mcp_people.db import store
from mcp_people.db.tables import people_audit
from mcp_people.fields import FieldError
from mcp_people.signal_types import MAX_SIGNAL_TYPES

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


async def _describe(ctx) -> list[tuple[str, str, int]]:
    async with session_for(ctx) as conn:
        types = await store.describe_signal_types(conn, ctx)
    return [(t.key, t.label, t.signal_count) for t in types]


async def _org(ctx, *, name: str = "Northwind Freight") -> str:
    async with session_for(ctx) as conn:
        org, _action, _promoted = await store.find_or_create_organization(conn, ctx, name=name)
    return org.id


async def _record(conn, ctx, org_id: str, *, signal_type: str, url: str):
    return await store.record_signal(
        conn,
        ctx,
        organization_id=org_id,
        claim="Northwind is standing up a second distribution centre.",
        evidence_quote="Our second distribution centre opens in the spring.",
        source_url=url,
        signal_type=signal_type,
    )


# --------------------------------------------------------------------------- #
# What a workspace starts with                                                 #
# --------------------------------------------------------------------------- #


async def test_a_fresh_workspace_defines_no_signal_types(app_engine) -> None:
    """There is no default list, and a read seeds nothing into an empty workspace."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    assert await _describe(ctx) == []
    assert await _describe(ctx) == []
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        with pytest.raises(FieldError, match="defines no signal types yet") as caught:
            await _record(conn, ctx, org_id, signal_type="hiring", url="https://news.example/a")
    assert "define_signal_type" in caught.value.next_step


async def test_signals_that_predate_the_vocabulary_seed_it(app_engine) -> None:
    """A workspace whose signals were recorded before types were rows gets one type per
    key they carry, labelled with the key, and counted over the active ones only."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        for index, (kind, status) in enumerate(
            (("hiring", "active"), ("hiring", "active"), ("expansion", "withdrawn"))
        ):
            await conn.execute(
                text(
                    "INSERT INTO signal (id, tenant_id, workspace_id, organization_id, claim, "
                    "evidence_quote, source_url, signal_type, status) "
                    "VALUES (:id, :tenant_id, :workspace, :org, 'A claim.', 'A quote.', :url, "
                    ":kind, :status)"
                ),
                {
                    "id": f"sg_seed{index}",
                    "tenant_id": ctx.tenant_id,
                    "workspace": ctx.workspace_id,
                    "org": org_id,
                    "url": f"https://news.example/{index}",
                    "kind": kind,
                    "status": status,
                },
            )
    assert await _describe(ctx) == [("expansion", "expansion", 0), ("hiring", "hiring", 2)]
    # A second read seeds nothing more, and another workspace stays empty.
    assert len(await _describe(ctx)) == 2
    assert await _describe(make_ctx(ctx.tenant_id, workspace_id="ws_2")) == []


# --------------------------------------------------------------------------- #
# Defining and changing types                                                 #
# --------------------------------------------------------------------------- #


async def test_define_adds_and_update_changes_only_what_is_given(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        created, action = await store.define_signal_type(
            conn, ctx, key=" Funding_Round ", label="Funding round", description="New capital."
        )
        assert (action, created.key, created.id[:3]) == ("created", "funding_round", "st_")
        # Defining a key the vocabulary holds changes nothing.
        same, action = await store.define_signal_type(conn, ctx, key="funding_round", label="Other")
        assert (action, same.id, same.label) == ("matched", created.id, "Funding round")
        relabelled, action = await store.update_signal_type(
            conn, ctx, key="funding_round", label="Funding"
        )
        assert (action, relabelled.label, relabelled.description) == (
            "updated",
            "Funding",
            "New capital.",
        )
        cleared, _ = await store.update_signal_type(conn, ctx, key="funding_round", description="")
        assert cleared.description is None
        _, action = await store.update_signal_type(conn, ctx, key="funding_round", label="Funding")
        assert action == "unchanged"
        with pytest.raises(FieldError, match="not in this workspace's vocabulary"):
            await store.update_signal_type(conn, ctx, key="absent", label="X")
        audited = (
            await conn.execute(
                select(people_audit.c.action).where(people_audit.c.entity == "signal_type")
            )
        ).all()
    assert len(audited) == 3
    assert await _describe(ctx) == [("funding_round", "Funding", 0)]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"key": "Not A Key", "label": "x"}, "lowercase letters"),
        ({"key": "fresh", "label": "  "}, "label must not be empty"),
        ({"key": "fresh", "label": "x" * 65}, "label exceeds"),
        ({"key": "fresh", "label": "x", "description": "x" * 1025}, "description exceeds"),
    ],
)
async def test_define_refuses_a_bad_type(app_engine, kwargs, message) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match=message):
            await store.define_signal_type(conn, ctx, **kwargs)


async def test_a_workspace_holds_at_most_the_cap(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    keys = [f"type_{n}" for n in range(MAX_SIGNAL_TYPES)]
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, *keys)
        with pytest.raises(ValueError, match=f"at most {MAX_SIGNAL_TYPES}"):
            await store.define_signal_type(conn, ctx, key="one_more", label="One more")
        # A type already defined still changes at the cap.
        _, action = await store.update_signal_type(conn, ctx, key="type_0", label="First")
    assert action == "updated"


# --------------------------------------------------------------------------- #
# What record_signal takes                                                     #
# --------------------------------------------------------------------------- #


async def test_record_signal_refuses_a_type_outside_the_vocabulary(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring", "expansion")
        with pytest.raises(FieldError, match="not in this workspace's vocabulary") as caught:
            await _record(conn, ctx, org_id, signal_type="vibes", url="https://a.example")
        assert "Use one of: expansion, hiring." in caught.value.next_step
        assert "define_signal_type" in caught.value.next_step
        # Keys compare stripped and lowercased, and store as the key.
        sig, _, _ = await _record(
            conn, ctx, org_id, signal_type=" Hiring ", url="https://a.example"
        )
    assert sig.signal_type == "hiring"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "record_signal",
            {
                "organization_id": "org_any",
                "claim": "Northwind is hiring a head of logistics.",
                "evidence_quote": "We are hiring a Head of Logistics.",
                "source_url": "https://a.example",
                "signal_type": "hiring",
                "confidence": "high",
            },
        ),
        ("record_signal", {"organization_id": "org_any", "need_open": "open"}),
        ("record_signal", {"organization_id": "org_any", "reasoning": "why"}),
        ("list_signals", {"need_open": "open"}),
    ],
)
async def test_a_signal_tool_refuses_a_grade_argument(tool: str, args: dict) -> None:
    """A signal carries no grade, and the tool schema admits no argument it does not
    declare: the call is refused before it runs, naming the argument."""
    schema = {t.name: t for t in await server.mcp.list_tools()}[tool].parameters
    assert schema.get("additionalProperties") is False
    assert not {"confidence", "need_open", "reasoning"} & set(schema["properties"])
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error
    assert "Unexpected keyword argument" in result.content[0].text


# --------------------------------------------------------------------------- #
# Row-level security                                                           #
# --------------------------------------------------------------------------- #


async def test_a_read_grant_does_not_borrow_another_workspaces_vocabulary(app_engine) -> None:
    tid = _tid()
    ctx_a = make_ctx(tid, workspace_id="ws_a")
    async with session_for(ctx_a) as conn:
        await define_signal_types(conn, ctx_a, "hiring")
    ctx_b = make_ctx(tid, workspace_id="ws_b", allowed_workspaces={"ws_a"})
    assert await _describe(ctx_b) == []
    org_id = await _org(ctx_b)
    async with session_for(ctx_b) as conn:
        with pytest.raises(FieldError, match="defines no signal types yet"):
            await _record(conn, ctx_b, org_id, signal_type="hiring", url="https://a.example")


# --------------------------------------------------------------------------- #
# Retiring a type                                                              #
# --------------------------------------------------------------------------- #


async def test_a_type_retires_while_signals_carry_it(app_engine) -> None:
    """Retiring is allowed while signals carry the type: they keep it, list_signals still
    filters on it, and no new signal takes it."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring", "headcount_growth")
        recorded, _action, _stored = await _record(
            conn, ctx, org_id, signal_type="hiring", url="https://news.example/hire"
        )
        retired, action = await store.update_signal_type(
            conn, ctx, key="hiring", retired=True, replaced_by="headcount_growth"
        )
        assert action == "retired"
        assert (retired.retired_at is not None, retired.replaced_by) == (True, "headcount_growth")
        assert retired.signal_count == 1
        with pytest.raises(FieldError, match="is retired") as caught:
            await _record(conn, ctx, org_id, signal_type="hiring", url="https://news.example/2")
        assert "Use 'headcount_growth', which replaces it." in caught.value.next_step
        kept = await store.get_signal(conn, ctx, signal_id=recorded.id)
        listed, _cursor = await store.list_signals(conn, ctx, signal_type="hiring")
        every = await store.describe_signal_types(conn, ctx, include_retired=True)
    assert await _describe(ctx) == [("headcount_growth", "headcount_growth", 0)]
    assert [(t.key, t.signal_count) for t in every] == [("headcount_growth", 0), ("hiring", 1)]
    assert kept.signal_type == "hiring"
    assert [s.id for s in listed] == [recorded.id]


async def test_a_retired_key_stays_taken_and_restoring_brings_it_back(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
        await store.update_signal_type(conn, ctx, key="hiring", retired=True)
        with pytest.raises(FieldError, match="stays taken") as caught:
            await store.define_signal_type(conn, ctx, key="hiring", label="Hiring")
        assert "update_signal_type(key='hiring', retired=false)" in caught.value.next_step
        restored, action = await store.update_signal_type(conn, ctx, key="hiring", retired=False)
    assert (restored.retired_at, action) == (None, "restored")
    assert await _describe(ctx) == [("hiring", "hiring", 0)]


async def test_a_retired_only_vocabulary_is_never_seeded_again(app_engine) -> None:
    """The seed runs only on a vocabulary with no row; a retired type keeps its row, so
    retiring every type brings none back, deleted signals' types included."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
        await _record(conn, ctx, org_id, signal_type="hiring", url="https://news.example/h")
        await store.update_signal_type(conn, ctx, key="hiring", retired=True)
    assert await _describe(ctx) == []
    assert await _describe(ctx) == []
    async with session_for(ctx) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT key, retired_at IS NOT NULL FROM signal_type WHERE workspace_id = 'ws_1'"
                )
            )
        ).all()
        with pytest.raises(FieldError, match="is retired") as caught:
            await _record(conn, ctx, org_id, signal_type="hiring", url="https://news.example/x")
    assert [tuple(r) for r in rows] == [("hiring", True)]
    assert "No live signal type is defined." in caught.value.next_step


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"replaced_by": "funding"}, "only to a retired signal type"),
        ({"retired": True, "replaced_by": "hiring"}, "cannot replace itself"),
        ({"retired": True, "replaced_by": "nowhere"}, "is not in this workspace's vocabulary"),
        ({"retired": True, "replaced_by": "layoffs"}, "is retired"),
    ],
)
async def test_a_replacement_must_be_another_live_type(app_engine, kwargs, message) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring", "funding", "layoffs")
        await store.update_signal_type(conn, ctx, key="layoffs", retired=True)
        with pytest.raises(FieldError, match=message):
            await store.update_signal_type(conn, ctx, key="hiring", **kwargs)


async def test_the_cap_counts_live_types(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, *(f"type_{i}" for i in range(MAX_SIGNAL_TYPES)))
        await store.update_signal_type(conn, ctx, key="type_0", retired=True)
        await store.define_signal_type(conn, ctx, key="fresh", label="Fresh")
        with pytest.raises(ValueError, match="live signal types"):
            await store.update_signal_type(conn, ctx, key="type_0", retired=False)
