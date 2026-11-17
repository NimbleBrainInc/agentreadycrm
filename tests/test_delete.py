"""Deletion.

What these pin: no delete removes a row or changes a value. ``delete_contact`` sets
``deleted_at`` on the contact, the duplicates merged into it, their interactions, the signals
about them and their external references, and on nothing else. ``delete_organization``
refuses while a live contact belongs to the company or, unless asked, a live opportunity names
it; then marks the company, its duplicates, signals, proof points, references and (when
asked) deals. Reads leave marked rows out and read a link to one as absent; the change log is
as it was, plus one ``delete`` row of counts. A deleted record holds no key against a new one:
the same email, domain, name, reference, source or idempotency key makes a new record, and no
match finds the old one. A wrong ``confirm`` deletes nothing, and a second delete is not found.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from conftest import define_signal_types, make_ctx, session_for
from sqlalchemy import func, select

from mcp_people.db import store
from mcp_people.db.store import (
    ContactNotFoundError,
    DeleteRefusedError,
    OrganizationNotFoundError,
)
from mcp_people.db.tables import (
    contact,
    external_ref,
    interaction,
    metadata,
    opportunity,
    organization,
    people_audit,
    signal,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(backend, app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _count(ctx, table, *where: Any) -> int:
    async with session_for(ctx) as conn:
        stmt = select(func.count()).select_from(table)
        if where:
            stmt = stmt.where(*where)
        return (await conn.execute(stmt)).scalar_one()


async def _rows(ctx) -> dict[str, list[dict[str, Any]]]:
    """Every row of every table, its ``deleted_at`` left out: what a delete must not change."""
    out: dict[str, list[dict[str, Any]]] = {}
    async with session_for(ctx) as conn:
        for table in metadata.sorted_tables:
            rows = (await conn.execute(select(table))).mappings().all()
            out[table.name] = sorted(
                ({k: v for k, v in r.items() if k != "deleted_at"} for r in rows), key=repr
            )
    return out


async def _marked(ctx) -> dict[str, set[str]]:
    """The ids each table holds with ``deleted_at`` set."""
    out: dict[str, set[str]] = {}
    async with session_for(ctx) as conn:
        for table in metadata.sorted_tables:
            if "deleted_at" in table.c:
                rows = await conn.execute(select(table.c.id).where(table.c.deleted_at.isnot(None)))
                out[table.name] = {r[0] for r in rows}
    return out


async def _assert_only_marked(ctx, before: dict[str, list[dict[str, Any]]]) -> None:
    """No row was removed and no value changed; the one row added is the delete's audit row."""
    after = await _rows(ctx)
    assert {t: rows for t, rows in after.items() if t != "people_audit"} == {
        t: rows for t, rows in before.items() if t != "people_audit"
    }
    assert after["people_audit"][: len(before["people_audit"])] == before["people_audit"] or (
        len(after["people_audit"]) == len(before["people_audit"]) + 1
    )
    added = [r for r in after["people_audit"] if r not in before["people_audit"]]
    assert [r["action"] for r in added] == ["delete"]


# --------------------------------------------------------------------------- #
# delete_contact                                                               #
# --------------------------------------------------------------------------- #


