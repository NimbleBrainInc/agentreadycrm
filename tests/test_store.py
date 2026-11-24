"""Store behavior suite.

These tests exercise the data-access layer on a database at the local schema's
head (``conftest.py``). They assert **behavior**, not implementation: idempotent convergence (call
twice, assert one row), the works_at org edge reuse, the immutable-id linkage on
interactions (rename the org → past interaction unchanged), null-safe partial
updates, opaque-cursor pagination that walks every row, and
domain-dedupe normalization.

Each test opens a fresh session per logical request (mirroring how the
server opens one transaction per tool call), so the assertions hold across
commits — not within one accidentally-shared transaction.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from conftest import make_ctx, session_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mcp_people.db import store
from mcp_people.db.store import ConflictError, ContactNotFoundError, OrganizationNotFoundError
from mcp_people.db.tables import (
    contact,
    interaction,
    organization,
    organization_edge,
    people_audit,
)
from mcp_people.keys import match_key

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


async def _count_contacts_by_email(ctx, email: str) -> int:
    """Count rows the store would dedupe on — same (account, workspace, lower(email)) key."""
    async with session_for(ctx) as conn:
        contacts, _cursor = await store.list_contacts(conn, ctx)
    return sum(1 for c in contacts if (c.email or "").lower() == email.lower())


# --------------------------------------------------------------------------- #
# 1. Idempotent contact upsert: twice on the same email → ONE row             #
# --------------------------------------------------------------------------- #


async def test_find_or_create_contact_is_idempotent_on_email(app_engine):
    """Two find_or_create with the same email converge to one row.

    First call ``created``, second ``matched``; the second fills an empty field
    (phone) without overwriting the existing name, and does NOT insert a second
    row. This is the load-bearing idempotent-upsert contract.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    email = "Dana@Example.COM"

    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana", email=email, title="Engineer"
        )
    assert action1 == "created"

    # Second call: same email (different case), supplies a NEW empty field +
    # a conflicting name that must NOT overwrite.
    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn, ctx, name="Dana SHOULD-NOT-OVERWRITE", email=email.lower(), phone="555-0100"
        )
    assert action2 == "matched"
    assert c2.id == c1.id  # same row

    # Exactly one row carries this email (no duplicate insert).
    assert await _count_contacts_by_email(ctx, email) == 1

    # Empty field filled; existing name preserved (merge fills holes only).
    async with session_for(ctx) as conn:
        fresh, counts = await store.get_contact(conn, ctx, contact_id=c1.id)
    assert fresh.name == "Dana"  # NOT overwritten
    assert fresh.phone == "555-0100"  # empty field filled
    assert fresh.title == "Engineer"  # untouched
    assert counts == {"interactions": 0}


async def test_find_or_create_contact_by_email_alone(app_engine):
    """An email with no name creates a contact named from the address.

    A later call with only the same email matches that row, and a real name given
    on a match does not overwrite the derived one (a match fills holes only);
    ``update_contact`` is the way to correct it.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(
            conn, ctx, email="jane.doe+crm@example.com"
        )
    assert action1 == "created"
    assert c1.name == "Jane Doe"

    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn, ctx, name="Jane Q. Doe", email="JANE.DOE+crm@example.com"
        )
    assert action2 == "matched"
    assert c2.id == c1.id
    assert c2.name == "Jane Doe"


async def test_find_or_create_contact_needs_a_name_or_an_email(app_engine):
    """With neither there is nothing to dedupe on or display: a clean ValueError."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="name or an email"):
            await store.find_or_create_contact(conn, ctx, name="  ", phone="555-0100")


@pytest.mark.parametrize(
    ("email", "name"),
    [
        ("jane.doe@example.com", "Jane Doe"),
        ("jane_doe-smith+tag@example.com", "Jane Doe Smith"),
        ("chris@example.com", "Chris"),
        ("12345@example.com", "12345"),
        ("+tag@example.com", "+tag@example.com"),
    ],
)
async def test_name_from_email(email, name):
    assert store.name_from_email(email) == name


async def test_new_contact_defaults_to_sourced(app_engine):
    """A contact created without an explicit status lands at ``sourced``.

    ``sourced`` is the first lifecycle stage (identified, not yet worked) — the
    DB ``server_default``. People never advances it; the agent working the lead does.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        c, action, _ = await store.find_or_create_contact(
            conn, ctx, name="Unworked Lead", email="lead@ex.example"
        )
    assert action == "created"
    assert c.status == "sourced"


# --------------------------------------------------------------------------- #
# 2. organization_name resolve-or-create + reuse the same org id              #
# --------------------------------------------------------------------------- #


async def test_organization_name_creates_links_and_reuses_org(app_engine):
    """find_or_create_contact with organization_name (no id) creates+links the
    org; a second contact with the same org name reuses the same org id."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        c1, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Eve", email="eve@acme.example", organization_name="Acme Inc"
        )
    assert c1.organization_id is not None
    org_id = c1.organization_id

    # A second, different contact naming the same org reuses the org id
    # (resolve, not re-create).
    async with session_for(ctx) as conn:
        c2, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Frank", email="frank@acme.example", organization_name="Acme Inc"
        )
    assert c2.organization_id == org_id

    # Only one organization exists in scope.
    async with session_for(ctx) as conn:
        orgs, _cursor = await store.list_organizations(conn, ctx)
    assert len(orgs) == 1
    assert orgs[0].id == org_id


async def test_direct_org_id_wires_onto_email_matched_empty_org_contact(app_engine):
    """A directly-given organization_id is wired onto a contact that matched by
    email and had an EMPTY org (the FIX-1 regression guard).

    Before the fix the org-wiring branch only fired for the by-name path
    (organization_id is None), so a direct id was silently dropped on an
    email-matched empty-org contact.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # An org to reference by id.
    async with session_for(ctx) as conn:
        org, _, _ = await store.find_or_create_organization(conn, ctx, name="Globex")
    org_id = org.id

    # First create the contact with NO org (org stays NULL).
    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(
            conn, ctx, name="Gail", email="gail@globex.example"
        )
    assert action1 == "created"
    assert c1.organization_id is None

    # Re-run with the SAME email and a DIRECT organization_id → match + wire edge.
    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn, ctx, name="Gail", email="gail@globex.example", organization_id=org_id
        )
    assert action2 == "matched"
    assert c2.id == c1.id
    assert c2.organization_id == org_id  # the edge WAS wired on a match


async def test_direct_org_id_does_not_overwrite_existing_org_on_match(app_engine):
    """A direct organization_id never overwrites a contact's EXISTING (different)
    org on an email match — fill-empty-only, never clobber."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        org_a, _, _ = await store.find_or_create_organization(conn, ctx, name="Org A")
        org_b, _, _ = await store.find_or_create_organization(conn, ctx, name="Org B")

    # Create the contact already linked to org A.
    async with session_for(ctx) as conn:
        c1, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Hank",
            email="hank@orga.example",
            organization_id=org_a.id,
        )
    assert c1.organization_id == org_a.id

    # Match by email with a DIFFERENT direct org id → existing org preserved.
    async with session_for(ctx) as conn:
        c2, action, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Hank",
            email="hank@orga.example",
            organization_id=org_b.id,
        )
    assert action == "matched"
    assert c2.id == c1.id
    assert c2.organization_id == org_a.id  # NOT overwritten with org B


async def test_org_name_fills_empty_org_on_email_match(app_engine):
    """The by-NAME fill-on-match path still works: an email-matched empty-org
    contact gets the org resolved-and-wired from organization_name (no regress)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # Create with no org.
    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(
            conn, ctx, name="Ida", email="ida@initech.example"
        )
    assert action1 == "created"
    assert c1.organization_id is None

    # Re-run with the same email + an org NAME → match, resolve, and wire.
    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Ida",
            email="ida@initech.example",
            organization_name="Initech",
        )
    assert action2 == "matched"
    assert c2.id == c1.id
    assert c2.organization_id is not None

    # The org was resolve-or-created exactly once, in scope.
    async with session_for(ctx) as conn:
        orgs, _cursor = await store.list_organizations(conn, ctx)
    assert [o.id for o in orgs] == [c2.organization_id]


# --------------------------------------------------------------------------- #
# 3. Interaction org link is the immutable id, not the name                   #
# --------------------------------------------------------------------------- #


async def test_interaction_org_link_is_immutable_id_not_name(app_engine):
    """log_interaction copies the contact's organization_id; renaming the org
    afterward leaves the PAST interaction's organization_id unchanged.

    Proves the linkage is a denormalized immutable id, never a
    denormalized name — the org-level timeline still resolves the same history
    after a rename.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Grace",
            email="grace@oldname.example",
            organization_name="Old Name LLC",
        )
        org_id = contact.organization_id
        ix = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="meeting", summary="Kickoff call"
        )
    assert org_id is not None
    assert ix.organization_id == org_id  # copied from the contact at log time

    # Rename the org. The id is stable; only the display name changes.
    async with session_for(ctx) as conn:
        renamed = await store.update_organization(
            conn, ctx, org_id=org_id, name="Brand New Name LLC"
        )
    assert renamed.id == org_id
    assert renamed.name == "Brand New Name LLC"

    # The PAST interaction still points at the SAME org id (not the name).
    async with session_for(ctx) as conn:
        interactions, _ = await store.list_interactions(conn, ctx, contact_id=contact.id)
    assert len(interactions) == 1
    assert interactions[0].id == ix.id
    assert interactions[0].organization_id == org_id

    # And the organization still resolves its history by id after the rename.
    async with session_for(ctx) as conn:
        org, _ = await store.get_organization(conn, ctx, org_id=org_id)
        contacts, _ = await store.list_contacts(conn, ctx, organization_id=org_id)
        org_ix, _ = await store.list_interactions(conn, ctx, organization_id=org_id)
    assert org.name == "Brand New Name LLC"
    assert {c.id for c in contacts} == {contact.id}
    assert {i.id for i in org_ix} == {ix.id}


