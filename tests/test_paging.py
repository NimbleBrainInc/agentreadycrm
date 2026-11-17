"""Keyset paging of every list read.

Each paged read walks to its end through ``pgkeyset.testing.walk`` with a row inserted
and a row removed between pages: rows seeded in ONE transaction share ``created_at``, so only
the id tiebreak orders them, and the walk meets each once. A cursor from one listing is
refused by another, and a malformed one is refused, both as ``InvalidCursor``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import define_signal_types, make_ctx, session_for
from pgkeyset import InvalidCursor
from pgkeyset.testing import walk
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_people.db import store
from mcp_people.db.tables import contact, opportunity, organization, proof_point, signal
from mcp_people.runtime import Scope

pytestmark = pytest.mark.asyncio

Seed = Callable[[AsyncConnection, Scope, str], Awaitable[str]]
Read = Callable[..., Awaitable[tuple[list[Any], str | None]]]


async def _contact(conn: AsyncConnection, ctx: Scope, name: str) -> str:
    row, _, _ = await store.find_or_create_contact(conn, ctx, name=name, email=f"{name}@ex.example")
    return row.id


async def _organization(conn: AsyncConnection, ctx: Scope, name: str) -> str:
    row, _, _ = await store.find_or_create_organization(
        conn, ctx, name=name, domain=f"{name}.example"
    )
    return row.id


async def _opportunity(conn: AsyncConnection, ctx: Scope, name: str) -> str:
    row, _ = await store.find_or_create_opportunity(conn, ctx, title=f"{name} deal")
    return row.id


async def _signal(conn: AsyncConnection, ctx: Scope, name: str) -> str:
    org_id = await _organization(conn, ctx, f"{name}-org")
    await define_signal_types(conn, ctx, "expansion")
    row, _, _ = await store.record_signal(
        conn,
        ctx,
        organization_id=org_id,
        claim=f"{name} is opening a second site.",
        evidence_quote=f"{name} opens its second site in the spring.",
        source_url=f"https://news.example/{name}",
        signal_type="expansion",
    )
    return row.id


async def _proof_point(conn: AsyncConnection, ctx: Scope, name: str) -> str:
    row, _ = await store.record_proof_point(conn, ctx, claim=f"{name} cut onboarding to a day.")
    return row.id


@dataclass(frozen=True)
class Paged:
    name: str
    seed: Seed
    read: Read
    table: Table


LISTINGS = [
    Paged("contacts", _contact, store.list_contacts, contact),
    Paged("organizations", _organization, store.list_organizations, organization),
    Paged("opportunities", _opportunity, store.list_opportunities, opportunity),
    Paged("signals", _signal, store.list_signals, signal),
    Paged("proof_points", _proof_point, store.list_proof_points, proof_point),
]


async def _seed(ctx: Scope, paged: Paged, prefix: str, n: int) -> list[str]:
    async with session_for(ctx) as conn:
        return [await paged.seed(conn, ctx, f"{prefix}{i}") for i in range(n)]


@pytest.mark.parametrize("paged", LISTINGS, ids=lambda p: p.name)
async def test_a_walk_meets_every_row_once_as_the_list_changes(app_engine, paged: Paged):
    ctx = make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")
    seeded = await _seed(ctx, paged, "p", 7)
    # Newest first, ids breaking the tie: max(seeded) is read on page 1, min(seeded) is last.
    read_already, not_reached = max(seeded), min(seeded)

    async def between(page_number: int) -> None:
        if page_number != 1:
            return
        await _seed(ctx, paged, "late", 2)  # newer: sorts ahead of the walk
        async with session_for(ctx) as conn:
            ids = [read_already, not_reached]
            await conn.execute(paged.table.delete().where(paged.table.c.id.in_(ids)))

    async def fetch(cursor: str | None) -> tuple[list[Any], str | None]:
        async with session_for(ctx) as conn:
            return await paged.read(conn, ctx, limit=3, cursor=cursor)

    expected = set(seeded) - {not_reached}
    walked = await walk(fetch, ident=lambda r: r.id, expected=expected, between_pages=between)
    assert [r.id for r in walked] == sorted(expected, reverse=True)


async def test_a_cursor_from_another_listing_is_refused(app_engine):
    ctx = make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")
    await _seed(ctx, LISTINGS[0], "c", 3)
    async with session_for(ctx) as conn:
        _, cursor = await store.list_contacts(conn, ctx, limit=1)
    assert cursor is not None
    for paged in LISTINGS[1:]:
        with pytest.raises(InvalidCursor):
            async with session_for(ctx) as conn:
                await paged.read(conn, ctx, cursor=cursor)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        "W10",  # []
        # The untagged `created_at \x1f id` form: refused, so the caller restarts once.
        base64.urlsafe_b64encode(b"2026-01-01T00:00:00+00:00\x1fct_1").decode(),
    ],
)
async def test_a_malformed_cursor_is_refused(app_engine, cursor: str):
    ctx = make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")
    for paged in LISTINGS:
        with pytest.raises(InvalidCursor):
            async with session_for(ctx) as conn:
                await paged.read(conn, ctx, cursor=cursor)
