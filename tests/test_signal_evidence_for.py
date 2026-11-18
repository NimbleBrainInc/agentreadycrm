"""Whom a signal is evidence for, and the change read.

Fixture companies are fictional.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from conftest import define_signal_types, make_ctx, session_for
from pgkeyset import InvalidCursor
from sqlalchemy import select

import mcp_people.server as server
from mcp_people.db import store
from mcp_people.db.store import (
    SignalWithdrawnError,
)
from mcp_people.db.tables import people_audit, signal_evidence_for
from mcp_people.evidence import MAX_PARTIES

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every committed change reads as settled, so a test reads its own writes at once.
    The settle margin itself is tested with its real value below."""
    monkeypatch.setattr(store, "SIGNAL_CHANGE_SETTLE", timedelta(0))


async def _org(ctx, name: str, domain: str) -> str:
    async with session_for(ctx) as conn:
        org, _action, _promoted = await store.find_or_create_organization(
            conn, ctx, name=name, domain=domain
        )
    return org.id


async def _record(
    ctx,
    org_id: str,
    *,
    url: str = "https://careers.northwind.example/ae",
    claim: str = "Northwind is hiring an Account Executive.",
    quote: str = "We are hiring an Account Executive for the western region.",
    evidence_for: list[str] | None = None,
    found_by: str | None = None,
):
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
        return await store.record_signal(
            conn,
            ctx,
            organization_id=org_id,
            claim=claim,
            evidence_quote=quote,
            source_url=url,
            signal_type="hiring",
            evidence_for=evidence_for,
            found_by=found_by,
        )


async def _add(ctx, signal_id: str, parties: list[str], **kwargs):
    async with session_for(ctx) as conn:
        return await store.add_signal_for(
            conn, ctx, signal_id=signal_id, evidence_for=parties, **kwargs
        )


async def _remove(ctx, signal_id: str, parties: list[str]):
    async with session_for(ctx) as conn:
        return await store.remove_signal_for(conn, ctx, signal_id=signal_id, evidence_for=parties)


async def _get(ctx, signal_id: str):
    async with session_for(ctx) as conn:
        return await store.get_signal(conn, ctx, signal_id=signal_id)


async def _list(ctx, **kwargs):
    async with session_for(ctx) as conn:
        return await store.list_signals(conn, ctx, **kwargs)


async def _changes(ctx, changed_since: str, **kwargs):
    async with session_for(ctx) as conn:
        return await store.list_signal_changes(conn, ctx, changed_since=changed_since, **kwargs)


async def _edge_rows(ctx, signal_id: str) -> list[dict]:
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
    return [dict(r) for r in rows]