async def test_interaction_direction_round_trips_and_defaults_none(app_engine):
    """``direction`` is stored and returned as given; omitted → None.

    An open string (inbound/outbound), nullable — direction-agnostic kinds
    (note/research) carry none.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Ivy", email="ivy@ex.example"
        )
        outbound = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="email", summary="Intro", direction="outbound"
        )
        note = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="note", summary="Internal note"
        )
    assert outbound.direction == "outbound"
    assert note.direction is None

    # Survives the reverse-chron timeline read.
    async with session_for(ctx) as conn:
        interactions, _ = await store.list_interactions(conn, ctx, contact_id=contact.id)
    by_id = {i.id: i for i in interactions}
    assert by_id[outbound.id].direction == "outbound"
    assert by_id[note.id].direction is None


# --------------------------------------------------------------------------- #
# 4. update_contact is null-safe: omitted unchanged, provided updated         #
# --------------------------------------------------------------------------- #


async def test_update_contact_is_null_safe(app_engine):
    """A partial update touches only the provided (non-``None``) fields; omitted
    fields are unchanged and ``updated_at`` is bumped."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        c, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Heidi",
            email="heidi@ex.example",
            phone="555-1111",
            title="Analyst",
            lead_source="referral",
        )
    before_updated_at = c.updated_at

    # Update only title; everything else omitted.
    async with session_for(ctx) as conn:
        updated = await store.update_contact(conn, ctx, contact_id=c.id, title="Senior Analyst")
    assert updated.title == "Senior Analyst"  # provided → updated
    assert updated.name == "Heidi"  # omitted → unchanged
    assert updated.phone == "555-1111"  # omitted → unchanged
    assert updated.email == "heidi@ex.example"  # omitted → unchanged
    assert updated.lead_source == "referral"  # omitted → unchanged
    assert updated.updated_at >= before_updated_at  # bumped (or equal at worst)


# --------------------------------------------------------------------------- #
# 5. A stored dossier is kept and never read                                  #
# --------------------------------------------------------------------------- #


async def test_a_stored_dossier_is_kept_and_never_read(app_engine):
    """A database can hold dossiers no tool reads or writes: the record reads without
    one, it sets no stage floor, and an update leaves the stored text as it was."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        c, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Ivan", email="ivan@ex.example"
        )
        await conn.execute(
            contact.update().where(contact.c.id == c.id).values(dossier="# Ivan\nResearch.")
        )
    async with session_for(ctx) as conn:
        await store.update_contact(conn, ctx, contact_id=c.id, title="Researcher")
        got, _counts = await store.get_contact(conn, ctx, contact_id=c.id)
        stored = (
            await conn.execute(select(contact.c.dossier).where(contact.c.id == c.id))
        ).scalar_one()
    assert "dossier" not in got.model_dump()
    assert got.effective_status == "sourced"
    assert stored == "# Ivan\nResearch."


# --------------------------------------------------------------------------- #
# 6. Opaque-cursor pagination walks every row with no dupes / gaps            #
# --------------------------------------------------------------------------- #


async def test_list_contacts_cursor_walks_all_rows(app_engine):
    """Paging with the opaque cursor at a small page size visits every row
    exactly once — no duplicates, no gaps — and the cursor is opaque (not a
    plain offset/id)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    created_ids = set()
    for i in range(7):
        async with session_for(ctx) as conn:
            c, _, _ = await store.find_or_create_contact(
                conn, ctx, name=f"Person {i}", email=f"p{i}@ex.example"
            )
        created_ids.add(c.id)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        async with session_for(ctx) as conn:
            page, cursor = await store.list_contacts(conn, ctx, limit=3, cursor=cursor)
        seen.extend(c.id for c in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "cursor never terminated"

    # Every row exactly once: no gaps, no dupes.
    assert len(seen) == len(created_ids)
    assert set(seen) == created_ids
    assert len(set(seen)) == len(seen)
    # More than one page was actually walked (3 + 3 + 1).
    assert pages == 3


# --------------------------------------------------------------------------- #
# 7. Domain dedupe: URL forms normalize to one org                            #
# --------------------------------------------------------------------------- #


async def test_domain_dedupe_normalizes_to_one_org(app_engine):
    """find_or_create_organization with ``https://www.Acme.com/x`` then
    ``acme.com`` collapse to one org under the normalized-domain dedupe key."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        o1, action1, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="https://www.Acme.com/x"
        )
    assert action1 == "created"
    assert o1.domain == "acme.com"  # normalized: scheme/www/path stripped, lowercased

    async with session_for(ctx) as conn:
        o2, action2, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme Corporation", domain="acme.com"
        )
    assert action2 == "matched"
    assert o2.id == o1.id  # same org — deduped by normalized domain

    # Exactly one org in scope.
    async with session_for(ctx) as conn:
        orgs, _cursor = await store.list_organizations(conn, ctx)
    assert len(orgs) == 1
    assert orgs[0].id == o1.id
    # The matched call did not overwrite the existing name (fill-empty only).
    assert orgs[0].name == "Acme"


# --------------------------------------------------------------------------- #
# 8. log_interaction against an unknown contact → NotFound (no crash)         #
# --------------------------------------------------------------------------- #


async def test_log_interaction_unknown_contact_raises_not_found(app_engine):
    """Logging against a contact id that is not in scope raises NotFound, never a
    silent write or an integrity crash."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(ContactNotFoundError):
        async with session_for(ctx) as conn:
            await store.log_interaction(
                conn, ctx, contact_id="ct_does_not_exist", kind="note", summary="orphan"
            )


# --------------------------------------------------------------------------- #
# 9. update collides with another row's dedupe key → ConflictError (not 500)   #
# --------------------------------------------------------------------------- #


async def test_update_contact_email_collision_raises_conflict(app_engine):
    """Updating a contact's email to one already held by ANOTHER contact in the
    same scope raises :class:`ConflictError`, NOT an opaque IntegrityError/500.

    The collision is on ``uq_contact_email``. The raised message must name the
    FIELD (``email``) for the caller, but must NEVER leak the constraint/index
    name — that is a schema-shape disclosure.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # Two distinct contacts with distinct emails (no dedupe collision on create).
    async with session_for(ctx) as conn:
        a, _, _ = await store.find_or_create_contact(conn, ctx, name="Ann", email="ann@ex.example")
        b, _, _ = await store.find_or_create_contact(conn, ctx, name="Bob", email="bob@ex.example")
    assert a.id != b.id

    # Update Bob's email to Ann's → the partial unique index rejects it.
    with pytest.raises(ConflictError) as excinfo:
        async with session_for(ctx) as conn:
            await store.update_contact(conn, ctx, contact_id=b.id, email="ann@ex.example")

    msg = str(excinfo.value)
    assert "email" in msg  # names the field
    assert "uq_contact_email" not in msg  # never leaks the index/constraint name


async def test_update_organization_domain_collision_raises_conflict(app_engine):
    """Updating an org's domain to one already held by ANOTHER org in the same
    scope raises :class:`ConflictError`, NOT an opaque IntegrityError/500.

    The collision is on ``uq_org_domain``; the message names ``domain`` but never
    the index name.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        a, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example"
        )
        b, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Beta", domain="beta.example"
        )
    assert a.id != b.id

    with pytest.raises(ConflictError) as excinfo:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=b.id, domain="acme.example")

    msg = str(excinfo.value)
    assert "domain" in msg  # names the field
    assert "uq_org_domain" not in msg  # never leaks the index/constraint name


# --------------------------------------------------------------------------- #
# 10. log_interaction idempotency key: same key dedupes, no key appends        #
# --------------------------------------------------------------------------- #