async def _seed_person(ctx) -> dict[str, str]:
    """A person (with a merged duplicate) at Acme, beside a colleague and the company's
    own records, which deleting the person must leave."""
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring", "expansion")
        acme, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example"
        )
        other, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Globex", domain="globex.example"
        )
        jane, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Janeway Jones",
            email="janeway@acme.example",
            phone="555-0199",
            organization_id=acme.id,
            external_refs=["vendor://people/1"],
        )
        dup, _, _ = await store.find_or_create_contact(
            conn, ctx, name="J. Jones", email="jj@old.example", external_refs=["vendor://people/2"]
        )
        await store.log_interaction(conn, ctx, contact_id=dup.id, kind="call", summary="Old call")
        await store.merge_contacts(conn, ctx, target_id=jane.id, source_id=dup.id)
        bob, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Bob Stays", email="bob@acme.example", organization_id=acme.id
        )
        deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Acme renewal", organization_id=acme.id, primary_contact_id=jane.id
        )
        bob_deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Acme pilot", organization_id=acme.id, primary_contact_id=bob.id
        )
        await store.log_interaction(
            conn,
            ctx,
            contact_id=jane.id,
            kind="meeting",
            summary="Renewal meeting",
            opportunity_id=deal.id,
            idempotency_key="renewal",
        )
        await store.log_interaction(conn, ctx, contact_id=bob.id, kind="email", summary="Pilot")
        about_jane, _, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=acme.id,
            contact_id=jane.id,
            claim="Janeway Jones joined Acme as CFO.",
            evidence_quote="Acme names Janeway Jones CFO.",
            source_url="https://news.example/jones-cfo",
            signal_type="hiring",
            evidence_for=["workspace", other.id],
        )
        about_acme, _, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=acme.id,
            claim="Acme opened a plant in Reno.",
            evidence_quote="Acme opened its Reno plant.",
            source_url="https://news.example/reno",
            signal_type="expansion",
        )
    return {
        "acme": acme.id,
        "other": other.id,
        "jane": jane.id,
        "dup": dup.id,
        "bob": bob.id,
        "deal": deal.id,
        "bob_deal": bob_deal.id,
        "about_jane": about_jane.id,
        "about_acme": about_acme.id,
    }


async def test_delete_contact_marks_the_person_and_changes_nothing_else(ctx) -> None:
    ids = await _seed_person(ctx)
    before = await _rows(ctx)

    async with session_for(ctx) as conn:
        summary = await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])

    assert summary == {"deleted": {"contact": 2, "interaction": 2, "signal": 1, "external_ref": 2}}
    await _assert_only_marked(ctx, before)
    marked = await _marked(ctx)
    assert marked["contact"] == {ids["jane"], ids["dup"]}
    assert marked["signal"] == {ids["about_jane"]}
    assert marked["organization"] == marked["opportunity"] == set()
    assert len(marked["interaction"]) == 2 and len(marked["external_ref"]) == 2
    # The deal keeps its link in the database, and a read shows no primary contact.
    assert await _count(ctx, opportunity, opportunity.c.primary_contact_id == ids["jane"]) == 1
    async with session_for(ctx) as conn:
        deal, _ = await store.get_opportunity(conn, ctx, opportunity_id=ids["deal"])
        bob_deal, _ = await store.get_opportunity(conn, ctx, opportunity_id=ids["bob_deal"])
        page = await store.page_opportunities(conn, ctx, primary_contact_id=ids["jane"])
    assert (deal.primary_contact_id, deal.organization_id) == (None, ids["acme"])
    assert bob_deal.primary_contact_id == ids["bob"]
    assert page.rows == []


async def test_delete_contact_writes_one_delete_row_and_leaves_the_log(ctx) -> None:
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        before = (await conn.execute(select(people_audit))).mappings().all()
        await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])
        after = (await conn.execute(select(people_audit))).mappings().all()
    assert after[: len(before)] == before
    [row] = after[len(before) :]
    assert (row["entity"], row["action"], row["entity_id"]) == ("contact", "delete", ids["jane"])
    assert row["detail"] == {
        "deleted": {"contact": 2, "interaction": 2, "signal": 1, "external_ref": 2}
    }


async def test_no_read_finds_a_deleted_person(ctx) -> None:
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])

    async with session_for(ctx) as conn:
        assert (await store.page_contacts(conn, ctx, terms="Jones")).rows == []
        for include_archived in (False, True):
            page = await store.page_contacts(
                conn, ctx, organization_id=ids["acme"], include_archived=include_archived
            )
            assert {c.id for c in page.rows} <= {ids["bob"]}
        counted = await store.count_by(conn, ctx, entity="contact", field="name")
        assert counted["total"] == 1
        for gone in (ids["jane"], ids["dup"]):
            with pytest.raises(ContactNotFoundError):
                await store.list_interactions(conn, ctx, contact_id=gone)
        contacts, _ = await store.list_contacts(conn, ctx, organization_id=ids["acme"])
        interactions, _ = await store.list_interactions(conn, ctx, organization_id=ids["acme"])
        assert [c.id for c in contacts] == [ids["bob"]]
        assert {i.contact_id for i in interactions} == {ids["bob"]}
        deal_touches, _ = await store.list_interactions(conn, ctx, opportunity_id=ids["deal"])
        assert deal_touches == []
        found = await store.find_by_external_refs(
            conn, ctx, refs=["vendor://people/1", "vendor://people/2"]
        )
        assert found["found"] == []
        signals, _ = await store.list_signals(conn, ctx, status="any")
        assert [s.id for s in signals] == [ids["about_acme"]]


