"""External references.

What these pin: a reference names one record and is matched before domain, email or
name, so a re-pull lands on the record it named even after the company's domain or the
person's address changed; a reference is never moved off the record that holds it, and
the conflict names both records; a record matched by name alone that already holds other
references is not bound to the row's; merges carry references to the record kept; the
lookup answers a batch in one call; and two concurrent creates of one referenced record
converge on one row.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import func, select

from mcp_people.db import store
from mcp_people.db.store import RefConflictError
from mcp_people.db.tables import external_ref, organization
from mcp_people.refs import clean_ref, clean_refs

pytestmark = pytest.mark.asyncio

REF = "vendor://companies/1"


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _org(ctx, **kwargs):
    async with session_for(ctx) as conn:
        org, action, _ = await store.find_or_create_organization(conn, ctx, **kwargs)
    return org, action


async def _refs(ctx, entity: str, record_id: str) -> list[str]:
    async with session_for(ctx) as conn:
        refs, _total = await store.external_refs_of(conn, entity=entity, record_id=record_id)
    return refs


async def _count(ctx, table) -> int:
    async with session_for(ctx) as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def test_a_reference_is_an_absolute_uri_kept_as_given() -> None:
    assert clean_ref("  vendor://companies/AbC  ") == "vendor://companies/AbC"
    for bad in [
        "companies/1",
        "Vendor://x",
        "vendor://has space",
        "people://organizations/org_1",
        7,
    ]:
        with pytest.raises(ValueError):
            clean_ref(bad)
    with pytest.raises(ValueError, match="at most 512 bytes"):
        clean_ref("vendor://" + "x" * 600)
    assert clean_refs(["a:1", "a:1", "b:2"]) == ["a:1", "b:2"]
    with pytest.raises(ValueError, match="list of URIs"):
        clean_refs("a:1")


async def test_a_reference_matches_before_the_domain(ctx) -> None:
    """A re-pull names the company by its reference; its domain changed in between."""
    first, action = await _org(
        ctx, name="Larkfield", domain="larkfield.example", external_refs=[REF]
    )
    assert action == "created"
    again, action = await _org(ctx, name="Larkfield", domain="larkfield.io", external_refs=[REF])
    assert (again.id, action, again.domain) == (first.id, "matched", "larkfield.example")
    assert await _count(ctx, organization) == 1
    assert await _refs(ctx, "organization", first.id) == [REF]


async def test_a_reference_is_attached_to_a_match_by_domain_and_found_later(ctx) -> None:
    held, _ = await _org(ctx, name="Larkfield", domain="larkfield.example")
    matched, action = await _org(
        ctx, name="Larkfield", domain="larkfield.example", external_refs=[REF]
    )
    assert (matched.id, action) == (held.id, "matched")
    async with session_for(ctx) as conn:
        result = await store.find_by_external_refs(conn, ctx, refs=[REF, "vendor://companies/2"])
    assert result["missing"] == ["vendor://companies/2"]
    assert [(f["ref"], f["entity"], f["id"], f["name"]) for f in result["found"]] == [
        (REF, "organization", held.id, "Larkfield")
    ]


async def test_a_reference_is_never_moved_and_the_conflict_names_both(ctx) -> None:
    a, _ = await _org(ctx, name="Alpha", external_refs=[REF])
    b, _ = await _org(ctx, name="Beta", domain="beta.example")
    # The reference names Alpha; the row's domain belongs to Beta.
    with pytest.raises(RefConflictError) as caught:
        await _org(ctx, name="Alpha", domain="beta.example", external_refs=[REF])
    assert a.id in caught.value.message and b.id in caught.value.message
    assert "merge_organizations" in caught.value.next_step
    # Beta's update cannot take Alpha's reference.
    with pytest.raises(RefConflictError) as conflict_1:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=b.id, add_external_refs=[REF])
    assert re.search(a.id, conflict_1.value.message)
    assert await _refs(ctx, "organization", a.id) == [REF]


async def test_references_naming_two_records_or_the_other_kind_conflict(ctx) -> None:
    await _org(ctx, name="Alpha", external_refs=["vendor://companies/a"])
    await _org(ctx, name="Beta", external_refs=["vendor://companies/b"])
    with pytest.raises(RefConflictError) as conflict_2:
        await _org(
            ctx, name="Gamma", external_refs=["vendor://companies/a", "vendor://companies/b"]
        )
    assert re.search("two organization records", conflict_2.value.message)
    async with session_for(ctx) as conn:
        await store.find_or_create_contact(
            conn, ctx, email="dana@x.example", external_refs=["vendor://people/1"]
        )
    with pytest.raises(RefConflictError) as conflict_3:
        await _org(ctx, name="Delta", external_refs=["vendor://people/1"])
    assert re.search("not this organization", conflict_3.value.message)


async def test_a_name_only_match_holding_other_references_is_not_bound(ctx) -> None:
    """Two companies share a name; the one on file already carries another source's id."""
    await _org(ctx, name="First Baptist Church", external_refs=["vendor://companies/100"])
    with pytest.raises(RefConflictError) as conflict_4:
        await _org(ctx, name="First Baptist Church", external_refs=["vendor://companies/200"])
    assert re.search("by name only", conflict_4.value.message)
    # A name match holding no references takes the row's.
    plain, _ = await _org(ctx, name="Solo Co")
    matched, action = await _org(ctx, name="Solo Co", external_refs=["vendor://companies/300"])
    assert (matched.id, action) == (plain.id, "matched")