async def test_log_interaction_idempotency_key_dedupes(app_engine):
    """Two log_interaction calls with the SAME idempotency_key converge to ONE
    interaction; the second returns the existing row's id, never a duplicate."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Karl", email="karl@ex.example"
        )

    key = "send-batch-42"
    async with session_for(ctx) as conn:
        ix1 = await store.log_interaction(
            conn,
            ctx,
            contact_id=contact.id,
            kind="email",
            summary="First send",
            idempotency_key=key,
        )
    # A retry with the SAME key (even with different content) returns the SAME row.
    async with session_for(ctx) as conn:
        ix2 = await store.log_interaction(
            conn,
            ctx,
            contact_id=contact.id,
            kind="email",
            summary="Retry — must not duplicate",
            idempotency_key=key,
        )
    assert ix2.id == ix1.id  # converged to one row
    assert ix2.summary == "First send"  # the original row, not the retry's content

    # Exactly one interaction exists on the timeline.
    async with session_for(ctx) as conn:
        interactions, _ = await store.list_interactions(conn, ctx, contact_id=contact.id)
    assert len(interactions) == 1
    assert interactions[0].id == ix1.id


async def test_log_interaction_no_key_appends_each_call(app_engine):
    """Without an idempotency_key, each call appends a NEW interaction — the
    original append behavior is preserved (no accidental dedupe)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Lena", email="lena@ex.example"
        )

    async with session_for(ctx) as conn:
        ix1 = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="note", summary="One"
        )
    async with session_for(ctx) as conn:
        ix2 = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="note", summary="Two"
        )
    assert ix1.id != ix2.id  # two distinct rows

    async with session_for(ctx) as conn:
        interactions, _ = await store.list_interactions(conn, ctx, contact_id=contact.id)
    assert len(interactions) == 2
    assert {i.id for i in interactions} == {ix1.id, ix2.id}


async def test_log_interaction_different_keys_append(app_engine):
    """Two log_interaction calls with DIFFERENT idempotency keys create two rows —
    dedupe is per-key, not a blanket collapse."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Mona", email="mona@ex.example"
        )

    async with session_for(ctx) as conn:
        ix1 = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="email", summary="A", idempotency_key="k-A"
        )
    async with session_for(ctx) as conn:
        ix2 = await store.log_interaction(
            conn, ctx, contact_id=contact.id, kind="email", summary="B", idempotency_key="k-B"
        )
    assert ix1.id != ix2.id

    async with session_for(ctx) as conn:
        interactions, _ = await store.list_interactions(conn, ctx, contact_id=contact.id)
    assert len(interactions) == 2


async def test_log_interaction_same_key_different_contacts_are_distinct(app_engine):
    """The SAME idempotency_key reused across DIFFERENT contacts yields TWO
    distinct rows — the idempotency key is scoped PER CONTACT.

    An index keyed only on (account, workspace, key) would let a key reused for a
    second contact silently return the FIRST contact's interaction — cross-contact
    data leakage masquerading as a dedupe.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        a, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Otto", email="otto@ex.example"
        )
        b, _, _ = await store.find_or_create_contact(conn, ctx, name="Pia", email="pia@ex.example")

    key = "shared-batch-key"
    async with session_for(ctx) as conn:
        ix_a = await store.log_interaction(
            conn, ctx, contact_id=a.id, kind="email", summary="To Otto", idempotency_key=key
        )
    async with session_for(ctx) as conn:
        ix_b = await store.log_interaction(
            conn, ctx, contact_id=b.id, kind="email", summary="To Pia", idempotency_key=key
        )

    # Two distinct rows, each on its own contact (no cross-contact collapse).
    assert ix_a.id != ix_b.id
    assert ix_a.contact_id == a.id
    assert ix_b.contact_id == b.id
    assert ix_b.summary == "To Pia"  # NOT the first contact's interaction

    async with session_for(ctx) as conn:
        ix_a_list, _ = await store.list_interactions(conn, ctx, contact_id=a.id)
        ix_b_list, _ = await store.list_interactions(conn, ctx, contact_id=b.id)
    assert [i.id for i in ix_a_list] == [ix_a.id]
    assert [i.id for i in ix_b_list] == [ix_b.id]


# --------------------------------------------------------------------------- #
# 11. Input size caps: an over-length value raises ValueError                 #
# --------------------------------------------------------------------------- #


async def test_oversized_name_raises_value_error(app_engine):
    """An over-length name on create raises ValueError before any write."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    too_long = "n" * (store._CAP_NAME + 1)
    with pytest.raises(ValueError, match="name exceeds the maximum length"):
        async with session_for(ctx) as conn:
            await store.find_or_create_contact(conn, ctx, name=too_long)


# --------------------------------------------------------------------------- #
# Upsert reselect pins the ACTIVE workspace (the cross-workspace-grant trap)   #
# --------------------------------------------------------------------------- #


async def test_contact_upsert_reselect_pins_active_workspace(app_engine):
    """A lost-race reselect must return the ACTIVE workspace's row, never a
    different read-granted workspace's row that shares the same email.

    A cross-workspace read grant (``allowed_workspaces`` ⊋ ``{active}``) would let a
    reselect keyed only on (account, lower(email)) match the WRONG workspace's
    row. The reselect predicate pins ``workspace_id == active`` so the dedupe
    always converges on the active workspace. We seed the SAME email in both
    ws_1 and ws_2, then find_or_create under a ws_1-active/ws_2-granted ctx and
    assert we get the ws_1 row.
    """
    account = _tid()
    email = "acme@x.example"

    # ws_2 row first (active in ws_2 — the store stamps the active workspace).
    ctx_ws2 = make_ctx(account, workspace_id="ws_2")
    async with session_for(ctx_ws2) as conn:
        c_ws2, _, _ = await store.find_or_create_contact(
            conn, ctx_ws2, name="Acme WS2", email=email
        )

    # ws_1 row (active in ws_1).
    ctx_ws1 = make_ctx(account, workspace_id="ws_1")
    async with session_for(ctx_ws1) as conn:
        c_ws1, _, _ = await store.find_or_create_contact(
            conn, ctx_ws1, name="Acme WS1", email=email
        )

    assert c_ws1.id != c_ws2.id

    # Active in ws_1, but with a READ grant for ws_2 as well. The
    # upsert hits the existing ws_1 row; the reselect must return THAT row.
    ctx_granted = make_ctx(account, workspace_id="ws_1", allowed_workspaces={"ws_1", "ws_2"})
    async with session_for(ctx_granted) as conn:
        got, action, _ = await store.find_or_create_contact(
            conn, ctx_granted, name="Acme", email=email
        )

    assert action == "matched"
    assert got.id == c_ws1.id  # never the ws_2 row, even though it is read-granted


async def test_org_upsert_reselect_pins_active_workspace(app_engine):
    """Org analogue of the contact reselect-pins-active-workspace invariant."""
    account = _tid()
    domain = "acme.example"

    ctx_ws2 = make_ctx(account, workspace_id="ws_2")
    async with session_for(ctx_ws2) as conn:
        o_ws2, _, _ = await store.find_or_create_organization(
            conn, ctx_ws2, name="Acme WS2", domain=domain
        )

    ctx_ws1 = make_ctx(account, workspace_id="ws_1")
    async with session_for(ctx_ws1) as conn:
        o_ws1, _, _ = await store.find_or_create_organization(
            conn, ctx_ws1, name="Acme WS1", domain=domain
        )

    assert o_ws1.id != o_ws2.id

    ctx_granted = make_ctx(account, workspace_id="ws_1", allowed_workspaces={"ws_1", "ws_2"})
    async with session_for(ctx_granted) as conn:
        got, action, _ = await store.find_or_create_organization(
            conn, ctx_granted, name="Acme", domain=domain
        )

    assert action == "matched"
    assert got.id == o_ws1.id


# --------------------------------------------------------------------------- #
# idempotency_key is length-capped (S1)                                       #
# --------------------------------------------------------------------------- #


async def test_oversized_idempotency_key_raises_value_error(app_engine):
    """An over-length idempotency_key raises ValueError before any write."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Olive", email="olive@ex.example"
        )

    too_long = "k" * (store._CAP_SHORT + 1)
    with pytest.raises(ValueError, match="idempotency_key exceeds the maximum length"):
        async with session_for(ctx) as conn:
            await store.log_interaction(
                conn,
                ctx,
                contact_id=contact.id,
                kind="note",
                summary="hi",
                idempotency_key=too_long,
            )


# --------------------------------------------------------------------------- #
# tz-naive occurred_at/follow_up_at is coerced to UTC (S4)                     #
# --------------------------------------------------------------------------- #


async def test_naive_occurred_at_is_stored_as_utc(app_engine):
    """A tz-naive occurred_at is interpreted as UTC and returned tz-aware, not
    reinterpreted in the Postgres session TZ."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        contact, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Pat", email="pat@ex.example"
        )

    naive = datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001 - deliberately tz-naive input
    assert naive.tzinfo is None
    async with session_for(ctx) as conn:
        ix = await store.log_interaction(
            conn,
            ctx,
            contact_id=contact.id,
            kind="meeting",
            summary="Naive ts",
            occurred_at=naive,
            follow_up_at=naive,
        )

    assert ix.occurred_at is not None and ix.occurred_at.tzinfo is not None
    assert ix.occurred_at == naive.replace(tzinfo=UTC)
    assert ix.follow_up_at is not None and ix.follow_up_at.tzinfo is not None
    assert ix.follow_up_at == naive.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 12. Path 2 (no-email) dedupe: adopt a name-only stub when an org appears     #
# --------------------------------------------------------------------------- #


async def _count_contacts_by_name(ctx, name: str) -> int:
    async with session_for(ctx) as conn:
        contacts, _cursor = await store.list_contacts(conn, ctx, include_archived=True)
        active, _c2 = await store.list_contacts(conn, ctx)
    everyone = {c.id: c for c in contacts} | {c.id: c for c in active}
    return sum(1 for c in everyone.values() if (c.name or "").lower() == name.lower())


async def test_no_email_stub_then_org_adopts_stub(app_engine):
    """The live dedupe bug: a name-only stub (organization_id NULL) then the same
    name WITH an org must ADOPT the stub — one contact, the second call matched,
    the stub's org filled to the target. Before the fix the org-given branch only
    matched ``organization_id == target`` exactly, so the NULL stub was invisible
    and a duplicate was created."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # First call: name only, no org → a NULL-org stub.
    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(conn, ctx, name="Xavier")
    assert action1 == "created"
    assert c1.organization_id is None

    # Second call: same name, now WITH an org name → adopt the stub, fill its org.
    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn, ctx, name="Xavier", organization_name="Acme"
        )
    assert action2 == "matched"
    assert c2.id == c1.id  # adopted the stub, no duplicate
    assert c2.organization_id is not None

    # Exactly one contact named Xavier in scope (active or archived).
    assert await _count_contacts_by_name(ctx, "Xavier") == 1

    # The stub's org is the org named "Acme".
    async with session_for(ctx) as conn:
        org, _counts = await store.get_organization(conn, ctx, org_id=c2.organization_id)
    assert org.name == "Acme"


