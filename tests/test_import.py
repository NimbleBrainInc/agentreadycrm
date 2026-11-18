"""Batch imports.

What these pin: an import handles each row exactly as find-or-create would (dedupe,
fill-only, convergence on a re-run); a matched row whose value the record keeps is
named, never dropped quietly; a failed row is rolled back alone and reported by
reason and index while the rest commit; a contact row wires its organization by
domain without orphaning one; and a list over the cap is refused whole.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

import mcp_people.server as server
from mcp_people.db import store
from mcp_people.db.tables import contact, organization, people_audit

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _import_orgs(ctx, rows, **kwargs):
    async with session_for(ctx) as conn:
        return await store.import_organizations(conn, ctx, rows=rows, **kwargs)


async def _import_contacts(ctx, rows, **kwargs):
    async with session_for(ctx) as conn:
        return await store.import_contacts(conn, ctx, rows=rows, **kwargs)


async def _count(ctx, table) -> int:
    async with session_for(ctx) as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def _orgs(ctx) -> dict:
    async with session_for(ctx) as conn:
        orgs, _ = await store.list_organizations(conn, ctx)
    return {o.domain: o for o in orgs}


async def _define(ctx, **kwargs):
    async with session_for(ctx) as conn:
        await store.define_field(conn, ctx, **kwargs)


def _rows_by_reason(groups) -> dict[str, list[int]]:
    return {g["reason"]: g["rows"] for g in groups}


async def test_import_organizations_converges_and_fills_only(ctx) -> None:
    await _define(ctx, entity="organization", key="vendor_id", label="Vendor id", field_type="text")
    rows = [
        {"name": "Larkfield", "domain": "larkfield.example", "fields": {"vendor_id": "v-1"}},
        {"name": "Brightwater", "domain": "https://www.brightwater.example/", "industry": ""},
        {"domain": "https://www.nameless.example/"},
    ]
    first = await _import_orgs(ctx, rows)
    assert first == {
        "received": 3,
        "created": 3,
        "matched": 0,
        "promoted": 0,
        "kept": [],
        "skipped": [],
    }

    again = await _import_orgs(ctx, rows)
    assert (again["created"], again["matched"], again["kept"]) == (0, 3, [])
    assert await _count(ctx, organization) == 3

    by_domain = await _orgs(ctx)
    lark = by_domain["larkfield.example"]
    assert (lark.name, lark.source, lark.fields) == ("Larkfield", "import", {"vendor_id": "v-1"})
    # A blank cell is absent; a row with no name is named by its normalized domain.
    assert by_domain["brightwater.example"].industry is None
    assert by_domain["nameless.example"].name == "nameless.example"


async def test_a_match_that_keeps_its_own_value_is_named(ctx) -> None:
    """A re-import fills only what is empty. A row whose value differs is not
    written, and is named under kept rather than reported as a plain match."""
    await _define(ctx, entity="organization", key="vendor_id", label="Vendor id", field_type="text")
    await _define(
        ctx, entity="organization", key="headcount", label="Headcount", field_type="number"
    )
    await _import_orgs(
        ctx,
        [
            {
                "name": "Larkfield",
                "domain": "larkfield.example",
                "industry": "Logistics",
                "fields": {"vendor_id": "v-1", "headcount": "27"},
            }
        ],
    )
    result = await _import_orgs(
        ctx,
        [
            # Same values, one in another form: nothing kept.
            {"domain": "larkfield.example", "fields": {"vendor_id": "v-1", "headcount": "27.0"}},
            # A different vendor id and industry for the same domain: both kept.
            {
                "name": "Larkfield Inc",
                "domain": "larkfield.example",
                "industry": "Freight",
                "fields": {"vendor_id": "v-2"},
            },
            # A value the record lacks is filled, not kept.
            {"domain": "larkfield.example", "region": "EMEA"},
        ],
    )
    assert result["matched"] == 3
    assert _rows_by_reason(result["kept"]) == {
        "matched an organization that keeps its own industry": [1],
        "matched an organization that keeps its own vendor_id": [1],
    }
    lark = (await _orgs(ctx))["larkfield.example"]
    assert (lark.industry, lark.region, lark.fields["vendor_id"]) == ("Logistics", "EMEA", "v-1")


async def test_import_skips_failed_rows_by_reason_and_saves_the_rest(ctx) -> None:
    rows = [
        {"name": "Keeps", "domain": "keeps.example"},
        {"website": "nowhere.example"},
        {"name": "Undefined", "domain": "undefined.example", "fields": {"nope": "x"}},
        {"name": "Typo", "domian": "typo.example"},
        {"name": "Also keeps", "domain": "also.example"},
        {"website": "elsewhere.example"},
        {"name": "Not text", "domain": 42},
        {"name": "Nul\x00byte", "domain": "nul.example"},
    ]
    result = await _import_orgs(ctx, rows)
    assert (result["created"], result["matched"]) == (3, 0)
    skipped = _rows_by_reason(result["skipped"])
    assert skipped["a row needs a name or a domain, or a reference held here"] == [1, 5]
    assert skipped["domain must be text"] == [6]
    assert any(rows == [2] and "nope" in reason for reason, rows in skipped.items())
    assert any(rows == [3] and "domian" in reason for reason, rows in skipped.items())
    # The failed rows left nothing behind, and a NUL in a cell is dropped, not fatal.
    assert await _count(ctx, organization) == 3
    assert (await _orgs(ctx))["nul.example"].name == "Nulbyte"


async def test_reason_groups_are_capped(ctx) -> None:
    rows = [{"name": f"Org {i}", f"col_{i}": "x"} for i in range(30)]
    result = await _import_orgs(ctx, rows)
    assert len(result["skipped"]) == 20
    assert result["skipped"][-1] == {"reason": "11 other reasons", "rows": list(range(19, 30))}


async def test_import_contacts_wires_the_organization_by_domain(ctx) -> None:
    rows = [
        {
            "name": "Dana Reyes",
            "email": "dana@larkfield.example",
            "organization_domain": "larkfield.example",
            "organization_name": "Larkfield",
        },
        {"email": "sam@larkfield.example", "organization_domain": "LARKFIELD.example"},
        {"email": "lee@brightwater.example", "organization_domain": "brightwater.example"},
        # No email: matched by name within the domain's organization.
        {"name": "Kim Park", "organization_domain": "brightwater.example"},
    ]
    result = await _import_contacts(ctx, rows)
    assert (result["created"], result["matched"], result["skipped"]) == (4, 0, [])

    by_domain = await _orgs(ctx)
    lark, bright = by_domain["larkfield.example"], by_domain["brightwater.example"]
    assert (lark.name, lark.source, bright.name) == ("Larkfield", "import", "brightwater.example")
    async with session_for(ctx) as conn:
        contacts, _ = await store.list_contacts(conn, ctx)
    wired = {c.email or c.name: c.organization_id for c in contacts}
    assert wired == {
        "dana@larkfield.example": lark.id,
        "sam@larkfield.example": lark.id,
        "lee@brightwater.example": bright.id,
        "Kim Park": bright.id,
    }
    again = await _import_contacts(ctx, rows)
    assert (again["created"], again["matched"]) == (0, 4)
    assert await _count(ctx, organization) == 2


async def test_a_matched_contact_with_an_organization_orphans_none(ctx) -> None:
    """A contact already wired to an organization keeps it, and the row's domain
    creates no organization nothing points at."""
    async with session_for(ctx) as conn:
        await store.find_or_create_contact(
            conn, ctx, email="dana@larkfield.example", organization_name="Larkfield"
        )
    result = await _import_contacts(
        ctx, [{"email": "dana@larkfield.example", "organization_domain": "elsewhere.example"}]
    )
    assert result["matched"] == 1
    assert await _count(ctx, organization) == 1


async def test_a_moved_contact_keeps_its_organization_and_status_and_says_so(ctx) -> None:
    """A matched contact is never rewired or moved along the lifecycle. A row naming
    another organization, by domain, name or id, or another status or email status,
    is named under kept; a row naming the one it holds is a plain match."""
    first = await _import_contacts(
        ctx,
        [
            {
                "email": "ana@a.example",
                "organization_domain": "a.example",
                "organization_name": "Alder",
                "status": "sourced",
                "email_status": "valid",
            },
            {"email": "bo@b.example", "organization_domain": "b.example"},
        ],
    )
    assert first["created"] == 2
    b_id = (await _orgs(ctx))["b.example"].id
    result = await _import_contacts(
        ctx,
        [
            {"email": "ana@a.example", "organization_domain": "b.example", "status": "Engaged"},
            {"email": "ana@a.example", "organization_name": "Birch", "email_status": "invalid"},
            {"email": "ana@a.example", "organization_id": b_id},
            {"email": "ana@a.example", "organization_domain": "A.example", "status": "sourced"},
            {"email": "ana@a.example", "organization_name": "alder", "email_status": "VALID"},
        ],
    )
    assert (result["matched"], result["skipped"]) == (5, [])
    assert _rows_by_reason(result["kept"]) == {
        "matched a contact that keeps its own status": [0],
        "matched a contact that keeps its own organization": [0, 1, 2],
        "matched a contact that keeps its own email_status": [1],
    }
    assert await _count(ctx, organization) == 2
    async with session_for(ctx) as conn:
        contacts, _ = await store.list_contacts(conn, ctx)
    ana = next(c for c in contacts if c.email == "ana@a.example")
    a_id = (await _orgs(ctx))["a.example"].id
    assert (ana.organization_id, ana.status, ana.email_status) == (a_id, "sourced", "valid")


async def test_a_contact_row_failing_after_its_write_leaves_nothing(ctx) -> None:
    """An email_status with no address raises after the contact is written; the
    row's savepoint rolls it back, and the next row still saves."""
    rows = [
        {"name": "No Address", "email_status": "valid"},
        {"name": "Fine", "email": "fine@example.com", "status": "Contacted"},
        {"name": "Elsewhere", "organization_id": "org_does_not_exist"},
        {"name": "Vendor status", "email": "v@example.com", "status": "Customer"},
    ]
    result = await _import_contacts(ctx, rows)
    assert result["created"] == 1
    skipped = _rows_by_reason(result["skipped"])
    assert skipped["organization_id is not an organization in this workspace"] == [2]
    assert skipped["status is one of sourced, enriched, contacted, engaged"] == [3]
    assert sorted(i for rows in skipped.values() for i in rows) == [0, 2, 3]
    assert await _count(ctx, contact) == 1
    async with session_for(ctx) as conn:
        contacts, _ = await store.list_contacts(conn, ctx)
    assert contacts[0].status == "contacted"