async def test_a_wrong_confirm_deletes_nothing(ctx) -> None:
    ids = await _seed_person(ctx)
    before = await _rows(ctx)
    for confirm in ("", "yes", ids["bob"], ids["jane"].upper(), f" {ids['jane']}"):
        with pytest.raises(ValueError, match="confirm"):
            async with session_for(ctx) as conn:
                await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=confirm)
    assert await _rows(ctx) == before
    assert await _marked(ctx) == {t: set() for t in await _marked(ctx)}


async def test_a_second_delete_is_not_found(ctx) -> None:
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])
    for gone in (ids["jane"], ids["dup"], "ct_absent"):
        with pytest.raises(ContactNotFoundError):
            async with session_for(ctx) as conn:
                await store.delete_contact(conn, ctx, contact_id=gone, confirm=gone)


async def test_deleting_a_merged_duplicate_deletes_only_it(ctx) -> None:
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        summary = await store.delete_contact(conn, ctx, contact_id=ids["dup"], confirm=ids["dup"])
    assert summary["deleted"]["contact"] == 1
    assert (await _marked(ctx))["contact"] == {ids["dup"]}


async def test_a_revived_duplicate_is_its_own_record(ctx) -> None:
    """A merged source that was un-archived is left; it is deleted by its own id."""
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        await store.archive_record(
            conn, ctx, entity="contact", record_id=ids["dup"], archived=False
        )
        summary = await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])
    assert summary["deleted"]["contact"] == 1
    assert (await _marked(ctx))["contact"] == {ids["jane"]}


async def test_a_duplicate_merged_into_a_duplicate_is_deleted_too(ctx) -> None:
    ids = await _seed_person(ctx)
    async with session_for(ctx) as conn:
        older, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Jan Jones", email="jan@older.example"
        )
        newer, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Janeway J.", email="jj@newer.example"
        )
        await store.merge_contacts(conn, ctx, target_id=newer.id, source_id=older.id)
        await store.merge_contacts(conn, ctx, target_id=ids["jane"], source_id=newer.id)
        summary = await store.delete_contact(conn, ctx, contact_id=ids["jane"], confirm=ids["jane"])
    assert summary["deleted"]["contact"] == 4
    assert (await _marked(ctx))["contact"] == {ids["jane"], ids["dup"], newer.id, older.id}


# --------------------------------------------------------------------------- #
# delete_organization                                                          #
# --------------------------------------------------------------------------- #