async def test_no_email_different_orgs_do_not_over_merge(app_engine):
    """Two name-matched contacts with DIFFERENT orgs and no NULL stub must NOT be
    merged — there is no stub to adopt, so the second is a genuine create."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        c1, action1, _ = await store.find_or_create_contact(
            conn, ctx, name="Yara", organization_name="Acme"
        )
    assert action1 == "created"

    async with session_for(ctx) as conn:
        c2, action2, _ = await store.find_or_create_contact(
            conn, ctx, name="Yara", organization_name="Beta"
        )
    assert action2 == "created"
    assert c2.id != c1.id  # different orgs, no NULL stub → two contacts
    assert c1.organization_id != c2.organization_id

    assert await _count_contacts_by_name(ctx, "Yara") == 2


async def test_no_email_exact_org_match_preferred_over_stub(app_engine):
    """When BOTH an exact-org contact and a NULL stub exist for the same name, the
    exact-org match wins (priority a), leaving the stub untouched.

    Order matters: create the exact-org row FIRST (no stub yet → genuine create),
    then a name-only NULL stub (the name-only path never adopts). Re-running with
    the same org must match the exact-org row, not the stub.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # Exact-org contact (org=Acme), created before any stub exists.
    async with session_for(ctx) as conn:
        exact, action1, _ = await store.find_or_create_contact(
            conn, ctx, name="Zane", organization_name="Acme"
        )
    assert action1 == "created"
    org_id = exact.organization_id
    assert org_id is not None

    # A separate NULL-org stub for the SAME name (name-only path never adopts).
    async with session_for(ctx) as conn:
        stub, action2, _ = await store.find_or_create_contact(conn, ctx, name="Zane")
    assert action2 == "created"
    assert stub.id != exact.id
    assert stub.organization_id is None

    # Re-run with the SAME org as `exact` → must match `exact` (priority a),
    # NOT the NULL stub.
    async with session_for(ctx) as conn:
        matched, action3, _ = await store.find_or_create_contact(
            conn, ctx, name="Zane", organization_id=org_id
        )
    assert action3 == "matched"
    assert matched.id == exact.id  # exact-org match preferred over the NULL stub

    # The stub is still a NULL-org row, untouched.
    async with session_for(ctx) as conn:
        still_stub, _counts = await store.get_contact(conn, ctx, contact_id=stub.id)
    assert still_stub.organization_id is None


# --------------------------------------------------------------------------- #
# 13. merge_contacts: reassign interactions, fill empty, archive source       #
# --------------------------------------------------------------------------- #


async def test_merge_contacts_reassigns_fills_and_archives(app_engine):
    """Merge a source (with interactions + a title the target lacks) into a
    target: interactions move to the target, the target's EMPTY fields fill from
    source, already-set target fields are NOT overwritten, and the source is
    archived (archived_at set), not deleted."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    # Target: has a phone (must NOT be overwritten), no title, no email.
    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dup Target", phone="555-TARGET"
        )
        # Source: has an email + phone + a title; some overlap with target.
        source, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Dup Source",
            email="dup@ex.example",
            phone="555-SOURCE",
            title="Engineer",
        )
        # Two interactions on the source.
        ix1 = await store.log_interaction(
            conn, ctx, contact_id=source.id, kind="note", summary="one"
        )
        ix2 = await store.log_interaction(
            conn, ctx, contact_id=source.id, kind="note", summary="two"
        )

    async with session_for(ctx) as conn:
        merged, summary = await store.merge_contacts(
            conn, ctx, target_id=target.id, source_id=source.id
        )

    # Interactions moved to the target.
    assert summary["interactions_moved"] == 2
    async with session_for(ctx) as conn:
        target_ix, _ = await store.list_interactions(conn, ctx, contact_id=target.id)
    assert {i.id for i in target_ix} == {ix1.id, ix2.id}

    # Target's EMPTY fields filled from source; populated phone NOT overwritten.
    assert merged.phone == "555-TARGET"  # already set → preserved
    assert merged.email == "dup@ex.example"  # was empty → filled
    assert merged.title == "Engineer"  # was empty → filled
    assert "email" in summary["fields_filled"]
    assert "title" in summary["fields_filled"]
    assert "phone" not in summary["fields_filled"]

    # Source archived (not hard-deleted): absent from the default view, present
    # with include_archived.
    async with session_for(ctx) as conn:
        default_list, _c1 = await store.list_contacts(conn, ctx)
        archived_list, _c2 = await store.list_contacts(conn, ctx, include_archived=True)
    assert source.id not in {c.id for c in default_list}
    archived = {c.id: c for c in archived_list}
    assert source.id in archived
    assert archived[source.id].archived_at is not None


async def test_merge_contacts_unknown_id_raises_not_found(app_engine):
    """Merging with an out-of-scope id raises NotFound (mapped to a 404 at the
    tool layer), never a silent write."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Real", email="real@ex.example"
        )

    with pytest.raises(ContactNotFoundError):
        async with session_for(ctx) as conn:
            await store.merge_contacts(
                conn, ctx, target_id=target.id, source_id="ct_does_not_exist"
            )


# --------------------------------------------------------------------------- #
# 14. Archived contacts hidden from default list / search, shown on request   #
# --------------------------------------------------------------------------- #


async def test_archived_contact_hidden_from_default_list_and_search(app_engine):
    """An archived contact is absent from the default list_contacts and from
    search, but present when list_contacts(include_archived=True) is requested."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        active, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Archie Active", email="active@ex.example"
        )
        gone, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Archie Archived", email="gone@ex.example"
        )
        await store.archive_record(conn, ctx, entity="contact", record_id=gone.id, archived=True)

    # Default list: only the active contact.
    async with session_for(ctx) as conn:
        default_list, _c = await store.list_contacts(conn, ctx)
    ids = {c.id for c in default_list}
    assert active.id in ids
    assert gone.id not in ids

    # include_archived still returns the archived row.
    async with session_for(ctx) as conn:
        archived_list, _c = await store.list_contacts(conn, ctx, include_archived=True)
    assert gone.id in {c.id for c in archived_list}

    # Search excludes the archived contact.
    async with session_for(ctx) as conn:
        results = await store.page_contacts(conn, ctx, terms="Archie")
    result_ids = {r.id for r in results.rows}
    assert active.id in result_ids
    assert gone.id not in result_ids


# --------------------------------------------------------------------------- #
# 14b. Multi-token search: "Matt Calloway" must find "Matthew Calloway"       #
# --------------------------------------------------------------------------- #


async def test_query_multi_token_matches_partial_name_words(app_engine):
    """A multi-token query matches when EACH token is a substring of the name,
    even when the whole query is not a contiguous substring and the tokens are
    given out of order. The regression: "Matt Calloway" found nothing because the
    search matched "%Matt Calloway%" as one literal substring, which "Matthew
    Calloway" does not contain — though "matt" alone is a substring of "matthew"."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        matthew, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Matthew Calloway", email="mcalloway@ex.example"
        )
        # A decoy that shares ONE token only — must be excluded by the AND.
        await store.find_or_create_contact(
            conn, ctx, name="Sarah Connor", email="sconnor@ex.example"
        )

    # The reported case: a name prefix token + a surname token, NOT a contiguous
    # substring of the stored name.
    async with session_for(ctx) as conn:
        hits = await store.page_contacts(conn, ctx, terms="Matt Calloway")
    assert matthew.id in {c.id for c in hits.rows}

    # Token order is irrelevant — each token is matched independently.
    async with session_for(ctx) as conn:
        reordered = await store.page_contacts(conn, ctx, terms="Calloway Matt")
    assert matthew.id in {c.id for c in reordered.rows}

    # AND semantics: an extra non-matching token narrows to nothing, never widens.
    async with session_for(ctx) as conn:
        narrowed = await store.page_contacts(conn, ctx, terms="Matt Connor")
    narrowed_ids = {c.id for c in narrowed.rows}
    assert matthew.id not in narrowed_ids