async def test_import_over_the_cap_is_refused_whole(ctx) -> None:
    rows = [{"name": f"Org {i}"} for i in range(store.IMPORT_MAX_ROWS + 1)]
    with pytest.raises(ValueError, match="split it"):
        await _import_orgs(ctx, rows)
    assert await _count(ctx, organization) == 0


async def test_an_import_writes_one_summary_audit_row(ctx) -> None:
    await _import_orgs(ctx, [{"name": "A", "domain": "a.example"}, {"website": "x"}])
    async with session_for(ctx) as conn:
        details = (
            (
                await conn.execute(
                    select(people_audit.c.detail).where(people_audit.c.action == "import")
                )
            )
            .scalars()
            .all()
        )
    assert details == [{"created": 1, "matched": 0, "promoted": 0, "received": 2, "skipped": 1}]


class _Deadlock(Exception):
    sqlstate = "40P01"


class _Other(Exception):
    sqlstate = "23505"


def _dbapi(orig: Exception) -> DBAPIError:
    return DBAPIError("statement", {}, orig)


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Each session _run_import opens, with no database behind it."""
    opened: list[object] = []

    @asynccontextmanager
    async def _session(_sctx):
        conn = object()
        opened.append(conn)
        yield conn

    monkeypatch.setattr(server.runtime(), "session", _session)
    return opened


async def test_an_import_ended_for_a_deadlock_runs_once_more(sessions) -> None:
    calls: list[object] = []

    async def run(conn):
        calls.append(conn)
        if len(calls) == 1:
            raise _dbapi(_Deadlock())
        return {"ok": True}

    assert await server._run_import(None, run) == {"ok": True}
    assert calls == sessions and len(calls) == 2


@pytest.mark.parametrize("errors", [[_Other()], [_Deadlock(), _Deadlock()]])
async def test_an_import_fails_on_another_error_or_a_second_deadlock(sessions, errors) -> None:
    pending = list(errors)

    async def run(_conn):
        raise _dbapi(pending.pop(0))

    with pytest.raises(DBAPIError) as raised:
        await server._run_import(None, run)
    assert raised.value.orig is errors[-1]
    assert len(sessions) == len(errors)