@pytest.fixture
async def world(app_engine):
    """A workspace with its account (Northwind) and a client it researches for
    (Contoso)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    northwind = await _org(ctx, "Northwind", "northwind.example")
    contoso = await _org(ctx, "Contoso", "contoso.example")
    return ctx, northwind, contoso


# --------------------------------------------------------------------------- #
# Adding, removing, and what a withdrawal or a collision does to them          #
# --------------------------------------------------------------------------- #


async def test_a_fact_found_again_for_another_party_keeps_both(world) -> None:
    ctx, northwind, contoso = world
    first, _, _ = await _record(ctx, northwind, evidence_for=["workspace"], found_by="run-1")
    again, action, stored = await _record(ctx, northwind, evidence_for=[contoso])

    assert (action, stored, again.id) == ("matched", True, first.id)
    assert again.evidence_for == ["workspace", contoso]
    edges = {e["for_organization_id"]: e for e in await _edge_rows(ctx, first.id)}
    assert edges[None]["added_by"] == "run-1"


async def test_the_signals_own_organization_may_be_a_party(world) -> None:
    ctx, northwind, _contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=[northwind])
    assert sig.evidence_for == [northwind]


async def test_adding_an_existing_edge_returns_it_and_a_removed_one_comes_back(world) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=["workspace"])

    same, added = await _add(ctx, sig.id, ["workspace"])
    assert added == []
    assert same.evidence_for == ["workspace"]
    assert same.updated_at == sig.updated_at, "a no-op add is not a change"

    await _remove(ctx, sig.id, ["workspace"])
    back, added = await _add(ctx, sig.id, ["Workspace", contoso, contoso])
    assert added == ["workspace", contoso]
    assert back.evidence_for == ["workspace", contoso]
    assert len(await _edge_rows(ctx, sig.id)) == 2, "the removed row came back, not a new one"


async def test_removing_keeps_the_row_with_removed_at(world) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=["workspace", contoso])

    after, removed = await _remove(ctx, sig.id, [contoso])
    assert removed == [contoso]
    assert after.evidence_for == ["workspace"]
    edges = {e["for_organization_id"]: e for e in await _edge_rows(ctx, sig.id)}
    assert edges[contoso]["removed_at"] is not None
    assert edges[None]["removed_at"] is None

    again, removed = await _remove(ctx, sig.id, [contoso])
    assert removed == []
    assert again.updated_at == after.updated_at, "removing a removed edge is not a change"


async def test_a_withdrawn_signal_refuses_both(world) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=["workspace"])
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=sig.id, reason="misread")

    with pytest.raises(SignalWithdrawnError):
        await _add(ctx, sig.id, [contoso])
    with pytest.raises(SignalWithdrawnError):
        await _remove(ctx, sig.id, ["workspace"])
    assert (await _get(ctx, sig.id)).evidence_for == ["workspace"]


async def test_a_collision_and_a_match_on_a_withdrawn_signal_add_no_edges(world) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind)

    collided, _, stored = await _record(
        ctx, northwind, claim="Northwind is hiring a CFO.", evidence_for=[contoso]
    )
    assert stored is False
    assert collided.evidence_for == []

    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=sig.id)
    withdrawn, _, stored = await _record(ctx, northwind, evidence_for=["workspace"])
    assert stored is True
    assert withdrawn.status == "withdrawn"
    assert withdrawn.evidence_for == []
    assert await _edge_rows(ctx, sig.id) == []


@pytest.mark.parametrize("word", ["unstated", "UNSTATED", " unstated "])
async def test_the_reserved_words_are_refused_as_organization_ids(world, word) -> None:
    ctx, northwind, _contoso = world
    sig, _, _ = await _record(ctx, northwind)
    for call in (
        _add(ctx, sig.id, [word]),
        _remove(ctx, sig.id, [word]),
        _record(ctx, northwind, url="https://news.example/3", evidence_for=[word]),
    ):
        with pytest.raises(ValueError, match="not a party"):
            await call
    # "workspace" is the workspace, never an organization of that id.
    named, _ = await _add(ctx, sig.id, ["workspace"])
    assert named.evidence_for == ["workspace"]
    assert [e["for_organization_id"] for e in await _edge_rows(ctx, sig.id)] == [None]


async def test_a_call_names_at_most_ten_parties(world) -> None:
    ctx, northwind, _contoso = world
    sig, _, _ = await _record(ctx, northwind)
    orgs = [await _org(ctx, f"Client {i}", f"client{i}.example") for i in range(MAX_PARTIES)]

    with pytest.raises(ValueError, match="at most 10"):
        await _add(ctx, sig.id, [*orgs, "workspace"])
    with pytest.raises(ValueError, match="at most 10"):
        await _record(ctx, northwind, evidence_for=[*orgs, "workspace"])
    full, added = await _add(ctx, sig.id, orgs)
    assert len(added) == len(full.evidence_for) == MAX_PARTIES


async def test_an_empty_party_list_is_refused_by_the_edge_tools(world) -> None:
    ctx, northwind, _contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=[])
    assert sig.evidence_for == []
    with pytest.raises(ValueError, match="at least one"):
        await _add(ctx, sig.id, [])
    with pytest.raises(ValueError, match="at least one"):
        await _remove(ctx, sig.id, [])


async def test_each_change_is_audited_and_moves_the_signals_updated_at(world) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind)
    stamps = [sig.updated_at]
    stamps.append((await _add(ctx, sig.id, ["workspace"]))[0].updated_at)
    stamps.append((await _remove(ctx, sig.id, ["workspace"]))[0].updated_at)
    stamps.append((await _record(ctx, northwind, evidence_for=[contoso]))[0].updated_at)
    async with session_for(ctx) as conn:
        stamps.append((await store.withdraw_signal(conn, ctx, signal_id=sig.id)).updated_at)
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps), stamps

    async with session_for(ctx) as conn:
        audits = (
            await conn.execute(
                select(people_audit.c.action, people_audit.c.detail)
                .where(people_audit.c.entity == "signal_evidence_for")
                .order_by(people_audit.c.id)
            )
        ).all()
    assert [(a, d["parties"]) for a, d in audits] == [
        ("add", ["workspace"]),
        ("remove", ["workspace"]),
        ("add", [contoso]),
    ]


# --------------------------------------------------------------------------- #
# Listing by party                                                             #
# --------------------------------------------------------------------------- #


async def test_list_signals_by_workspace_by_organization_and_unstated(world) -> None:
    ctx, northwind, contoso = world
    ours, _, _ = await _record(
        ctx, northwind, url="https://n.example/1", evidence_for=["workspace"]
    )
    theirs, _, _ = await _record(ctx, northwind, url="https://n.example/2", evidence_for=[contoso])
    both, _, _ = await _record(
        ctx, northwind, url="https://n.example/3", evidence_for=["workspace", contoso]
    )
    unstated, _, _ = await _record(ctx, northwind, url="https://n.example/4")
    dropped, _, _ = await _record(ctx, northwind, url="https://n.example/5", evidence_for=[contoso])
    await _remove(ctx, dropped.id, [contoso])

    async def ids(party: str) -> set[str]:
        return {s.id for s in (await _list(ctx, evidence_for=party))[0]}

    assert await ids("workspace") == {ours.id, both.id}
    assert await ids(contoso) == {theirs.id, both.id}
    assert await ids("unstated") == {unstated.id, dropped.id}
    rows = {s.id: s.evidence_for for s in (await _list(ctx))[0]}
    assert rows[both.id] == ["workspace", contoso]
    assert rows[dropped.id] == []


async def test_status_any_reads_both_states_and_an_unknown_status_is_refused(world) -> None:
    ctx, northwind, _contoso = world
    live, _, _ = await _record(ctx, northwind, url="https://n.example/1")
    gone, _, _ = await _record(ctx, northwind, url="https://n.example/2")
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=gone.id)

    assert {s.id for s in (await _list(ctx))[0]} == {live.id}
    assert {s.id for s in (await _list(ctx, status="any"))[0]} == {live.id, gone.id}
    with pytest.raises(ValueError, match="status must be one of"):
        await _list(ctx, status="everything")


# --------------------------------------------------------------------------- #
# Reading changes                                                              #
# --------------------------------------------------------------------------- #


async def test_changes_return_creates_edge_changes_and_withdrawals_in_order(world, settled) -> None:
    ctx, northwind, contoso = world
    first, _, _ = await _record(ctx, northwind, url="https://n.example/1")
    second, _, _ = await _record(ctx, northwind, url="https://n.example/2")

    rows, mark, more = await _changes(ctx, "", status="any")
    assert [s.id for s in rows] == [first.id, second.id]
    assert more is False

    await _add(ctx, first.id, [contoso])
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=second.id)

    rows, mark, _ = await _changes(ctx, mark, status="any")
    assert [(s.id, s.status, s.evidence_for) for s in rows] == [
        (first.id, "active", [contoso]),
        (second.id, "withdrawn", []),
    ]
    caught_up, same_mark, more = await _changes(ctx, mark, status="any")
    assert (caught_up, same_mark, more) == ([], mark, False)

    # The default status reads active signals only, so a withdrawal does not reach it.
    await _remove(ctx, first.id, [contoso])
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=first.id)
    assert (await _changes(ctx, mark))[0] == []


async def test_a_reader_filtered_to_a_party_sees_its_removal(world, settled) -> None:
    ctx, northwind, contoso = world
    sig, _, _ = await _record(ctx, northwind, evidence_for=[contoso])
    rows, mark, _ = await _changes(ctx, "", evidence_for=contoso)
    assert [s.id for s in rows] == [sig.id]

    await _remove(ctx, sig.id, [contoso])
    rows, mark, _ = await _changes(ctx, mark, evidence_for=contoso)
    assert [(s.id, s.evidence_for) for s in rows] == [(sig.id, [])]
    # The listing reads current edges only.
    assert (await _list(ctx, evidence_for=contoso))[0] == []


async def test_a_cursor_of_the_other_mode_is_refused(world, settled) -> None:
    ctx, northwind, _contoso = world
    for i in range(3):
        await _record(ctx, northwind, url=f"https://n.example/{i}")
    _rows, listing_cursor = await _list(ctx, limit=1)
    _rows, change_mark, _ = await _changes(ctx, "", limit=1)
    assert listing_cursor and change_mark

    with pytest.raises(InvalidCursor):
        await _changes(ctx, listing_cursor)
    with pytest.raises(InvalidCursor):
        await _list(ctx, cursor=change_mark)


async def test_changes_are_scoped_to_the_workspace(world, settled) -> None:
    ctx, northwind, _contoso = world
    await _record(ctx, northwind)
    _rows, mark, _ = await _changes(ctx, "")
    other = make_ctx(ctx.tenant_id, workspace_id="ws_2")
    assert (await _changes(other, ""))[0] == []
    assert (await _changes(other, mark))[0] == []


# --------------------------------------------------------------------------- #
# The tool layer                                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def headers(app_engine) -> dict[str, str]:
    """The tools act as the local runtime's one principal; this suite needs no identity
    of its own, so the mapping is empty."""
    return {}


async def _tool_signal(**kwargs) -> dict:
    await server.define_signal_type(key="hiring", label="Hiring")
    org = await server.find_or_create_organization(name="Northwind", domain="northwind.example")
    return await server.record_signal(
        organization_id=org["organization"]["id"],
        claim="Northwind is hiring an Account Executive.",
        evidence_quote="We are hiring an Account Executive.",
        source_url="https://careers.northwind.example/ae",
        signal_type="hiring",
        **kwargs,
    )


async def test_the_tools_refuse_a_withdrawn_signal_as_a_conflict(headers) -> None:
    sig = (await _tool_signal(evidence_for=["workspace"]))["signal"]
    assert sig["evidence_for"] == ["workspace"]
    await server.withdraw_signal(signal_id=sig["id"])

    for res in (
        await server.update_signal(signal_id=sig["id"], add_evidence_for=["workspace"]),
        await server.update_signal(signal_id=sig["id"], remove_evidence_for=["workspace"]),
    ):
        assert res["error"]["code"] == "conflict"
        assert "withdrawn" in res["error"]["message"]
        assert res["next_step"].strip()


async def test_record_signal_says_the_purposes_were_not_recorded(headers) -> None:
    await _tool_signal()
    # The same fact again: stored, so its purpose is recorded.
    collided = await _tool_signal(evidence_for=["workspace"], found_by="run-2")
    assert collided["signal"]["evidence_for"] == ["workspace"]

    org_id = collided["signal"]["organization_id"]
    other = await server.record_signal(
        organization_id=org_id,
        claim="Northwind is hiring a CFO.",
        evidence_quote="We are hiring a CFO.",
        source_url="https://careers.northwind.example/ae",
        signal_type="hiring",
        evidence_for=["workspace"],
    )
    assert other["next_step"].startswith("NOT STORED")
    assert "evidence_for purposes were not recorded" in other["next_step"]

    await server.withdraw_signal(signal_id=collided["signal"]["id"])
    withdrawn = await _tool_signal(evidence_for=["workspace"])
    assert withdrawn["signal"]["status"] == "withdrawn"
    assert "evidence_for purposes were not recorded" in withdrawn["next_step"]


async def test_list_signals_reads_changes_and_refuses_a_mixed_cursor(
    headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "SIGNAL_CHANGE_SETTLE", timedelta(0))
    sig = (await _tool_signal(evidence_for=["workspace"]))["signal"]

    first = await server.list_signals(changed_since="", evidence_for="workspace")
    assert [s["id"] for s in first["signals"]] == [sig["id"]]
    assert first["next_cursor"]
    assert "Caught up" in first["next_step"]

    listing = await server.list_signals(limit=1)
    assert "next_cursor" not in listing  # one signal: the last page

    refused = await server.list_signals(changed_since="bm90LWEtbWFyaw")
    assert refused["error"]["code"] == "invalid_request"
    assert "changed_since" in refused["next_step"]
    refused = await server.list_signals(cursor=first["next_cursor"])
    assert refused["error"]["code"] == "invalid_request"
    both = await server.list_signals(changed_since="", cursor=first["next_cursor"])
    assert both["error"]["code"] == "invalid_request"


async def test_the_tools_report_an_out_of_scope_party_as_not_found(headers) -> None:
    sig = (await _tool_signal())["signal"]
    res = await server.update_signal(signal_id=sig["id"], add_evidence_for=["org_absent"])
    assert res["error"]["code"] == "not_found"
    assert res["error"]["entity"] == "organization"
    res = await server.update_signal(signal_id="sg_absent", remove_evidence_for=["workspace"])
    assert res["error"]["code"] == "not_found"
    assert res["error"]["entity"] == "signal"


async def test_a_signal_write_past_its_deadline_rolls_back(
    headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that stalls inside its transaction past SIGNAL_WRITE_DEADLINE never
    commits, so it cannot land behind a change mark already issued."""
    sig = (await _tool_signal())["signal"]
    monkeypatch.setattr(store, "SIGNAL_WRITE_DEADLINE", timedelta(seconds=0.2))
    real_add = store.add_signal_for

    async def stalled_add(*args, **kwargs):
        out = await real_add(*args, **kwargs)
        await asyncio.sleep(1)  # still inside the transaction
        return out

    monkeypatch.setattr(store, "add_signal_for", stalled_add)
    res = await server.update_signal(signal_id=sig["id"], add_evidence_for=["workspace"])
    assert res["error"]["code"] == "internal_error"

    monkeypatch.setattr(store, "add_signal_for", real_add)
    after = await server.get_signal(signal_id=sig["id"])
    assert after["signal"]["evidence_for"] == []
    assert after["signal"]["updated_at"] == sig["updated_at"]