async def _seed_company(ctx) -> dict[str, str]:
    """A company with a merged duplicate, its signals, proof point, references and
    lookalike edges both ways, and a person who worked there and has moved on."""
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "expansion")
        initech, _, _ = await store.find_or_create_organization(
            conn,
            ctx,
            name="Initech",
            domain="initech.example",
            external_refs=["vendor://companies/initech"],
        )
        dup, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Initech Holdings", domain="initech-holdings.example"
        )
        await store.merge_organizations(conn, ctx, target_id=initech.id, source_id=dup.id)
        other, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Globex", domain="globex.example"
        )
        await store.link_similar_companies(
            conn, ctx, org_id=initech.id, companies=[{"domain": "umbrella.example", "name": "Umb"}]
        )
        await store.link_similar_companies(
            conn,
            ctx,
            org_id=other.id,
            companies=[{"domain": "initech.example", "name": "Initech"}],
        )
        dana, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana Moved", email="dana@globex.example", organization_id=initech.id
        )
        await store.log_interaction(conn, ctx, contact_id=dana.id, kind="call", summary="Intro")
        await store.update_contact(conn, ctx, contact_id=dana.id, organization_id=other.id)
        own, _, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=initech.id,
            claim="Initech opened a plant.",
            evidence_quote="Initech opened its plant.",
            source_url="https://news.example/initech-plant",
            signal_type="expansion",
        )
        theirs, _, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=other.id,
            claim="Globex opened an office.",
            evidence_quote="Globex opened an office.",
            source_url="https://news.example/globex-office",
            signal_type="expansion",
            evidence_for=["workspace", initech.id],
        )
        proof, _ = await store.record_proof_point(
            conn, ctx, claim="Halved onboarding.", organization_id=initech.id, idempotency_key="p1"
        )
    return {
        "initech": initech.id,
        "dup": dup.id,
        "other": other.id,
        "dana": dana.id,
        "own": own.id,
        "theirs": theirs.id,
        "proof": proof.id,
    }


async def test_an_organization_with_contacts_is_refused(ctx) -> None:
    ids = await _seed_company(ctx)
    async with session_for(ctx) as conn:
        await store.find_or_create_contact(
            conn,
            ctx,
            name="Still Here",
            email="still@initech.example",
            organization_id=ids["initech"],
        )
    before = await _rows(ctx)
    with pytest.raises(DeleteRefusedError) as refused:
        async with session_for(ctx) as conn:
            await store.delete_organization(
                conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
            )
    assert str(refused.value) == "contacts"
    assert "delete_contact" in refused.value.next_step
    assert await _rows(ctx) == before


async def test_an_archived_contact_still_blocks_and_a_deleted_one_does_not(ctx) -> None:
    ids = await _seed_company(ctx)
    async with session_for(ctx) as conn:
        a, _, _ = await store.find_or_create_contact(
            conn, ctx, name="A", email="a@initech.example", organization_id=ids["initech"]
        )
        b, _, _ = await store.find_or_create_contact(
            conn, ctx, name="B", email="b@globex.example", organization_id=ids["other"]
        )
        await store.merge_contacts(conn, ctx, target_id=b.id, source_id=a.id)
    with pytest.raises(DeleteRefusedError):
        async with session_for(ctx) as conn:
            await store.delete_organization(
                conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
            )
    async with session_for(ctx) as conn:
        await store.delete_contact(conn, ctx, contact_id=a.id, confirm=a.id)
        summary = await store.delete_organization(
            conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
        )
    assert summary["deleted"]["organization"] == 2


async def test_an_organization_with_opportunities_is_refused_unless_asked(ctx) -> None:
    ids = await _seed_company(ctx)
    async with session_for(ctx) as conn:
        deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Widget deal", organization_id=ids["initech"], idempotency_key="d1"
        )
        touch = await store.log_interaction(
            conn, ctx, contact_id=ids["dana"], kind="email", summary="Terms", opportunity_id=deal.id
        )
    before = await _rows(ctx)
    with pytest.raises(DeleteRefusedError) as refused:
        async with session_for(ctx) as conn:
            await store.delete_organization(
                conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
            )
    assert str(refused.value) == "opportunities"
    assert await _rows(ctx) == before

    async with session_for(ctx) as conn:
        summary = await store.delete_organization(
            conn,
            ctx,
            organization_id=ids["initech"],
            confirm=ids["initech"],
            delete_opportunities=True,
        )
        touches, _ = await store.list_interactions(conn, ctx, contact_id=ids["dana"])
    assert summary["deleted"]["opportunity"] == 1
    assert (await _marked(ctx))["opportunity"] == {deal.id}
    # The touch keeps its link in the database and reads without the deal or the company.
    assert await _count(ctx, interaction, interaction.c.opportunity_id == deal.id) == 1
    [read] = [t for t in touches if t.id == touch.id]
    assert read.organization_id == ids["other"]  # logged after Dana moved
    async with session_for(ctx) as conn:
        with pytest.raises(store.OpportunityNotFoundError):
            await store.list_interactions(conn, ctx, opportunity_id=deal.id)