async def test_query_matches_name_or_email_and_pages_in_listing_order(app_engine):
    """A listing's ``query`` keeps the rows whose name or email holds every token, in the
    listing's own order, and its cursor resumes under the same query."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        by_name, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Matthew Calloway", email="mcalloway@ex.example"
        )
        by_email, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Joe Bloggs", email="dmatto@ex.example"
        )
        await store.find_or_create_contact(conn, ctx, name="Sarah Jones", email="sj@ex.example")
        literal, _, _ = await store.find_or_create_contact(
            conn, ctx, name="100% Matt", email="pct@ex.example"
        )

    async with session_for(ctx) as conn:
        first = await store.page_contacts(conn, ctx, terms="matt", limit=2)
        rest = await store.page_contacts(conn, ctx, terms="matt", limit=2, cursor=first.next_cursor)
        escaped = await store.page_contacts(conn, ctx, terms="100%")
    # Newest first, as the listing orders, not ranked.
    assert [c.id for c in first.rows + rest.rows] == [literal.id, by_email.id, by_name.id]
    assert first.total == 3 and rest.next_cursor is None
    # A % in the query is matched literally, not as a wildcard.
    assert [c.id for c in escaped.rows] == [literal.id]


async def test_merge_contacts_self_merge_raises(app_engine):
    """merge_contacts(X, X) raises ValueError before any DB work — it must never
    archive the contact the caller meant to KEEP."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Keep Me", email="keep@ex.example"
        )

    with pytest.raises(ValueError, match="must differ"):
        async with session_for(ctx) as conn:
            await store.merge_contacts(conn, ctx, target_id=keep.id, source_id=keep.id)

    # The contact is untouched: still active, still in the default view.
    async with session_for(ctx) as conn:
        got, _counts = await store.get_contact(conn, ctx, contact_id=keep.id)
        default_list, _c = await store.list_contacts(conn, ctx)
    assert got.archived_at is None
    assert keep.id in {c.id for c in default_list}


async def test_archived_contact_excluded_from_org_surfaces(app_engine):
    """A contact archived at an org drops out of organization_timeline's roster
    AND get_organization's contact_count, while an include_archived list still
    surfaces it."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        keep, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Org Keeper", email="orgkeep@ex.example", organization_name="Acme Co"
        )
        org_id = keep.organization_id
        assert org_id is not None
        dupe, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Org Dupe", email="orgdupe@ex.example", organization_id=org_id
        )
        await store.archive_record(conn, ctx, entity="contact", record_id=dupe.id, archived=True)

    # Org roster: only the live contact.
    async with session_for(ctx) as conn:
        contacts, _ = await store.list_contacts(conn, ctx, organization_id=org_id)
    roster_ids = {c.id for c in contacts}
    assert keep.id in roster_ids
    assert dupe.id not in roster_ids

    # contact_count counts only the live roster.
    async with session_for(ctx) as conn:
        _org, counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert counts["contacts"] == 1

    # include_archived still surfaces the dupe.
    async with session_for(ctx) as conn:
        archived_list, _c = await store.list_contacts(conn, ctx, include_archived=True)
    assert dupe.id in {c.id for c in archived_list}


async def test_merge_clears_source_email_so_it_never_shadows_dedupe(app_engine):
    """Merging two contacts that BOTH have distinct emails clears the source's
    email on archive, so a later find_or_create_contact on the source's former
    address does NOT ON-CONFLICT-match the hidden archived row."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Keep", email="keep-target@ex.example"
        )
        source, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dupe", email="dupe-source@ex.example"
        )

    async with session_for(ctx) as conn:
        merged, summary = await store.merge_contacts(
            conn, ctx, target_id=target.id, source_id=source.id
        )

    # Target keeps its own (already-set) email; source's email was NOT filled onto
    # it — yet the source's email must still have been cleared.
    assert merged.email == "keep-target@ex.example"
    assert "email" not in summary["fields_filled"]

    # The archived source now holds NO email.
    async with session_for(ctx) as conn:
        archived_list, _c = await store.list_contacts(conn, ctx, include_archived=True)
    archived = {c.id: c for c in archived_list}
    assert source.id in archived
    assert archived[source.id].email is None

    # find_or_create on the source's FORMER email creates a brand-new contact —
    # it does not resurrect / match the archived row.
    async with session_for(ctx) as conn:
        fresh, action, _ = await store.find_or_create_contact(
            conn, ctx, name="New Person", email="dupe-source@ex.example"
        )
    assert action == "created"
    assert fresh.id != source.id
    assert fresh.archived_at is None


# --------------------------------------------------------------------------- #
# 15. merge_contacts: colliding idempotency_key on both sides does NOT crash    #
# --------------------------------------------------------------------------- #


async def test_merge_retires_colliding_idempotency_key(app_engine):
    """Source and target each hold an interaction with the SAME idempotency_key
    (an outreach agent produces these on send-retry-dedupe). The blanket
    reassign would collide on uq_interaction_idem (unique PER CONTACT), so the
    merge retires the source key BEFORE the reassign. The merge must succeed (no
    IntegrityError), ALL interactions hang off the target, and the formerly-
    colliding source interaction now carries idempotency_key IS NULL."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Keep", email="keep@ex.example"
        )
        source, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dupe", email="dupe@ex.example"
        )
        # Each side logs an interaction under the SAME key — distinct rows today
        # because the index is per-contact, but a collision the moment they share
        # a contact_id.
        tgt_ix = await store.log_interaction(
            conn,
            ctx,
            contact_id=target.id,
            kind="email",
            summary="target step",
            idempotency_key="step-1",
        )
        src_ix = await store.log_interaction(
            conn,
            ctx,
            contact_id=source.id,
            kind="email",
            summary="source step",
            idempotency_key="step-1",
        )

    # The merge must not raise IntegrityError → internal_error.
    async with session_for(ctx) as conn:
        _merged, summary = await store.merge_contacts(
            conn, ctx, target_id=target.id, source_id=source.id
        )

    # Every interaction (both the target's and the moved source's) now hangs off
    # the target; the moved row's count is reflected in interactions_moved.
    assert summary["interactions_moved"] == 1
    async with session_for(ctx) as conn:
        target_ix, _ = await store.list_interactions(conn, ctx, contact_id=target.id)
    assert {i.id for i in target_ix} == {tgt_ix.id, src_ix.id}

    # The formerly-colliding source interaction moved AND had its spent key nulled
    # (the target's own interaction keeps its key).
    async with session_for(ctx) as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        interaction.c.id, interaction.c.contact_id, interaction.c.idempotency_key
                    )
                )
            )
            .mappings()
            .all()
        )
    by_id = {r["id"]: r for r in rows}
    assert by_id[src_ix.id]["contact_id"] == target.id
    assert by_id[src_ix.id]["idempotency_key"] is None
    assert by_id[tgt_ix.id]["contact_id"] == target.id
    assert by_id[tgt_ix.id]["idempotency_key"] == "step-1"


# --------------------------------------------------------------------------- #
# 16. merge_contacts: audit row records source_id in detail                     #
# --------------------------------------------------------------------------- #


async def test_merge_audit_detail_records_source_id(app_engine):
    """The durable merge audit row (action="merge") carries a detail JSONB whose
    source_id is the archived source — so what merged where is not lost from the
    log."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Keep", email="keep-d@ex.example"
        )
        source, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Dupe", email="dupe-d@ex.example"
        )

    async with session_for(ctx) as conn:
        await store.merge_contacts(conn, ctx, target_id=target.id, source_id=source.id)

    async with session_for(ctx) as conn:
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.entity_id, people_audit.c.detail).where(
                        people_audit.c.action == "merge"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_id"] == target.id
    assert row["detail"]["source_id"] == source.id
    assert row["detail"]["target_id"] == target.id
    assert "interactions_moved" in row["detail"]
    assert "fields_filled" in row["detail"]


# --------------------------------------------------------------------------- #
# 17. merge_contacts: archived-target / archived-source guards                   #
# --------------------------------------------------------------------------- #


async def test_merge_into_archived_target_raises_and_changes_nothing(app_engine):
    """Merging INTO an already-archived target raises ValueError (→ invalid_request)
    and nothing changes — the source stays active, the target stays archived."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Archived Target", email="at@ex.example"
        )
        await store.archive_record(conn, ctx, entity="contact", record_id=target.id, archived=True)
        source, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Live Source", email="ls@ex.example"
        )

    with pytest.raises(ValueError, match="cannot merge into an archived contact"):
        async with session_for(ctx) as conn:
            await store.merge_contacts(conn, ctx, target_id=target.id, source_id=source.id)

    # Nothing changed: source still active, target still archived.
    async with session_for(ctx) as conn:
        got_source, _ = await store.get_contact(conn, ctx, contact_id=source.id)
        got_target, _ = await store.get_contact(conn, ctx, contact_id=target.id)
    assert got_source.archived_at is None
    assert got_target.archived_at is not None


async def test_merge_already_archived_source_raises_and_changes_nothing(app_engine):
    """Merging an already-archived source raises ValueError (→ invalid_request) and
    nothing changes — the target stays active."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        target, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Live Target", email="lt@ex.example"
        )
        source, _, _ = await store.find_or_create_contact(
            conn, ctx, name="Archived Source", email="as@ex.example"
        )
        await store.archive_record(conn, ctx, entity="contact", record_id=source.id, archived=True)

    with pytest.raises(ValueError, match="source contact is already archived"):
        async with session_for(ctx) as conn:
            await store.merge_contacts(conn, ctx, target_id=target.id, source_id=source.id)

    async with session_for(ctx) as conn:
        got_target, _ = await store.get_contact(conn, ctx, contact_id=target.id)
    assert got_target.archived_at is None