async def test_update_adds_and_removes_references(ctx) -> None:
    org, _ = await _org(ctx, name="Larkfield")
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org.id, add_external_refs=[REF, "vendor://x/2"]
        )
    assert await _refs(ctx, "organization", org.id) == [REF, "vendor://x/2"]
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org.id, remove_external_refs=[REF, "vendor://never"]
        )
    assert await _refs(ctx, "organization", org.id) == ["vendor://x/2"]


async def test_detaching_marks_the_reference_and_reattaching_inserts_a_new_one(ctx) -> None:
    """Detaching removes no row: it marks the reference, every read leaves it out, and the
    URI is free at once, for this record again or for another."""
    org, _ = await _org(ctx, name="Larkfield", external_refs=[REF])
    other, _ = await _org(ctx, name="Brightwater")
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org.id, remove_external_refs=[REF])
        found = await store.find_by_external_refs(conn, ctx, refs=[REF])
        rows = (await conn.execute(select(external_ref))).mappings().all()
    assert await _refs(ctx, "organization", org.id) == []
    assert found["found"] == [] and found["missing"] == [REF]
    assert [(r["uri"], r["organization_id"], r["deleted_at"] is not None) for r in rows] == [
        (REF, org.id, True)
    ]
    # Attaching it again inserts a new row; the marked one stays as it was.
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org.id, add_external_refs=[REF])
    assert await _refs(ctx, "organization", org.id) == [REF]
    assert await _count(ctx, external_ref) == 2
    # Detached again, the URI can name another record.
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org.id, remove_external_refs=[REF])
        await store.update_organization(conn, ctx, org_id=other.id, add_external_refs=[REF])
    matched, action = await _org(ctx, name="Anything", external_refs=[REF])
    assert (matched.id, action) == (other.id, "matched")
    assert await _count(ctx, external_ref) == 3


async def test_a_detached_reference_does_not_count_against_a_name_match(ctx) -> None:
    """A record matched by name that holds only detached references holds none."""
    org, _ = await _org(ctx, name="Larkfield", external_refs=["vendor://old/1"])
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org.id, remove_external_refs=["vendor://old/1"]
        )
    again, action = await _org(ctx, name="Larkfield", external_refs=["vendor://new/1"])
    assert (again.id, action) == (org.id, "matched")
    assert await _refs(ctx, "organization", org.id) == ["vendor://new/1"]


async def test_merge_carries_references_to_the_record_kept(ctx) -> None:
    keep, _ = await _org(ctx, name="Larkfield", domain="larkfield.example")
    dup, _ = await _org(ctx, name="Larkfield Inc", external_refs=[REF])
    async with session_for(ctx) as conn:
        _org_kept, summary = await store.merge_organizations(
            conn, ctx, target_id=keep.id, source_id=dup.id
        )
    assert summary["refs_moved"] == 1
    # A re-pull by the reference now lands on the record kept, not the archived one.
    again, action = await _org(ctx, name="Larkfield Inc", external_refs=[REF])
    assert (again.id, action) == (keep.id, "matched")

    async with session_for(ctx) as conn:
        c_keep, _, _ = await store.find_or_create_contact(conn, ctx, email="dana@larkfield.example")
        c_dup, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana R", external_refs=["vendor://people/9"]
        )
    async with session_for(ctx) as conn:
        _c, csummary = await store.merge_contacts(
            conn, ctx, target_id=c_keep.id, source_id=c_dup.id
        )
    assert csummary["refs_moved"] == 1
    assert await _refs(ctx, "contact", c_keep.id) == ["vendor://people/9"]