async def test_delete_organization_marks_the_company_and_changes_nothing_else(ctx) -> None:
    ids = await _seed_company(ctx)
    before = await _rows(ctx)

    async with session_for(ctx) as conn:
        summary = await store.delete_organization(
            conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
        )

    assert summary == {
        "deleted": {
            "organization": 2,
            "signal": 1,
            "proof_point": 1,
            "external_ref": 1,
            "opportunity": 0,
        }
    }
    await _assert_only_marked(ctx, before)
    marked = await _marked(ctx)
    assert marked["organization"] == {ids["initech"], ids["dup"]}
    assert marked["signal"] == {ids["own"]}
    assert marked["contact"] == marked["interaction"] == set()
    async with session_for(ctx) as conn:
        [call], _ = await store.list_interactions(conn, ctx, contact_id=ids["dana"])
        theirs = await store.get_signal(conn, ctx, signal_id=ids["theirs"])
        for_initech, _ = await store.list_signals(conn, ctx, evidence_for=ids["initech"])
        unstated, _ = await store.list_signals(conn, ctx, evidence_for="unstated")
        similar = await store.list_similar_companies(conn, ctx, org_id=ids["other"])
        for gone in (ids["initech"], ids["dup"]):
            with pytest.raises(OrganizationNotFoundError):
                await store.get_organization(conn, ctx, org_id=gone)
        for status in ("active", "discovered"):
            page = await store.page_organizations(
                conn, ctx, terms="Initech", status=status, include_archived=True
            )
            assert page.rows == []
    assert call.organization_id is None
    assert theirs.evidence_for == ["workspace"]
    assert for_initech == [] and unstated == []
    assert similar == []


async def test_a_logging_retry_reads_a_deleted_company_as_absent(ctx) -> None:
    async with session_for(ctx) as conn:
        hooli, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Hooli", domain="hooli.example"
        )
        piper, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Piper", domain="piper.example"
        )
        rick, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Rick Moves", email="rick@piper.example", organization_id=hooli.id
        )
        first = await store.log_interaction(
            conn, ctx, contact_id=rick.id, kind="call", summary="Intro", idempotency_key="intro"
        )
        await store.update_contact(conn, ctx, contact_id=rick.id, organization_id=piper.id)
        await store.delete_organization(conn, ctx, organization_id=hooli.id, confirm=hooli.id)
        retry = await store.log_interaction(
            conn, ctx, contact_id=rick.id, kind="call", summary="Intro", idempotency_key="intro"
        )
        [read], _ = await store.list_interactions(conn, ctx, contact_id=rick.id)
    assert first.organization_id == hooli.id
    assert retry.id == read.id == first.id
    assert retry.organization_id is None and read.organization_id is None


async def test_delete_organization_confirm_and_repeat(ctx) -> None:
    ids = await _seed_company(ctx)
    before = await _rows(ctx)
    with pytest.raises(ValueError, match="confirm"):
        async with session_for(ctx) as conn:
            await store.delete_organization(
                conn, ctx, organization_id=ids["initech"], confirm=ids["other"]
            )
    assert await _rows(ctx) == before
    async with session_for(ctx) as conn:
        await store.delete_organization(
            conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
        )
    with pytest.raises(OrganizationNotFoundError):
        async with session_for(ctx) as conn:
            await store.delete_organization(
                conn, ctx, organization_id=ids["initech"], confirm=ids["initech"]
            )


# --------------------------------------------------------------------------- #
# A deleted record holds no key                                                #
# --------------------------------------------------------------------------- #