# --------------------------------------------------------------------------- #
# 19. list_organizations allowlist: discovered hidden by default               #
# --------------------------------------------------------------------------- #


async def test_list_organizations_allowlist_hides_discovered(app_engine):
    """The default list returns ``active`` only; a ``discovered`` lookalike is
    hidden until an explicit ``status="discovered"`` surfaces it."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")

    async with session_for(ctx) as conn:
        active, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Real Co", domain="real.example"
        )
        counts = await store.link_similar_companies(
            conn,
            ctx,
            org_id=active.id,
            companies=[{"domain": "look.example", "name": "Lookalike", "similarity": 0.9}],
        )
    assert counts["created"] == 1

    # Default: only the active org; the discovered lookalike is hidden.
    async with session_for(ctx) as conn:
        default_orgs, _ = await store.list_organizations(conn, ctx)
        discovered_orgs, _ = await store.list_organizations(conn, ctx, status="discovered")
    default_ids = {o.id for o in default_orgs}
    assert active.id in default_ids
    assert len(discovered_orgs) == 1
    disc = discovered_orgs[0]
    assert disc.id not in default_ids  # hidden from the default allowlist
    assert disc.status == "discovered"
    assert disc.source == "similar_companies"
    assert disc.domain == "look.example"


# --------------------------------------------------------------------------- #
# 20. Promotion: find_or_create match promotes a discovered org; active stays   #
# --------------------------------------------------------------------------- #


async def _seed_discovered_org(ctx, *, seed_domain: str, look_domain: str, look_name: str) -> str:
    """Create one discovered lookalike via link_similar_companies; return its id."""
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain=seed_domain
        )
        await store.link_similar_companies(
            conn,
            ctx,
            org_id=seed.id,
            companies=[{"domain": look_domain, "name": look_name, "similarity": 0.8}],
        )
        discovered_orgs, _ = await store.list_organizations(conn, ctx, status="discovered")
    (disc_id,) = {o.id for o in discovered_orgs}
    return disc_id


async def test_find_or_create_promotes_discovered_match(app_engine):
    """A find_or_create match on a ``discovered`` org flips it to ``active`` and
    reports ``promoted=True``; re-matching the now-active org does not."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    disc_id = await _seed_discovered_org(
        ctx, seed_domain="seed.example", look_domain="look.example", look_name="Look Co"
    )

    async with session_for(ctx) as conn:
        org, action, promoted = await store.find_or_create_organization(
            conn, ctx, name="Look Co", domain="look.example"
        )
    assert action == "matched"
    assert org.id == disc_id
    assert promoted is True
    assert org.status == "active"

    # Now visible in the default list.
    async with session_for(ctx) as conn:
        default_orgs, _ = await store.list_organizations(conn, ctx)
    assert disc_id in {o.id for o in default_orgs}

    # Re-matching the already-active org is not a promotion.
    async with session_for(ctx) as conn:
        again, _action, promoted2 = await store.find_or_create_organization(
            conn, ctx, name="Look Co", domain="look.example"
        )
    assert again.id == disc_id
    assert promoted2 is False


async def test_update_organization_does_not_promote_discovered(app_engine):
    """Editing a discovered lookalike (``update_organization``) is NOT a promotion
    trigger — it stays ``discovered`` and hidden from the default list."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    disc_id = await _seed_discovered_org(
        ctx, seed_domain="s.example", look_domain="look.example", look_name="Look"
    )

    async with session_for(ctx) as conn:
        updated = await store.update_organization(conn, ctx, org_id=disc_id, industry="SaaS")
    assert updated.status == "discovered"  # editing does not promote
    assert updated.industry == "SaaS"

    async with session_for(ctx) as conn:
        default_orgs, _ = await store.list_organizations(conn, ctx)
    assert disc_id not in {o.id for o in default_orgs}


async def test_find_or_create_contact_promotes_discovered_org(app_engine):
    """Wiring a real contact to a ``discovered`` org promotes it to ``active`` and
    surfaces ``org_promoted=True`` (attaching a real contact means it is real)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    disc_id = await _seed_discovered_org(
        ctx, seed_domain="s.example", look_domain="look.example", look_name="Look"
    )

    async with session_for(ctx) as conn:
        contact, action, org_promoted = await store.find_or_create_contact(
            conn, ctx, name="Real Person", email="rp@look.example", organization_id=disc_id
        )
    assert action == "created"
    assert org_promoted is True
    assert contact.organization_id == disc_id

    async with session_for(ctx) as conn:
        org, _ = await store.get_organization(conn, ctx, org_id=disc_id)
    assert org.status == "active"


async def test_find_or_create_promotion_writes_promote_audit(app_engine):
    """A discovered→active flip triggered by a find_or_create MATCH writes a durable
    ``promote`` audit row (entity_id = the promoted org) — as observable as an
    explicit promote_organization, and distinct from the co-emitted find_or_create
    row. Seeding the lookalike (link_similar_companies) is not itself a promotion, so
    the only ``promote`` row is the match-triggered one."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    disc_id = await _seed_discovered_org(
        ctx, seed_domain="seed.example", look_domain="look.example", look_name="Look Co"
    )

    async with session_for(ctx) as conn:
        _org, action, promoted = await store.find_or_create_organization(
            conn, ctx, name="Look Co", domain="look.example"
        )
    assert action == "matched"
    assert promoted is True

    async with session_for(ctx) as conn:
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.entity_id).where(people_audit.c.action == "promote")
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["entity_id"] == disc_id


# --------------------------------------------------------------------------- #
# 21. promote_organization: discovered → active                                #
# --------------------------------------------------------------------------- #


async def test_promote_organization_moves_into_default_views(app_engine):
    """promote_organization flips a discovered org to active so it enters the
    default list."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    disc_id = await _seed_discovered_org(
        ctx, seed_domain="s.example", look_domain="look.example", look_name="Look"
    )

    async with session_for(ctx) as conn:
        promoted = await store.promote_organization(conn, ctx, org_id=disc_id)
    assert promoted.status == "active"

    async with session_for(ctx) as conn:
        default_orgs, _ = await store.list_organizations(conn, ctx)
    assert disc_id in {o.id for o in default_orgs}


async def test_promote_organization_unknown_id_raises_not_found(app_engine):
    """Promoting an out-of-scope / absent org id raises NotFound (404 at the tool
    layer), never a silent write."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(OrganizationNotFoundError):
        async with session_for(ctx) as conn:
            await store.promote_organization(conn, ctx, org_id="org_missing")


# --------------------------------------------------------------------------- #
# 22. _upsert_edge idempotency (the edge-write core)                            #
# --------------------------------------------------------------------------- #


async def test_upsert_edge_is_idempotent(app_engine):
    """Two _upsert_edge calls on the same (from, to, relation) converge to ONE edge
    under uq_organization_edge — never a duplicate (the first row wins). This is the
    edge-write core link_similar_companies leans on for its re-link convergence."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        a, _, _ = await store.find_or_create_organization(conn, ctx, name="A", domain="a.example")
        b, _, _ = await store.find_or_create_organization(conn, ctx, name="B", domain="b.example")
        e1 = await store._upsert_edge(
            conn, ctx, from_id=a.id, to_id=b.id, relation="similar_to", score=0.9, source=None
        )
        e2 = await store._upsert_edge(
            conn, ctx, from_id=a.id, to_id=b.id, relation="similar_to", score=0.1, source=None
        )
    assert e1.id == e2.id  # converged to one edge (the first row wins)

    async with session_for(ctx) as conn:
        rows = (await conn.execute(select(organization_edge.c.id))).fetchall()
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# 23. link_similar_companies: created/linked/skipped + provenance guardrail      #
# --------------------------------------------------------------------------- #


