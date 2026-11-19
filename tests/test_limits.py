"""Limits: what a runtime's :class:`~mcp_people.runtime.Limits` lets an account store.

What these pin, for each limit: a create past it is refused and keeps nothing, one below it
is not, and a match, an edit, a merge, an archive, a move or a delete is never refused at it.
A live count frees room when a record is deleted or merged; a lifetime count does not, and
an archived record counts in both. An import longer than ``max_import_rows`` is refused
whole, and a row inside one that would create past a count is skipped under a reason naming
the limit while the rest commit. A dossier is bounded as it would be stored, so an append is
measured by the joined text. ``None`` limits nothing. Through the tools, a refusal is
``invalid_request`` with ``limit`` naming the field, a ``next_step`` naming what frees room
and the local install, and that field's event.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any

import pytest
from conftest import RUNTIME, make_ctx, session_for
from fastmcp import Client
from sqlalchemy import func, select

from mcp_people.db import dialect, store
from mcp_people.db.store import LimitExceededError
from mcp_people.db.tables import contact, organization, people_audit
from mcp_people.runtime import Limits, PeopleEvent
from mcp_people.server import mcp as server

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}")


async def _run(ctx, fn, **kwargs):
    async with session_for(ctx) as conn:
        return await fn(conn, ctx, **kwargs)


async def _refused(ctx, limit: str, fn, **kwargs) -> LimitExceededError:
    with pytest.raises(LimitExceededError) as caught:
        await _run(ctx, fn, **kwargs)
    assert caught.value.limit == limit
    return caught.value


async def _count(ctx, table, *where) -> int:
    async with session_for(ctx) as conn:
        stmt = select(func.count()).select_from(table).where(table.c.tenant_id == ctx.tenant_id)
        return (await conn.execute(stmt.where(*where))).scalar_one()


async def _contact(ctx, limits, email: str, **kwargs):
    found, action, _ = await _run(
        ctx, store.find_or_create_contact, email=email, limits=limits, **kwargs
    )
    return found, action


async def _org(ctx, limits, domain: str, **kwargs):
    org, action, _ = await _run(
        ctx, store.find_or_create_organization, name=domain, domain=domain, limits=limits, **kwargs
    )
    return org, action


# --------------------------------------------------------------------------- #
# The contract                                                                 #
# --------------------------------------------------------------------------- #


def test_every_limit_defaults_to_none_and_has_a_refusal_event() -> None:
    names = [f.name for f in dataclasses.fields(Limits)]
    assert all(getattr(Limits(), name) is None for name in names)
    for name in names:
        assert PeopleEvent(f"{name}_refused")
        assert "locally (free, no limits)" in store.limit_next_step(name)


async def test_none_limits_nothing(ctx) -> None:
    for limits in (None, Limits()):
        for i in range(3):
            _, action = await _contact(ctx, limits, f"p{i}@acme.example")
        await _run(ctx, store.set_contact_dossier, contact_id=_.id, dossier="x" * 5000)
    assert await _count(ctx, contact) == 3


# --------------------------------------------------------------------------- #
# Contacts and organizations: live and ever created                            #
# --------------------------------------------------------------------------- #


async def test_contacts_refuse_at_the_live_cap_and_a_match_edit_or_archive_is_not_refused(
    ctx,
) -> None:
    limits = Limits(max_contacts=2)
    first, _ = await _contact(ctx, limits, "a@acme.example")
    second, _ = await _contact(ctx, limits, "b@acme.example")

    refusal = await _refused(
        ctx, "max_contacts", store.find_or_create_contact, email="c@acme.example", limits=limits
    )
    assert "limit of 2 contacts (max_contacts)" in refusal.message
    assert "delete_contact" in refusal.next_step
    assert await _count(ctx, contact) == 2

    _, action = await _contact(ctx, limits, "A@acme.example", title="CEO")
    assert action == "matched"
    await _run(ctx, store.update_contact, contact_id=first.id, title="CTO")
    # A merge is never refused, and the duplicate it archives still counts.
    await _run(ctx, store.merge_contacts, target_id=first.id, source_id=second.id)
    await _run(ctx, store.archive_record, entity="contact", record_id=first.id, archived=True)
    await _refused(
        ctx, "max_contacts", store.find_or_create_contact, email="c@acme.example", limits=limits
    )
    # Deleting the archived duplicate frees its room.
    await _run(ctx, store.delete_contact, contact_id=second.id, confirm=second.id)
    _, action = await _contact(ctx, limits, "c@acme.example")
    assert action == "created"


async def test_a_delete_frees_live_room_but_not_the_lifetime_allowance(ctx) -> None:
    limits = Limits(max_contacts=2, max_contacts_created=3)
    a, _ = await _contact(ctx, limits, "a@acme.example")
    await _contact(ctx, limits, "b@acme.example")
    await _run(ctx, store.delete_contact, contact_id=a.id, confirm=a.id)
    _, action = await _contact(ctx, limits, "c@acme.example")  # live room freed
    assert action == "created"

    c_row = await _count(ctx, contact, contact.c.deleted_at.is_(None))
    assert c_row == 2
    b = (await _contact(ctx, limits, "b@acme.example"))[0]
    await _run(ctx, store.delete_contact, contact_id=b.id, confirm=b.id)
    # One live contact, but three ever created: the lifetime count refuses, and is named
    # ahead of the live one, since freeing live room would not help.
    refusal = await _refused(
        ctx,
        "max_contacts_created",
        store.find_or_create_contact,
        email="d@acme.example",
        limits=limits,
    )
    assert "lifetime allowance of 3 contacts" in refusal.message
    assert "Nothing frees this allowance" in refusal.next_step


async def test_organizations_count_every_path_that_creates_one(ctx) -> None:
    limits = Limits(max_organizations=1)
    acme, _ = await _org(ctx, limits, "acme.example")
    _, action = await _org(ctx, limits, "acme.example", industry="Widgets")
    assert action == "matched"
    await _refused(
        ctx,
        "max_organizations",
        store.find_or_create_organization,
        name="Globex",
        limits=limits,
    )
    # A contact naming a new organization would create one: refused, contact and all.
    await _refused(
        ctx,
        "max_organizations",
        store.find_or_create_contact,
        email="a@globex.example",
        organization_name="Globex",
        limits=limits,
    )
    assert await _count(ctx, contact) == 0
    # Naming the one it holds is a match, and the contact is created.
    _, action = await _contact(ctx, limits, "a@acme.example", organization_name="acme.example")
    assert action == "created"
    assert await _count(ctx, organization) == 1


async def test_organizations_ever_created_count_deleted_ones(ctx) -> None:
    limits = Limits(max_organizations_created=1)
    acme, _ = await _org(ctx, limits, "acme.example")
    await _run(ctx, store.delete_organization, organization_id=acme.id, confirm=acme.id)
    await _refused(
        ctx,
        "max_organizations_created",
        store.find_or_create_organization,
        name="acme.example",
        domain="acme.example",
        limits=limits,
    )


async def test_a_merge_of_organizations_is_never_refused_and_its_duplicate_counts_until_deleted(
    ctx,
) -> None:
    limits = Limits(max_organizations=2)
    acme, _ = await _org(ctx, limits, "acme.example")
    dup, _ = await _org(ctx, limits, "acme-dup.example")
    await _run(ctx, store.merge_organizations, target_id=acme.id, source_id=dup.id)
    await _refused(
        ctx,
        "max_organizations",
        store.find_or_create_organization,
        name="globex.example",
        domain="globex.example",
        limits=limits,
    )
    await _run(ctx, store.delete_organization, organization_id=dup.id, confirm=dup.id)
    _, action = await _org(ctx, limits, "globex.example")
    assert action == "created"


async def test_lookalikes_new_past_the_cap_are_skipped_and_held_ones_still_link(ctx) -> None:
    limits = Limits(max_organizations=2)
    seed, _ = await _org(ctx, limits, "seed.example")
    held, _ = await _org(ctx, limits, "held.example")
    result = await _run(
        ctx,
        store.link_similar_companies,
        org_id=seed.id,
        companies=[
            {"name": "New", "domain": "new.example", "similarity": 0.9},
            {"name": "Held", "domain": "held.example", "similarity": 0.8},
            {"name": "New again", "domain": "new.example", "similarity": 0.9},
        ],
        limits=limits,
    )
    assert result == {"linked": 1, "created": 0, "skipped": 1, "limit": "max_organizations"}
    assert await _count(ctx, organization) == 2
    assert held.id


# --------------------------------------------------------------------------- #
# Deals, interactions, evidence                                                #
# --------------------------------------------------------------------------- #


async def _deal(ctx, limits, title: str, **kwargs):
    return await _run(ctx, store.find_or_create_opportunity, title=title, limits=limits, **kwargs)


async def _move(ctx, limits, deal_id: str, stage: str):
    return await _run(
        ctx, store.set_opportunity_stage, opportunity_id=deal_id, stage=stage, limits=limits
    )


async def test_open_deals_refuse_at_the_cap_and_a_replay_or_open_move_is_not_refused(ctx) -> None:
    limits = Limits(max_open_opportunities=1)
    deal, _ = await _deal(ctx, limits, "Pilot", idempotency_key="k1")
    _, action = await _deal(ctx, limits, "Pilot", idempotency_key="k1")
    assert action == "matched"
    refusal = await _refused(
        ctx, "max_open_opportunities", store.find_or_create_opportunity, title="Next", limits=limits
    )
    assert "limit of 1 open deal (max_open_opportunities)" in refusal.message
    assert "won or lost stage" in refusal.next_step
    await _move(ctx, limits, deal.id, "proposal")  # open to open: never refused
    # A deal created closed takes no open room.
    _, action = await _deal(ctx, limits, "Lost already", stage="closed_lost")
    assert action == "created"


async def test_closing_a_deal_frees_open_room_and_reopening_one_counts(ctx) -> None:
    limits = Limits(max_open_opportunities=1)
    pilot, _ = await _deal(ctx, limits, "Pilot")
    await _move(ctx, limits, pilot.id, "closed_won")
    renewal, action = await _deal(ctx, limits, "Renewal")  # the won deal frees its slot
    assert action == "created"
    await _refused(
        ctx,
        "max_open_opportunities",
        store.set_opportunity_stage,
        opportunity_id=pilot.id,
        stage="lead",
        limits=limits,
    )
    stored, _ = await _run(ctx, store.get_opportunity, opportunity_id=pilot.id)
    assert stored.stage == "closed_won"  # the refused move changed nothing
    await _move(ctx, limits, pilot.id, "closed_lost")  # closed to closed: never refused
    await _move(ctx, limits, renewal.id, "closed_lost")
    await _move(ctx, limits, pilot.id, "negotiation")  # room again: the reopen counts and fits


async def test_a_retired_closing_stage_keeps_its_deals_closed(ctx) -> None:
    limits = Limits(max_open_opportunities=1)
    pilot, _ = await _deal(ctx, limits, "Pilot")
    await _move(ctx, limits, pilot.id, "closed_lost")
    await _run(ctx, store.update_pipeline_stage, key="closed_lost", retired=True)
    _, action = await _deal(ctx, limits, "Renewal")
    assert action == "created"


async def test_deals_ever_created_count_closed_and_deleted_ones(ctx) -> None:
    limits = Limits(max_opportunities_created=2)
    acme, _ = await _org(ctx, None, "acme.example")
    first, _ = await _deal(ctx, limits, "Pilot", organization_id=acme.id, idempotency_key="k1")
    await _deal(ctx, limits, "Pilot", organization_id=acme.id, idempotency_key="k1")  # replay
    second, _ = await _deal(ctx, limits, "Renewal", stage="closed_won")
    await _run(
        ctx,
        store.delete_organization,
        organization_id=acme.id,
        confirm=acme.id,
        delete_opportunities=True,
    )
    refusal = await _refused(
        ctx,
        "max_opportunities_created",
        store.find_or_create_opportunity,
        title="Next",
        limits=limits,
    )
    assert "lifetime allowance of 2 deals" in refusal.message
    assert "Nothing frees this allowance" in refusal.next_step
    await _move(ctx, limits, second.id, "lead")  # a move never counts toward it
    assert first.id


async def test_interactions_are_a_lifetime_allowance_and_a_replay_is_not_refused(ctx) -> None:
    limits = Limits(max_interactions=1)
    person, _ = await _contact(ctx, None, "a@acme.example")
    log = {"contact_id": person.id, "kind": "call", "summary": "Intro", "limits": limits}
    await _run(ctx, store.log_interaction, idempotency_key="k1", **log)
    await _run(ctx, store.log_interaction, idempotency_key="k1", **log)
    await _refused(ctx, "max_interactions", store.log_interaction, **log)


async def test_signals_are_a_lifetime_allowance_and_a_match_or_withdrawal_is_not_refused(
    ctx,
) -> None:
    limits = Limits(max_signals=1)
    acme, _ = await _org(ctx, None, "acme.example")
    async with session_for(ctx) as conn:
        await store.define_signal_type(conn, ctx, key="hiring", label="Hiring")
    signal = {
        "organization_id": acme.id,
        "claim": "Acme is hiring engineers.",
        "evidence_quote": "We are hiring engineers.",
        "signal_type": "hiring",
        "limits": limits,
    }
    first, _, _ = await _run(
        ctx, store.record_signal, source_url="https://acme.example/jobs", **signal
    )
    _, action, _ = await _run(
        ctx, store.record_signal, source_url="https://acme.example/jobs", **signal
    )
    assert action == "matched"
    await _run(ctx, store.withdraw_signal, signal_id=first.id)
    await _refused(
        ctx, "max_signals", store.record_signal, source_url="https://acme.example/news", **signal
    )


async def test_proof_points_are_a_lifetime_allowance_and_a_replay_or_verify_is_not_refused(
    ctx,
) -> None:
    limits = Limits(max_proof_points=1)
    point, _ = await _run(
        ctx,
        store.record_proof_point,
        claim="Cut onboarding to a day.",
        idempotency_key="k1",
        limits=limits,
    )
    _, action = await _run(
        ctx,
        store.record_proof_point,
        claim="Cut onboarding to a day.",
        idempotency_key="k1",
        limits=limits,
    )
    assert action == "matched"
    await _run(
        ctx,
        store.verify_proof_point,
        proof_point_id=point.id,
        verification="verified",
        attested_by="Jane",
    )
    await _refused(
        ctx, "max_proof_points", store.record_proof_point, claim="Another.", limits=limits
    )


# --------------------------------------------------------------------------- #
# Vocabulary: custom fields, stages, signal types                              #
# --------------------------------------------------------------------------- #


async def test_custom_fields_count_across_entities_and_retired_ones(ctx) -> None:
    limits = Limits(max_custom_fields=2)
    field = {"label": "Fit", "field_type": "text", "limits": limits}
    await _run(ctx, store.define_field, entity="contact", key="fit", **field)
    _, action, _ = await _run(ctx, store.define_field, entity="contact", key="fit", **field)
    assert action == "matched"
    await _run(ctx, store.define_field, entity="organization", key="fit", **field)
    await _run(ctx, store.update_field, entity="contact", key="fit", retired=True)
    refusal = await _refused(
        ctx, "max_custom_fields", store.define_field, entity="opportunity", key="fit", **field
    )
    assert "lifetime allowance of 2 custom fields" in refusal.message


async def test_stages_count_the_seed_and_the_seed_is_never_refused(ctx) -> None:
    limits = Limits(max_pipeline_stages=3)  # below the six seeded stages
    pipeline = await _run(ctx, store.get_pipeline)
    assert len(pipeline["stages"]) == 6
    _, action = await _run(ctx, store.find_or_create_opportunity, title="Pilot", limits=limits)
    assert action == "created"
    await _run(ctx, store.update_pipeline_stage, key="closed_won", label="Won")
    _, action = await _run(
        ctx, store.define_pipeline_stage, key="closed_won", label="Won", limits=limits
    )
    assert action == "matched"
    await _refused(
        ctx,
        "max_pipeline_stages",
        store.define_pipeline_stage,
        key="pilot",
        label="Pilot",
        limits=limits,
    )


async def test_stages_allow_below_the_cap_counting_the_seed(ctx) -> None:
    limits = Limits(max_pipeline_stages=7)
    _, action = await _run(
        ctx, store.define_pipeline_stage, key="pilot", label="Pilot", limits=limits
    )
    assert action == "created"
    await _refused(
        ctx,
        "max_pipeline_stages",
        store.define_pipeline_stage,
        key="legal",
        label="Legal",
        limits=limits,
    )


async def test_signal_types_are_a_lifetime_allowance(ctx) -> None:
    limits = Limits(max_signal_types=1)
    await _run(ctx, store.define_signal_type, key="hiring", label="Hiring", limits=limits)
    _, action = await _run(
        ctx, store.define_signal_type, key="hiring", label="Hiring", limits=limits
    )
    assert action == "matched"
    await _refused(
        ctx,
        "max_signal_types",
        store.define_signal_type,
        key="funding",
        label="Funding",
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# Dossiers                                                                     #
# --------------------------------------------------------------------------- #


async def test_a_dossier_is_bounded_as_stored_on_a_write_and_an_append(ctx) -> None:
    limits = Limits(max_dossier_bytes=12)
    person, _ = await _contact(ctx, None, "a@acme.example")
    write = {"contact_id": person.id, "limits": limits}
    await _run(ctx, store.set_contact_dossier, dossier="  ten bytes!  ", **write)
    await _run(ctx, store.set_contact_dossier, dossier="é" * 6, **write)  # 12 bytes
    refusal = await _refused(
        ctx, "max_dossier_bytes", store.set_contact_dossier, dossier="é" * 7, **write
    )
    assert "14 bytes" in refusal.message and "12 bytes per dossier" in refusal.message

    await _run(ctx, store.set_contact_dossier, dossier="abcdefgh", **write)
    await _run(ctx, store.set_contact_dossier, dossier="xy", append=True, **write)  # 8+2+2
    await _refused(
        ctx, "max_dossier_bytes", store.set_contact_dossier, dossier="z", append=True, **write
    )
    stored, _ = await _run(ctx, store.get_contact, contact_id=person.id)
    assert stored.dossier == "abcdefgh\n\nxy"
    await _run(ctx, store.set_contact_dossier, dossier="", **write)  # clearing is never refused


async def test_the_dossier_limit_binds_organizations_and_deals(ctx) -> None:
    limits = Limits(max_dossier_bytes=4)
    acme, _ = await _org(ctx, None, "acme.example")
    deal, _ = await _run(ctx, store.find_or_create_opportunity, title="Pilot")
    await _refused(
        ctx,
        "max_dossier_bytes",
        store.set_organization_dossier,
        org_id=acme.id,
        dossier="12345",
        limits=limits,
    )
    await _refused(
        ctx,
        "max_dossier_bytes",
        store.set_opportunity_dossier,
        opportunity_id=deal.id,
        dossier="12345",
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# Imports                                                                      #
# --------------------------------------------------------------------------- #


async def test_an_import_over_the_row_limit_is_refused_whole(ctx) -> None:
    limits = Limits(max_import_rows=2)
    rows = [{"email": f"p{i}@acme.example"} for i in range(3)]
    refusal = await _refused(
        ctx, "max_import_rows", store.import_contacts, rows=rows, limits=limits
    )
    assert "at most 2 rows per call, and this one has 3" in refusal.message
    assert await _count(ctx, contact) == 0
    result = await _run(ctx, store.import_contacts, rows=rows[:2], limits=limits)
    assert result["created"] == 2


async def test_an_import_skips_the_rows_past_a_count_and_saves_the_rest(ctx) -> None:
    limits = Limits(max_contacts=2, max_organizations=1)
    await _contact(ctx, None, "held@acme.example")
    rows = [
        {"email": "held@acme.example", "title": "CEO"},  # a match: never refused
        {"email": "new@acme.example", "organization_domain": "acme.example"},
        {"email": "other@globex.example", "organization_domain": "globex.example"},
        {"email": "more@acme.example"},
    ]
    result = await _run(ctx, store.import_contacts, rows=rows, limits=limits)
    assert (result["created"], result["matched"]) == (1, 1)
    assert result["skipped"] == [
        {
            "reason": "this account is at its limit of 1 organization (max_organizations)",
            "rows": [2],
            "limit": "max_organizations",
        },
        {
            "reason": "this account is at its limit of 2 contacts (max_contacts)",
            "rows": [3],
            "limit": "max_contacts",
        },
    ]
    assert await _count(ctx, contact) == 2
    assert await _count(ctx, organization) == 1


async def test_an_organization_import_skips_the_rows_past_the_lifetime_count(ctx) -> None:
    limits = Limits(max_organizations_created=1)
    rows = [{"domain": "acme.example"}, {"domain": "globex.example"}, {"domain": "acme.example"}]
    result = await _run(ctx, store.import_organizations, rows=rows, limits=limits)
    assert (result["created"], result["matched"]) == (1, 1)
    assert [g["limit"] for g in result["skipped"]] == ["max_organizations_created"]


# --------------------------------------------------------------------------- #
# Concurrency                                                                  #
# --------------------------------------------------------------------------- #


async def test_concurrent_creates_cannot_both_take_the_last_slot(ctx) -> None:
    """Two transactions creating at once, one slot left: one commits, one is refused.

    SQLite's ``BEGIN IMMEDIATE`` serializes them, as the account lock does on Postgres, so the
    second counts the first's committed row.
    """
    limits = Limits(max_contacts=1)
    outcomes = await asyncio.gather(
        *(_contact(ctx, limits, f"p{i}@acme.example") for i in range(2)),
        return_exceptions=True,
    )
    refused = [o for o in outcomes if isinstance(o, LimitExceededError)]
    assert len(refused) == 1, outcomes
    assert await _count(ctx, contact) == 1


async def test_on_postgres_the_tenant_lock_is_a_transaction_advisory_lock() -> None:
    from sqlalchemy.dialects import postgresql

    executed: list[str] = []

    class _Conn:
        dialect = postgresql.dialect()

        async def execute(self, stmt: Any) -> None:
            executed.append(str(stmt.compile(dialect=self.dialect)))

    await dialect.serialize_on(_Conn(), "people:limits:tenant-a")  # ty: ignore[invalid-argument-type]
    assert len(executed) == 1
    assert "pg_advisory_xact_lock(hashtextextended(" in executed[0]


# --------------------------------------------------------------------------- #
# Through the tools                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def limited(monkeypatch, app_engine):
    """The installed runtime returning ``Limits``, and the events it records."""
    events: list[tuple[PeopleEvent, int]] = []
    holder: dict[str, Limits] = {}
    monkeypatch.setattr(RUNTIME, "limits", lambda scope: holder["limits"])
    monkeypatch.setattr(RUNTIME, "record", lambda event, count=1: events.append((event, count)))

    def set_limits(limits: Limits) -> list[tuple[PeopleEvent, int]]:
        holder["limits"] = limits
        return events

    return set_limits


async def _call(client: Client, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert isinstance(result.structured_content, dict), result
    return result.is_error, result.structured_content


async def test_a_tool_refusal_names_the_limit_what_frees_room_and_the_local_install(
    limited,
) -> None:
    events = limited(Limits(max_contacts=1))
    async with Client(server) as client:
        failed, _ = await _call(client, "find_or_create_contact", email="a@acme.example")
        assert not failed
        failed, body = await _call(client, "find_or_create_contact", email="b@acme.example")
    assert failed
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["limit"] == "max_contacts"
    assert body["error"]["retryable"] is False
    assert "delete_contact" in body["next_step"]
    assert "run Agent Ready CRM locally (free, no limits)" in body["next_step"]
    assert (PeopleEvent.MAX_CONTACTS_REFUSED, 1) in events
    async with RUNTIME.engine().connect() as conn:
        outcomes = (
            (
                await conn.execute(
                    select(people_audit.c.outcome).where(people_audit.c.action == "find_or_create")
                )
            )
            .scalars()
            .all()
        )
    assert "denied" in outcomes


async def test_an_import_tool_names_the_limit_in_its_next_step_and_records_each_row(
    limited,
) -> None:
    events = limited(Limits(max_contacts=1))
    async with Client(server) as client:
        failed, body = await _call(
            client,
            "import_contacts",
            rows=[{"email": f"p{i}@acme.example"} for i in range(3)],
        )
    assert not failed
    assert body["skipped"][0]["limit"] == "max_contacts"
    assert "would pass this account's limits" in body["next_step"]
    assert "delete_contact" in body["next_step"]
    assert (PeopleEvent.MAX_CONTACTS_REFUSED, 2) in events


async def test_an_import_with_many_skip_reasons_keeps_its_limit_rows_apart(limited) -> None:
    """Past the most reason groups a result names, the rows a limit skipped keep their own
    group, its ``limit``, its next step and their events; only the other reasons fold."""
    events = limited(Limits(max_contacts=0))
    bad = [{"email": f"p{i}@acme.example", "fields": {f"f{i:02}": "x"}} for i in range(25)]
    async with Client(server) as client:
        failed, body = await _call(
            client, "import_contacts", rows=[*bad, {"email": "ok@acme.example"}]
        )
    assert not failed
    assert len(body["skipped"]) == 20
    [held] = [g for g in body["skipped"] if "limit" in g]
    assert held["limit"] == "max_contacts"
    assert held["rows"] == [25]
    assert "would pass this account's limits" in body["next_step"]
    assert "Fix the rows listed under skipped" in body["next_step"]
    assert sorted(i for g in body["skipped"] for i in g["rows"]) == list(range(26))
    assert (PeopleEvent.MAX_CONTACTS_REFUSED, 1) in events


async def test_an_import_over_the_row_limit_is_refused_by_the_tool(limited) -> None:
    limited(Limits(max_import_rows=1))
    async with Client(server) as client:
        failed, body = await _call(
            client, "import_organizations", rows=[{"domain": "a.example"}, {"domain": "b.example"}]
        )
    assert failed
    assert body["error"]["limit"] == "max_import_rows"
    assert "Split the list" in body["next_step"]


async def test_a_dossier_append_past_the_limit_is_refused_by_the_tool(limited) -> None:
    limited(Limits(max_dossier_bytes=10))
    async with Client(server) as client:
        _, made = await _call(client, "find_or_create_contact", email="a@acme.example")
        contact_id = made["contact"]["id"]
        failed, _ = await _call(client, "update_contact", contact_id=contact_id, dossier="12345")
        assert not failed
        failed, body = await _call(
            client, "update_contact", contact_id=contact_id, dossier_append="12345"
        )
    assert failed
    assert body["error"]["limit"] == "max_dossier_bytes"


async def test_lookalikes_past_the_limit_are_named_in_the_tool_next_step(limited) -> None:
    limited(Limits(max_organizations=1))
    async with Client(server) as client:
        _, made = await _call(
            client, "find_or_create_organization", name="Acme", domain="acme.example"
        )
        failed, body = await _call(
            client,
            "link_similar_organizations",
            organization_id=made["organization"]["id"],
            companies=[{"name": "Globex", "domain": "globex.example", "similarity": 0.9}],
        )
    assert not failed
    assert body["skipped"] == 1 and body["limit"] == "max_organizations"
    assert "max_organizations limit" in body["next_step"]


async def test_reopening_a_deal_past_the_open_limit_is_refused_by_the_tool(limited) -> None:
    events = limited(Limits(max_open_opportunities=1))
    async with Client(server) as client:
        _, made = await _call(client, "create_opportunity", title="Pilot", stage="closed_won")
        deal_id = made["opportunity"]["id"]
        failed, _ = await _call(client, "create_opportunity", title="Renewal")
        assert not failed
        failed, body = await _call(client, "move_opportunity", opportunity_id=deal_id, stage="lead")
    assert failed
    assert body["error"]["limit"] == "max_open_opportunities"
    assert "won or lost stage" in body["next_step"]
    assert (PeopleEvent.MAX_OPEN_OPPORTUNITIES_REFUSED, 1) in events


async def test_making_a_closing_stage_open_past_the_limit_is_refused_by_the_tool(limited) -> None:
    events = limited(Limits(max_open_opportunities=1))
    async with Client(server) as client:
        await _call(client, "create_opportunity", title="Pilot")
        _, won = await _call(client, "create_opportunity", title="Renewal", stage="closed_won")
        failed, body = await _call(
            client, "update_pipeline_stage", key="closed_won", outcome="open"
        )
        assert failed
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["limit"] == "max_open_opportunities"
        assert "would reopen its 1 deal" in body["next_step"]
        assert "Close or delete deals first" in body["next_step"]
        _, pipeline = await _call(client, "get_pipeline")
        _, deal = await _call(client, "get_opportunity", opportunity_id=won["opportunity"]["id"])
    stage = next(s for s in pipeline["stages"] if s["key"] == "closed_won")
    assert stage["outcome"] == "won"  # nothing changed
    assert deal["opportunity"]["closed_at"] is not None
    assert (PeopleEvent.MAX_OPEN_OPPORTUNITIES_REFUSED, 1) in events


async def test_making_a_closing_stage_open_under_the_limit_is_allowed_by_the_tool(limited) -> None:
    limited(Limits(max_open_opportunities=2))
    async with Client(server) as client:
        await _call(client, "create_opportunity", title="Pilot")
        await _call(client, "create_opportunity", title="Renewal", stage="closed_won")
        failed, body = await _call(
            client, "update_pipeline_stage", key="closed_won", outcome="open"
        )
    assert not failed, body
    assert body["stage"]["outcome"] == "open"


async def test_a_stage_reopening_no_deal_is_never_refused(ctx) -> None:
    limits = Limits(max_open_opportunities=1)
    await _deal(ctx, limits, "Pilot")
    await _deal(ctx, limits, "Lost", stage="closed_lost")
    # The won stage holds no deal, so making it open reopens none.
    _, action = await _run(
        ctx, store.update_pipeline_stage, key="closed_won", outcome="open", limits=limits
    )
    assert action == "updated"
    # Retiring and restoring a closing stage keeps its outcome, so its deals stay closed.
    await _run(ctx, store.update_pipeline_stage, key="closed_lost", retired=True, limits=limits)
    await _run(ctx, store.update_pipeline_stage, key="closed_lost", retired=False, limits=limits)
    # Restoring it as open in one call does reopen its deal, and counts.
    await _run(ctx, store.update_pipeline_stage, key="closed_lost", retired=True, limits=limits)
    await _refused(
        ctx,
        "max_open_opportunities",
        store.update_pipeline_stage,
        key="closed_lost",
        retired=False,
        outcome="open",
        limits=limits,
    )