async def test_a_deleted_record_matches_nothing_and_blocks_nothing(ctx) -> None:
    """Every key a deleted record held makes a new record, and no match returns the old one."""
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
        org, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Solo", domain="solo.example", external_refs=["vendor://c/solo"]
        )
        bare, _, _ = await store.find_or_create_organization(conn, ctx, name="Bare")
        person, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Pat", email="pat@x.example", external_refs=["vendor://p/pat"]
        )
        named, _, _ = await store.find_or_create_contact(conn, ctx, name="Lee")
        sig, _, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=org.id,
            claim="Solo is hiring.",
            evidence_quote="Solo is hiring.",
            source_url="https://news.example/solo",
            signal_type="hiring",
        )
        deal, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Solo deal", organization_id=org.id, idempotency_key="deal-1"
        )
        proof, _ = await store.record_proof_point(
            conn, ctx, claim="Did it.", organization_id=org.id, idempotency_key="proof-1"
        )
        for record in (person, named):
            await store.delete_contact(conn, ctx, contact_id=record.id, confirm=record.id)
        await store.delete_organization(
            conn, ctx, organization_id=org.id, confirm=org.id, delete_opportunities=True
        )
        await store.delete_organization(conn, ctx, organization_id=bare.id, confirm=bare.id)

    async with session_for(ctx) as conn:
        again, action, _ = await store.find_or_create_contact(
            conn, ctx, name="Pat", email="pat@x.example", external_refs=["vendor://p/pat"]
        )
        assert action == "created" and again.id != person.id
        lee, action, _ = await store.find_or_create_contact(conn, ctx, name="Lee")
        assert action == "created" and lee.id != named.id
        solo, action, _ = await store.find_or_create_organization(
            conn, ctx, name="Solo", domain="solo.example", external_refs=["vendor://c/solo"]
        )
        assert action == "created" and solo.id != org.id
        bare2, action, _ = await store.find_or_create_organization(conn, ctx, name="Bare")
        assert action == "created" and bare2.id != bare.id
        sig2, action, _ = await store.record_signal(
            conn,
            ctx,
            organization_id=solo.id,
            claim="Solo is hiring.",
            evidence_quote="Solo is hiring.",
            source_url="https://news.example/solo",
            signal_type="hiring",
        )
        assert action == "created" and sig2.id != sig.id
        deal2, action = await store.find_or_create_opportunity(
            conn, ctx, title="Solo deal", organization_id=solo.id, idempotency_key="deal-1"
        )
        assert action == "created" and deal2.id != deal.id
        proof2, action = await store.record_proof_point(
            conn, ctx, claim="Did it.", organization_id=solo.id, idempotency_key="proof-1"
        )
        assert action == "created" and proof2.id != proof.id
        imported = await store.import_contacts(
            conn, ctx, rows=[{"name": "Pat", "email": "pat@x.example"}]
        )
        assert imported["matched"] == 1 and imported["created"] == 0  # the new Pat, not the old
    assert await _count(ctx, contact, contact.c.email_key == "pat@x.example") == 2
    assert await _count(ctx, external_ref, external_ref.c.uri == "vendor://p/pat") == 2
    assert await _count(ctx, organization, organization.c.domain_key == "solo.example") == 2
    assert await _count(ctx, signal, signal.c.source_url == "https://news.example/solo") == 2


async def test_a_reference_match_takes_a_key_a_deleted_record_held(ctx) -> None:
    """A live record named by a reference fills an email or domain only a deleted record held."""
    async with session_for(ctx) as conn:
        gone_org, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Gone", domain="gone.example"
        )
        gone_person, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Gone Person", email="gone@x.example"
        )
        await store.delete_contact(conn, ctx, contact_id=gone_person.id, confirm=gone_person.id)
        await store.delete_organization(conn, ctx, organization_id=gone_org.id, confirm=gone_org.id)
        org, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Live Co", external_refs=["vendor://c/live"]
        )
        person, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Live Person", external_refs=["vendor://p/live"]
        )

    async with session_for(ctx) as conn:
        filled_org, action, _ = await store.find_or_create_organization(
            conn, ctx, name="Live Co", domain="gone.example", external_refs=["vendor://c/live"]
        )
        assert action == "matched" and filled_org.id == org.id
        assert filled_org.domain == "gone.example"
        filled, action, _ = await store.find_or_create_contact(
            conn, ctx, name="Live Person", email="gone@x.example", external_refs=["vendor://p/live"]
        )
        assert action == "matched" and filled.id == person.id
        assert filled.email == "gone@x.example"