async def test_link_similar_companies_counts_and_provenance(app_engine):
    """link_similar_companies creates+links new lookalikes as discovered, links an
    existing ACTIVE org WITHOUT downgrading it, skips a domainless row, and is
    idempotent on re-link (created drops to 0, linked/skipped stay)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain="seed.example"
        )
        existing, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Existing Customer", domain="existing.example", source="manual"
        )
    assert existing.status == "active"

    companies = [
        {
            "domain": "new1.example",
            "name": "New One",
            "similarity": 0.9,
            "employee_range": "11-50",
            "revenue_range": "$1M-$10M",
            "headquarters": "Honolulu, HI",
        },
        {"domain": "existing.example", "name": "Existing (lookalike)", "similarity": 0.8},
        {"name": "No Domain Co", "similarity": 0.7},  # domainless → skipped
    ]
    async with session_for(ctx) as conn:
        counts = await store.link_similar_companies(conn, ctx, org_id=seed.id, companies=companies)
    assert counts == {"linked": 2, "created": 1, "skipped": 1}

    # The existing ACTIVE customer is neither downgraded nor restamped.
    async with session_for(ctx) as conn:
        again, _ = await store.get_organization(conn, ctx, org_id=existing.id)
    assert again.status == "active"
    assert again.source == "manual"

    # The freshly-inserted lookalike is discovered + provenance-flagged.
    async with session_for(ctx) as conn:
        discovered_orgs, _ = await store.list_organizations(conn, ctx, status="discovered")
    by_domain = {o.domain: o for o in discovered_orgs}
    assert "existing.example" not in by_domain  # the active org stayed active
    assert "new1.example" in by_domain
    new_org = by_domain["new1.example"]
    assert new_org.status == "discovered"
    assert new_org.source == "similar_companies"
    # Firmographics persist where they are actually written — link_similar_companies
    # (its own _upsert_returning is the sole writer; no tool wires them elsewhere).
    assert new_org.employee_range == "11-50"
    assert new_org.revenue_range == "$1M-$10M"
    assert new_org.headquarters == "Honolulu, HI"

    # Re-linking the same set converges: no new orgs, edges idempotent.
    async with session_for(ctx) as conn:
        counts2 = await store.link_similar_companies(conn, ctx, org_id=seed.id, companies=companies)
    assert counts2 == {"linked": 2, "created": 0, "skipped": 1}


async def test_link_similar_companies_unknown_seed_raises_not_found(app_engine):
    """Linking against an out-of-scope seed org raises NotFound, never a write."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    with pytest.raises(OrganizationNotFoundError):
        async with session_for(ctx) as conn:
            await store.link_similar_companies(
                conn,
                ctx,
                org_id="org_missing",
                companies=[{"domain": "x.example", "name": "X", "similarity": 0.5}],
            )


async def test_link_similar_companies_dedups_and_skips_malformed(app_engine):
    """A duplicate domain in the batch links ONCE (not twice); a row over a length cap
    is skipped without rolling back the batch; and a malformed FIRST occurrence of a
    domain does not pre-consume it — a valid later occurrence still links."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain="seed.example"
        )

    too_long_name = "n" * (store._CAP_NAME + 1)
    companies = [
        {"domain": "dup.example", "name": "Dup A", "similarity": 0.9},  # linked (created)
        {"domain": "dup.example", "name": "Dup B", "similarity": 0.5},  # dup → not counted
        {"domain": "bad.example", "name": too_long_name, "similarity": 0.8},  # over cap → skipped
        {"domain": "bad.example", "name": "Bad Good", "similarity": 0.7},  # valid later → linked
        {"name": "No Domain", "similarity": 0.3},  # domainless → skipped
    ]
    async with session_for(ctx) as conn:
        counts = await store.link_similar_companies(conn, ctx, org_id=seed.id, companies=companies)
    # dup.example + bad.example link once each; the malformed bad row and the
    # domainless row are the two skips; the in-batch dup is counted in neither.
    assert counts == {"linked": 2, "created": 2, "skipped": 2}

    async with session_for(ctx) as conn:
        similar = await store.list_similar_companies(conn, ctx, org_id=seed.id)
    by_domain = {org.domain: org for org, _s, _d in similar}
    assert set(by_domain) == {"dup.example", "bad.example"}
    # The valid later occurrence created bad.example (the malformed first never wrote).
    assert by_domain["bad.example"].name == "Bad Good"


# --------------------------------------------------------------------------- #
# 24. list_similar_companies: join to targets, ordered by similarity            #
# --------------------------------------------------------------------------- #


async def test_list_similar_companies_returns_targets_with_scores(app_engine):
    """list_similar_companies returns each linked target org with its similarity and
    a discovered_at, ordered by similarity (highest first)."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain="seed.example"
        )
        await store.link_similar_companies(
            conn,
            ctx,
            org_id=seed.id,
            companies=[
                {"domain": "high.example", "name": "High", "similarity": 0.95},
                {"domain": "low.example", "name": "Low", "similarity": 0.20},
            ],
        )
    async with session_for(ctx) as conn:
        similar = await store.list_similar_companies(conn, ctx, org_id=seed.id)

    assert [org.domain for org, _s, _d in similar] == ["high.example", "low.example"]
    assert [score for _o, score, _d in similar] == [0.95, 0.20]
    assert all(discovered_at is not None for _o, _s, discovered_at in similar)
    assert all(org.status == "discovered" for org, _s, _d in similar)


# --------------------------------------------------------------------------- #
# Organization name dedupe — the key behind the domain-less path               #
# --------------------------------------------------------------------------- #


async def test_database_refuses_a_second_domainless_org_with_the_same_name():
    """The guarantee is the INDEX, so assert the database itself refuses.

    The production failure was five parallel `find_or_create_contact(
    organization_name=...)` calls for one company, in five transactions, all
    reading "absent" and all inserting — four rows inside a 15ms window. No
    application-level check closes that, because the read is not a lock; only a
    unique constraint is. Racing five real connections here would prove nothing
    the constraint doesn't, and would leave a multi-connection pool bound to a
    closed event loop for the next test to trip over. So: write past the store
    and require the database to say no.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seeded, _, _ = await store.find_or_create_organization(conn, ctx, name="Vandelay")

    async with session_for(ctx) as conn:
        # Inside a SAVEPOINT: the violation aborts the nested transaction only,
        # so the enclosing one stays usable and commits cleanly on exit. Letting
        # the IntegrityError escape the session instead poisons the connection,
        # which then returns to the pool broken and fails a later, unrelated test
        # during teardown.
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    organization.insert().values(
                        id="org_forced_duplicate",
                        tenant_id=ctx.tenant_id,
                        workspace_id=ctx.workspace_id,
                        # Different case on purpose — the index is on the name key.
                        name="VANDELAY",
                        name_key=match_key("VANDELAY"),
                        domain=None,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )

    async with session_for(ctx) as conn:
        rows = (
            (await conn.execute(select(organization).where(organization.c.domain.is_(None))))
            .mappings()
            .all()
        )
    assert [r["id"] for r in rows] == [seeded.id]


async def test_repeated_domainless_creates_converge_to_one_org():
    """Call it five times over, get one row: created once, matched after."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    seen = []
    for _ in range(5):
        async with session_for(ctx) as conn:
            org, action, _ = await store.find_or_create_organization(conn, ctx, name="Vandelay")
            seen.append((org.id, action))

    assert len({org_id for org_id, _ in seen}) == 1
    assert [action for _, action in seen] == ["created"] + ["matched"] * 4


async def test_domainless_call_matches_an_org_that_has_a_domain():
    """A call with no domain must find the company already on file WITH one.

    The name index is partial on `domain IS NULL`, so it cannot see a domained
    row. If the lookup were narrowed to match the index, every domain-less call
    for a known company would mint a second row — the very duplication this is
    meant to prevent.
    """
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seeded, action, _ = await store.find_or_create_organization(
            conn, ctx, name="Initech", domain="initech.example"
        )
    assert action == "created"

    async with session_for(ctx) as conn:
        found, action, _ = await store.find_or_create_organization(conn, ctx, name="initech")

    assert action == "matched"
    assert found.id == seeded.id
    assert found.domain == "initech.example"


async def test_two_domained_orgs_may_share_a_name():
    """Same name, different domains — different companies. The partial index
    must not refuse the second."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        us, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example"
        )
        uk, action, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme", domain="acme.example.co.uk"
        )
    assert action == "created"
    assert us.id != uk.id


async def test_rename_onto_a_domainless_namesake_reports_a_name_conflict():
    """The org has two partial unique indexes now, so an update can collide on
    either. The 409 must name the field the caller actually sent — reporting
    `domain` for a name collision points at a field they never passed."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await store.find_or_create_organization(conn, ctx, name="Vandelay")
        other, _, _ = await store.find_or_create_organization(conn, ctx, name="Kruger")

    with pytest.raises(ConflictError) as caught:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=other.id, name="Vandelay")

    assert "name" in str(caught.value)
    assert "domain" not in str(caught.value)


# --------------------------------------------------------------------------- #
# merge_organizations                                                          #
# --------------------------------------------------------------------------- #


async def _org(conn, ctx, name, **kw):
    org, _, _ = await store.find_or_create_organization(conn, ctx, name=name, **kw)
    return org


async def test_merge_organizations_moves_every_reference_and_archives_the_source():
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Vandelay Industries", domain="vandelay.example")
        dup = await _org(conn, ctx, "Vandelay Inc")
        c, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Art Vandelay",
            email="art@vandelay.example",
            organization_id=dup.id,
        )
        await store.log_interaction(conn, ctx, contact_id=c.id, kind="meeting", summary="intro")

    async with session_for(ctx) as conn:
        merged, summary = await store.merge_organizations(
            conn, ctx, target_id=keep.id, source_id=dup.id
        )

    assert merged.id == keep.id
    assert summary["contacts_moved"] == 1
    assert summary["interactions_moved"] == 1

    async with session_for(ctx) as conn:
        moved = (await conn.execute(select(contact).where(contact.c.id == c.id))).mappings().one()
        src = (
            (await conn.execute(select(organization).where(organization.c.id == dup.id)))
            .mappings()
            .one()
        )
    assert moved["organization_id"] == keep.id
    assert src["archived_at"] is not None