async def test_a_contact_reference_matches_before_the_email(ctx) -> None:
    async with session_for(ctx) as conn:
        first, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana", email="dana@old.example", external_refs=["vendor://people/1"]
        )
    async with session_for(ctx) as conn:
        again, action, _ = await store.find_or_create_contact(
            conn, ctx, email="dana@new.example", external_refs=["vendor://people/1"]
        )
    assert (again.id, action, again.email) == (first.id, "matched", "dana@old.example")


async def test_the_lookup_refuses_a_malformed_reference(ctx) -> None:
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="not an absolute URI"):
            await store.find_by_external_refs(conn, ctx, refs=["companies/1"])


async def test_imports_match_on_references_and_report_what_differs(ctx) -> None:
    async with session_for(ctx) as conn:
        await store.import_organizations(
            conn,
            ctx,
            rows=[{"name": "Larkfield", "domain": "larkfield.example", "external_refs": [REF]}],
        )
    async with session_for(ctx) as conn:
        result = await store.import_organizations(
            conn,
            ctx,
            rows=[
                # A re-pull with a new domain: matched by reference, the domain kept.
                {"name": "Larkfield", "domain": "larkfield.io", "external_refs": [REF]},
                # A reference-only row for a held company: matched.
                {"external_refs": [REF]},
                # A reference-only row nothing holds: skipped.
                {"external_refs": ["vendor://companies/404"]},
            ],
        )
    assert (result["created"], result["matched"]) == (0, 2)
    assert {g["reason"]: g["rows"] for g in result["kept"]} == {
        "matched an organization that keeps its own domain": [0]
    }
    assert {g["reason"]: g["rows"] for g in result["skipped"]} == {
        "a row needs a name or a domain, or a reference held here": [2]
    }
    assert await _count(ctx, organization) == 1


async def test_an_import_conflict_is_skipped_with_its_reason(ctx) -> None:
    a, _ = await _org(ctx, name="Alpha", external_refs=[REF])
    await _org(ctx, name="Beta", domain="beta.example")
    async with session_for(ctx) as conn:
        result = await store.import_organizations(
            conn, ctx, rows=[{"name": "Alpha", "domain": "beta.example", "external_refs": [REF]}]
        )
    [group] = result["skipped"]
    assert a.id in group["reason"] and group["rows"] == [0]


async def test_concurrent_creates_of_one_referenced_record_converge(ctx) -> None:
    """Two pulls create the same company at once under different domains; the reference
    is the only thing they share. The loser retries through the reference and matches."""

    async def pull(domain: str):
        async with session_for(ctx) as conn:
            org, action, _ = await store.find_or_create_organization(
                conn, ctx, name="Larkfield", domain=domain, external_refs=[REF]
            )
        return org.id, action

    results = await asyncio.gather(pull("larkfield.example"), pull("larkfield.io"))
    assert {rid for rid, _ in results} == {results[0][0]}
    assert sorted(action for _, action in results) == ["created", "matched"]
    assert await _count(ctx, organization) == 1
    assert await _count(ctx, external_ref) == 1


async def test_concurrent_creates_of_one_domainless_referenced_record_converge(ctx) -> None:
    """With no domain, the loser matches the winner by name; the reference the winner
    attached is the row's own, not another source's, so the name-only guard lets it pass."""

    async def pull():
        async with session_for(ctx) as conn:
            org, action, _ = await store.find_or_create_organization(
                conn, ctx, name="Larkfield", external_refs=[REF]
            )
        return org.id, action

    results = await asyncio.gather(pull(), pull())
    assert {rid for rid, _ in results} == {results[0][0]}
    assert sorted(action for _, action in results) == ["created", "matched"]
    assert await _count(ctx, organization) == 1
    assert await _count(ctx, external_ref) == 1
