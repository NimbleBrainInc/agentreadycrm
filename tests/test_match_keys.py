"""The store matches records on the keys the application computes (``mcp_people.keys``).

Names fold with NFKC and full case folding, so ``Straße`` and ``STRASSE`` are one name;
emails and domains with NFKC and lowercasing, so ``straße.de`` and ``strasse.de`` stay two
hosts. Every path that writes a name, an email or a domain writes its key with it (the
``_keys_in_sync`` fixture in ``conftest.py`` checks every row after every test); these
tests hold the matches that follow and the keys a few paths leave.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import func, select

from mcp_people.db import store
from mcp_people.db.tables import contact, organization
from mcp_people.keys import match_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _count(ctx, table) -> int:
    async with session_for(ctx) as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def _keys(ctx, table, record_id: str) -> dict:
    columns = (
        (table.c.name, table.c.name_key, table.c.domain, table.c.domain_key)
        if table is organization
        else (table.c.name, table.c.name_key, table.c.email, table.c.email_key)
    )
    async with session_for(ctx) as conn:
        row = (await conn.execute(select(*columns).where(table.c.id == record_id))).mappings().one()
    return dict(row)


async def _org(ctx, **kwargs):
    async with session_for(ctx) as conn:
        return await store.find_or_create_organization(conn, ctx, **kwargs)


async def _contact(ctx, **kwargs):
    async with session_for(ctx) as conn:
        return await store.find_or_create_contact(conn, ctx, **kwargs)


# --------------------------------------------------------------------------- #
# find_or_create                                                               #
# --------------------------------------------------------------------------- #

# (first spelling, second spelling, whether they are one record)
_ORG_NAMES = [
    ("Émile Zola SA", "ÉMILE ZOLA SA", True),
    ("Straße GmbH", "STRASSE GMBH", True),
    ("ﬁnance co", "Finance Co", True),
    ("ＡＣＭＥ Ｗｉｄｇｅｔｓ", "Acme Widgets", True),
    ("ıstanbul Ltd", "Istanbul Ltd", False),
]


@pytest.mark.parametrize(("first", "second", "same"), _ORG_NAMES)
async def test_a_domainless_organization_matches_on_its_folded_name(
    ctx, first: str, second: str, same: bool
) -> None:
    created, action, _ = await _org(ctx, name=first)
    assert action == "created"
    again, action, _ = await _org(ctx, name=second)
    assert (again.id == created.id) is same
    assert action == ("matched" if same else "created")
    assert await _count(ctx, organization) == (1 if same else 2)


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("ＡＣＭＥ.com", "acme.com", True),
        ("STRASSE.de", "strasse.de", True),
        ("straße.de", "strasse.de", False),
    ],
)
async def test_an_organization_matches_on_its_lowercased_domain(
    ctx, first: str, second: str, same: bool
) -> None:
    created, _, _ = await _org(ctx, name="One", domain=first)
    again, action, _ = await _org(ctx, name="Two", domain=second)
    assert (again.id == created.id) is same
    assert action == ("matched" if same else "created")


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("Émile@Example.com", "émile@example.com", True),
        ("ＥＭＩＬＥ@example.com", "emile@example.com", True),
        ("straße@example.de", "strasse@example.de", False),
    ],
)
async def test_a_contact_matches_on_its_lowercased_email(
    ctx, first: str, second: str, same: bool
) -> None:
    created, _, _ = await _contact(ctx, name="Émile", email=first)
    again, action, _ = await _contact(ctx, name="Émile", email=second)
    assert (again.id == created.id) is same
    assert action == ("matched" if same else "created")


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("Émile Zola", "ÉMILE ZOLA", True),
        ("Jürgen Straße", "JÜRGEN STRASSE", True),
        ("Ｊｏ Ｌｅｅ", "jo lee", True),
        ("ıvan", "Ivan", False),
    ],
)
async def test_a_contact_without_email_matches_on_its_folded_name(
    ctx, first: str, second: str, same: bool
) -> None:
    created, _, _ = await _contact(ctx, name=first)
    again, action, _ = await _contact(ctx, name=second)
    assert (again.id == created.id) is same
    assert action == ("matched" if same else "created")


async def test_a_contact_wires_to_the_organization_its_folded_name_names(ctx) -> None:
    org, _, _ = await _org(ctx, name="Straße GmbH")
    person, _, _ = await _contact(ctx, name="Ann", organization_name="STRASSE GMBH")
    assert person.organization_id == org.id
    assert await _count(ctx, organization) == 1


# --------------------------------------------------------------------------- #
# Imports                                                                      #
# --------------------------------------------------------------------------- #


async def test_an_organization_import_dedupes_on_the_keys(ctx) -> None:
    rows = [
        {"name": "Straße GmbH"},
        {"name": "STRASSE GMBH"},
        {"name": "ﬁnance co"},
        {"name": "Finance Co"},
        {"name": "One", "domain": "ＡＣＭＥ.com"},
        {"name": "Two", "domain": "acme.com"},
        {"name": "Three", "domain": "straße.de"},
        {"name": "Four", "domain": "strasse.de"},
    ]
    async with session_for(ctx) as conn:
        result = await store.import_organizations(conn, ctx, rows=rows)
    assert (result["created"], result["matched"]) == (5, 3)
    assert await _count(ctx, organization) == 5


async def test_a_contact_import_dedupes_on_the_keys(ctx) -> None:
    rows = [
        {"name": "Émile Zola", "email": "Émile@Example.com"},
        {"name": "Émile Zola", "email": "émile@example.com"},
        {"name": "Jürgen Straße"},
        {"name": "JÜRGEN STRASSE"},
        {"name": "Ann", "email": "straße@example.de"},
        {"name": "Ann", "email": "strasse@example.de"},
    ]
    async with session_for(ctx) as conn:
        result = await store.import_contacts(conn, ctx, rows=rows)
    assert (result["created"], result["matched"]) == (4, 2)
    assert await _count(ctx, contact) == 4


# --------------------------------------------------------------------------- #
# Updates and merges keep the keys                                             #
# --------------------------------------------------------------------------- #


async def test_update_organization_rewrites_the_keys(ctx) -> None:
    org, _, _ = await _org(ctx, name="Acme")
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org.id, name="Straße GmbH", domain="ＡＣＭＥ.com"
        )
    assert await _keys(ctx, organization, org.id) == {
        "name": "Straße GmbH",
        "name_key": "strasse gmbh",
        "domain": "ａｃｍｅ.com",
        "domain_key": "acme.com",
    }
    again, action, _ = await _org(ctx, name="Other", domain="acme.com")
    assert (again.id, action) == (org.id, "matched")


async def test_a_rename_onto_a_folded_namesake_is_a_conflict(ctx) -> None:
    await _org(ctx, name="Straße GmbH")
    other, _, _ = await _org(ctx, name="Other")
    with pytest.raises(store.ConflictError):
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=other.id, name="STRASSE GMBH")


async def test_update_contact_rewrites_the_keys(ctx) -> None:
    person, _, _ = await _contact(ctx, name="Ann")
    async with session_for(ctx) as conn:
        await store.update_contact(
            conn, ctx, contact_id=person.id, name="Émile Straße", email="Émile@Example.com"
        )
    assert await _keys(ctx, contact, person.id) == {
        "name": "Émile Straße",
        "name_key": "émile strasse",
        "email": "émile@example.com",
        "email_key": "émile@example.com",
    }


async def test_a_filled_email_writes_its_key(ctx) -> None:
    person, _, _ = await _contact(ctx, name="Émile Zola", external_refs=["crm://people/1"])
    filled, action, _ = await _contact(
        ctx, external_refs=["crm://people/1"], email="ＥＭＩＬＥ@example.com"
    )
    assert (filled.id, action) == (person.id, "matched")
    assert (await _keys(ctx, contact, person.id))["email_key"] == "emile@example.com"


async def test_merge_contacts_moves_the_email_key(ctx) -> None:
    target, _, _ = await _contact(ctx, name="Émile Zola")
    source, _, _ = await _contact(ctx, name="E. Zola", email="Émile@Example.com")
    async with session_for(ctx) as conn:
        await store.merge_contacts(conn, ctx, target_id=target.id, source_id=source.id)
    assert (await _keys(ctx, contact, target.id))["email_key"] == "émile@example.com"
    assert (await _keys(ctx, contact, source.id))["email_key"] is None
    again, action, _ = await _contact(ctx, name="x", email="ÉMILE@example.com")
    assert (again.id, action) == (target.id, "matched")


async def test_merge_organizations_moves_the_keys(ctx) -> None:
    target, _, _ = await _org(ctx, name="Straße GmbH")
    source, _, _ = await _org(ctx, name="Strasse Holding", domain="strasse.example")
    async with session_for(ctx) as conn:
        await store.merge_organizations(conn, ctx, target_id=target.id, source_id=source.id)
    assert (await _keys(ctx, organization, target.id))["domain_key"] == "strasse.example"
    archived = await _keys(ctx, organization, source.id)
    assert archived["domain_key"] is None
    assert archived["name_key"] == match_key(archived["name"])
    again, action, _ = await _org(ctx, name="x", domain="STRASSE.example")
    assert (again.id, action) == (target.id, "matched")


# --------------------------------------------------------------------------- #
# Filters compare the same keys                                                #
# --------------------------------------------------------------------------- #


async def test_a_filter_on_a_keyed_field_folds_its_value_the_same_way(ctx) -> None:
    straße, _, _ = await _org(ctx, name="Straße GmbH", domain="straße.de")
    strasse, _, _ = await _org(ctx, name="Strasse AG", domain="strasse.de")

    async def ids(filter) -> set[str]:
        async with session_for(ctx) as conn:
            page = await store.page_organizations(conn, ctx, filter=filter)
        return {o.id for o in page.rows}

    assert await ids({"field": "name", "op": "eq", "value": "STRASSE GMBH"}) == {straße.id}
    assert await ids({"field": "name", "op": "starts_with", "value": "STRASSE"}) == {
        straße.id,
        strasse.id,
    }
    assert await ids({"field": "domain", "op": "eq", "value": "STRASSE.de"}) == {strasse.id}
    assert await ids({"field": "domain", "op": "eq", "value": "ＳＴＲＡßＥ.de"}) == {straße.id}


async def test_a_count_groups_a_keyed_field_on_its_key(ctx) -> None:
    await _org(ctx, name="Straße GmbH", domain="a.example")
    await _org(ctx, name="STRASSE GMBH", domain="b.example")
    async with session_for(ctx) as conn:
        counted = await store.count_by(conn, ctx, entity="organization", field="name")
    assert [v["count"] for v in counted["values"]] == [2]