async def test_merge_organizations_archived_source_shadows_neither_dedupe_key():
    """The archived row must not ON-CONFLICT-match a later find_or_create — not
    on `domain` (uq_org_domain) and not on `name` (uq_org_name_when_domainless).
    Clearing the domain alone would move it INTO the name index, where a merge of
    two same-named orgs collides immediately."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Acme")
        dup = await _org(conn, ctx, "Acme Corp", domain="acme.example")

    async with session_for(ctx) as conn:
        await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)

    async with session_for(ctx) as conn:
        src = (
            (await conn.execute(select(organization).where(organization.c.id == dup.id)))
            .mappings()
            .one()
        )
    assert src["domain"] is None
    assert src["name"].endswith(f"({dup.id} merged into {keep.id})")

    # The domain is not orphaned — fill-empty moved it onto the survivor, so a
    # later lookup by that domain resolves to the KEPT org, never the hidden
    # archived one.
    async with session_for(ctx) as conn:
        again, action, _ = await store.find_or_create_organization(
            conn, ctx, name="Acme Reborn", domain="acme.example"
        )
    assert action == "matched"
    assert again.id == keep.id


async def test_merge_organizations_of_two_same_named_orgs_is_possible():
    """The ordinary merge shape. Both domain-less and identically named cannot
    coexist under the new index, but one CAN carry a domain — and after the
    merge the archived source must not collide with the target's name."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Initech")
        dup = await _org(conn, ctx, "Initech", domain="initech.example")
    assert keep.id != dup.id

    async with session_for(ctx) as conn:
        merged, _ = await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)
    # Fill-empty gave the survivor the domain the duplicate carried.
    assert merged.domain == "initech.example"


async def test_merge_organizations_collapses_edges_that_cannot_survive():
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Keep Co")
        dup = await _org(conn, ctx, "Dup Co")
        other = await _org(conn, ctx, "Other Co")
        for src, dst in ((keep.id, other.id), (dup.id, other.id), (dup.id, keep.id)):
            await conn.execute(
                organization_edge.insert().values(
                    id=f"oe_{src[-6:]}_{dst[-6:]}",
                    tenant_id=ctx.tenant_id,
                    workspace_id=ctx.workspace_id,
                    from_organization_id=src,
                    to_organization_id=dst,
                    relation="similar_to",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    async with session_for(ctx) as conn:
        _, summary = await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)

    assert summary["edges_dropped"] == 2  # the self-edge and the duplicate
    async with session_for(ctx) as conn:
        rows = (await conn.execute(select(organization_edge))).mappings().all()
    live = [r for r in rows if r["deleted_at"] is None]
    assert [(r["from_organization_id"], r["to_organization_id"]) for r in live] == [
        (keep.id, other.id)
    ]
    # The dropped edges are marked, not removed, and keep their raw endpoints.
    assert sorted(
        (r["from_organization_id"], r["to_organization_id"]) for r in rows if r["deleted_at"]
    ) == sorted([(dup.id, other.id), (dup.id, keep.id)])
    async with session_for(ctx) as conn:
        similar = await store.list_similar_companies(conn, ctx, org_id=keep.id)
        from_dup = await store.list_similar_companies(conn, ctx, org_id=dup.id)
    assert [org.id for org, _score, _at in similar] == [other.id]
    assert from_dup == []  # the archived source keeps only marked edges


async def test_a_merge_marks_a_duplicate_edge_a_relink_can_take_again():
    """An edge a merge marked holds no key: linking the same pair again writes a new live
    edge beside it, and the lookalike is listed once."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain="seed.example"
        )
        keep, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Keep", domain="keep.example"
        )
        dup, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Dup", domain="dup.example"
        )
        for target in (keep, dup):
            await store._upsert_edge(
                conn,
                ctx,
                from_id=seed.id,
                to_id=target.id,
                relation="similar_to",
                score=0.5,
                source=None,
            )
    async with session_for(ctx) as conn:
        _, summary = await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)
        assert summary["edges_dropped"] == 1
        await store.archive_record(
            conn, ctx, entity="organization", record_id=dup.id, archived=False
        )
        await store._upsert_edge(
            conn,
            ctx,
            from_id=seed.id,
            to_id=dup.id,
            relation="similar_to",
            score=0.7,
            source=None,
        )
        similar = await store.list_similar_companies(conn, ctx, org_id=seed.id)
        rows = (await conn.execute(select(organization_edge))).mappings().all()
    assert sorted(org.id for org, _s, _a in similar) == sorted([keep.id, dup.id])
    pair = [r for r in rows if r["to_organization_id"] == dup.id]
    assert len(pair) == 2 and sum(r["deleted_at"] is None for r in pair) == 1


async def test_an_archived_lookalike_is_not_listed():
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        seed, _, _ = await store.find_or_create_organization(
            conn, ctx, name="Seed", domain="seed.example"
        )
        await store.link_similar_companies(
            conn,
            ctx,
            org_id=seed.id,
            companies=[
                {"domain": "kept.example", "name": "Kept", "similarity": 0.9},
                {"domain": "shelved.example", "name": "Shelved", "similarity": 0.8},
            ],
        )
        listed = await store.list_similar_companies(conn, ctx, org_id=seed.id)
        shelved = next(org for org, _s, _a in listed if org.name == "Shelved")
        await store.archive_record(
            conn, ctx, entity="organization", record_id=shelved.id, archived=True
        )
        after = await store.list_similar_companies(conn, ctx, org_id=seed.id)
        await store.archive_record(
            conn, ctx, entity="organization", record_id=shelved.id, archived=False
        )
        restored = await store.list_similar_companies(conn, ctx, org_id=seed.id)
    assert [org.name for org, _s, _a in after] == ["Kept"]
    assert [org.name for org, _s, _a in restored] == ["Kept", "Shelved"]


async def test_merge_organizations_refuses_self_and_archived():
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        a = await _org(conn, ctx, "A Co")
        b = await _org(conn, ctx, "B Co")

    with pytest.raises(ValueError):
        async with session_for(ctx) as conn:
            await store.merge_organizations(conn, ctx, target_id=a.id, source_id=a.id)

    async with session_for(ctx) as conn:
        await store.merge_organizations(conn, ctx, target_id=a.id, source_id=b.id)

    with pytest.raises(ValueError):
        async with session_for(ctx) as conn:
            await store.merge_organizations(conn, ctx, target_id=a.id, source_id=b.id)


async def test_merge_two_same_named_duplicates_into_one_target():
    """The tool's own headline case, and the one the archived-source rename has
    to survive. The name index is partial, so same-name rows carrying different
    domains are legal; folding two of them into a third is exactly "different
    domains, same company". Keying the rename on the target alone gave both
    archived rows a byte-identical domain-less name and the second merge died on
    uq_org_name_when_domainless."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Acme", domain="keep.example")
        d1 = await _org(conn, ctx, "Acme", domain="a.example")
        d2 = await _org(conn, ctx, "Acme", domain="b.example")

    for dup in (d1, d2):
        async with session_for(ctx) as conn:
            await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)

    async with session_for(ctx) as conn:
        rows = (
            (await conn.execute(select(organization).where(organization.c.archived_at.isnot(None))))
            .mappings()
            .all()
        )
    assert {r["id"] for r in rows} == {d1.id, d2.id}
    assert len({r["name"] for r in rows}) == 2, "archived names must be distinct"


async def test_merge_organizations_repoints_edges_and_opportunities():
    """r1f2: the edge test drops both source edges, so nothing covered the
    repoint itself, and nothing created an opportunity at all."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Keeper")
        dup = await _org(conn, ctx, "Dupe")
        far = await _org(conn, ctx, "Faraway")
        # An edge the target does NOT hold — must move, not be dropped.
        await conn.execute(
            organization_edge.insert().values(
                id="oe_moves",
                tenant_id=ctx.tenant_id,
                workspace_id=ctx.workspace_id,
                from_organization_id=dup.id,
                to_organization_id=far.id,
                relation="similar_to",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        opp, _ = await store.find_or_create_opportunity(
            conn, ctx, title="Renewal", organization_id=dup.id
        )

    async with session_for(ctx) as conn:
        _, summary = await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)

    assert summary["edges_moved"] == 1
    assert summary["edges_dropped"] == 0
    assert summary["opportunities_moved"] == 1

    async with session_for(ctx) as conn:
        edge = (await conn.execute(select(organization_edge))).mappings().one()
        got, _counts = await store.get_opportunity(conn, ctx, opportunity_id=opp.id)
    assert edge["from_organization_id"] == keep.id
    assert got.organization_id == keep.id


async def test_merge_organizations_writes_an_audit_row():
    """r1f3: merge_contacts has an audit test; this had none. The archived
    source_id and the discarded domain are only recoverable from here."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        keep = await _org(conn, ctx, "Keeper", domain="keeper.example")
        dup = await _org(conn, ctx, "Dupe", domain="dupe.example")

    async with session_for(ctx) as conn:
        await store.merge_organizations(conn, ctx, target_id=keep.id, source_id=dup.id)

    async with session_for(ctx) as conn:
        row = (
            (await conn.execute(select(people_audit).where(people_audit.c.action == "merge")))
            .mappings()
            .one()
        )
    assert row["entity"] == "organization"
    assert row["entity_id"] == keep.id
    assert row["outcome"] == "ok"
    detail = row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert detail["source_id"] == dup.id
    # The domain the archive cleared, and the target already had, is recorded.
    assert detail["source_domain"] == "dupe.example"
