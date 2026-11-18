"""Evidence store behaviour.

The contract these lock: a signal is idempotent on the fact it records, a
withdrawn signal is gone from the read that matters without being gone from the
database, and a proof point's ``verification`` is the only thing that makes it
citable. The pure validators live in ``test_evidence_validation.py``.

Every fixture company here is fictional.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from conftest import define_signal_types, make_ctx, session_for
from sqlalchemy import select

from mcp_people.db import store
from mcp_people.db.store import ProofPointNotFoundError, SignalNotFoundError
from mcp_people.db.tables import people_audit
from mcp_people.db.tables import signal as signal_table

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def ctx(app_engine):
    """A fresh workspace whose vocabulary holds the types these cases record."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "announcement", "expansion", "hiring", "regulatory")
    return ctx


async def _org(ctx, *, name: str = "Northwind Freight", domain: str = "northwind.example") -> str:
    async with session_for(ctx) as conn:
        org, _action, _promoted = await store.find_or_create_organization(
            conn, ctx, name=name, domain=domain
        )
    return org.id


# The baseline signal every case starts from; each overrides only what it is about.
_SIGNAL: dict[str, Any] = {
    "claim": "Northwind is standing up a second distribution centre.",
    "evidence_quote": "Our second distribution centre opens in the spring.",
    "source_url": "https://news.example/northwind-dc",
    "signal_type": "expansion",
}


async def _record(conn, ctx, org_id: str, **overrides: Any):
    """Record the baseline signal for ``org_id`` with ``overrides`` applied.

    Unpacked field by field rather than splatted, so the call site stays typed —
    a ``**dict`` splat erases which value belongs to which parameter.
    """
    f = {**_SIGNAL, **overrides}
    return await store.record_signal(
        conn,
        ctx,
        organization_id=org_id,
        claim=f["claim"],
        evidence_quote=f["evidence_quote"],
        source_url=f["source_url"],
        signal_type=f["signal_type"],
        contact_id=f.get("contact_id"),
        source_date=f.get("source_date"),
        event_date=f.get("event_date"),
        found_by=f.get("found_by"),
    )


# --------------------------------------------------------------------------- #
# Signals                                                                      #
# --------------------------------------------------------------------------- #


async def test_record_signal_stores_the_full_attestation(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, action, _stored = await _record(
            conn,
            ctx,
            org_id,
            source_date=date(2026, 3, 1),
            found_by="research run 12",
        )
    assert action == "created"
    assert sig.id.startswith("sg_")
    assert sig.claim == _SIGNAL["claim"]
    assert sig.evidence_quote == _SIGNAL["evidence_quote"]
    assert sig.source_date == date(2026, 3, 1)
    assert sig.status == "active"
    assert sig.withdrawn_at is None


async def test_record_signal_refuses_a_claim_that_drops_its_quotes_hedge(ctx) -> None:
    """The reported shape: a blog alleges an attack, and the claim states it as fact.
    Refused before anything is written, so no row exists for a draft to cite."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="alleges"):
            await _record(
                conn,
                ctx,
                org_id,
                claim="Northwind was hit by ransomware in August.",
                evidence_quote=(
                    "Northwind is investigating what a security blog alleges was a ransomware "
                    "attack on its billing systems."
                ),
                source_url="https://blog.example/northwind-incident",
                signal_type="announcement",
            )
    async with session_for(ctx) as conn:
        rows = (await conn.execute(select(signal_table))).all()
    assert rows == []


async def test_record_signal_normalizes_the_source_url_on_the_stored_row(ctx) -> None:
    """The stored URL is the normalized one, which is what makes the dedupe key
    behave — two spellings of one page are one signal, not two."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _, _ = await _record(
            conn, ctx, org_id, source_url="HTTPS://News.Example/northwind-dc#more"
        )
    assert sig.source_url == "https://news.example/northwind-dc"


async def test_re_recording_the_same_fact_converges_to_one_row(ctx) -> None:
    """Idempotent on (organization, source_url, signal_type): re-running research
    over an account matches instead of stacking near-duplicates."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        first, first_action, _ = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        # A differently-cased URL: same fact, same page.
        second, second_action, second_stored = await _record(
            conn, ctx, org_id, source_url="https://NEWS.example/northwind-dc"
        )
    assert (first_action, second_action) == ("created", "matched")
    assert second.id == first.id
    # The row holds the claim and quote this call submitted, so nothing was lost.
    assert second_stored is True

    async with session_for(ctx) as conn:
        signals, _ = await store.list_signals(conn, ctx, organization_id=org_id)
    assert len(signals) == 1


async def test_a_match_fills_only_the_empty_annotation_fields(ctx) -> None:
    """A re-record adds context it did not have (provenance, the date) and rewrites
    nothing that was already there."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await _record(conn, ctx, org_id, found_by="research run 12")
    async with session_for(ctx) as conn:
        second, action, _ = await _record(
            conn,
            ctx,
            org_id,
            found_by="research run 13",
            source_date=date(2026, 3, 1),
        )
    assert action == "matched"
    assert second.found_by == "research run 12"  # already had one; untouched
    assert second.source_date == date(2026, 3, 1)  # was empty; filled


async def test_event_date_is_stored_and_returned_apart_from_source_date(ctx) -> None:
    """The article's date and the event's date are two facts; each read returns both."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _action, _stored = await _record(
            conn, ctx, org_id, source_date=date(2026, 5, 4), event_date=date(2026, 2, 1)
        )
    assert (sig.source_date, sig.event_date) == (date(2026, 5, 4), date(2026, 2, 1))
    async with session_for(ctx) as conn:
        got = await store.get_signal(conn, ctx, signal_id=sig.id)
        listed, _ = await store.list_signals(conn, ctx, organization_id=org_id)
    assert got.event_date == date(2026, 2, 1)
    assert [s.event_date for s in listed] == [date(2026, 2, 1)]


async def test_event_date_stays_null_and_is_never_taken_from_source_date(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _action, _stored = await _record(conn, ctx, org_id, source_date=date(2026, 3, 1))
    assert sig.source_date == date(2026, 3, 1)
    assert sig.event_date is None


async def test_an_event_date_in_the_future_is_refused(ctx) -> None:
    org_id = await _org(ctx)
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="event_date .* is in the future"):
            await _record(conn, ctx, org_id, event_date=tomorrow)
    async with session_for(ctx) as conn:
        rows = (await conn.execute(select(signal_table))).all()
    assert rows == []


async def test_an_event_date_today_is_accepted(ctx) -> None:
    org_id = await _org(ctx)
    today = datetime.now(UTC).date()
    async with session_for(ctx) as conn:
        sig, _action, _stored = await _record(conn, ctx, org_id, event_date=today)
    assert sig.event_date == today


async def test_an_event_date_after_the_source_date_is_refused(ctx) -> None:
    """A source cannot attest an event that had not happened when it was written; the
    same day is fine."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        with pytest.raises(ValueError, match="event_date 2026-03-02 is after source_date"):
            await _record(
                conn, ctx, org_id, source_date=date(2026, 3, 1), event_date=date(2026, 3, 2)
            )
    async with session_for(ctx) as conn:
        rows = (await conn.execute(select(signal_table))).all()
    assert rows == []
    async with session_for(ctx) as conn:
        sig, _action, _stored = await _record(
            conn, ctx, org_id, source_date=date(2026, 3, 1), event_date=date(2026, 3, 1)
        )
    assert sig.event_date == date(2026, 3, 1)


async def test_a_match_fills_event_date_but_not_past_the_stored_source_date(ctx) -> None:
    """A match's fill is checked as the row will stand: a submitted event_date cannot
    land after a source_date already stored, and the refused fill writes nothing."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        first, _action, _stored = await _record(conn, ctx, org_id, source_date=date(2026, 3, 1))
    # The error leaves the session, as it does from the tool, so the transaction rolls back.
    with pytest.raises(ValueError, match="is after source_date"):
        async with session_for(ctx) as conn:
            await _record(conn, ctx, org_id, event_date=date(2026, 3, 9))
    async with session_for(ctx) as conn:
        assert (await store.get_signal(conn, ctx, signal_id=first.id)).event_date is None
    async with session_for(ctx) as conn:
        second, action, _stored = await _record(conn, ctx, org_id, event_date=date(2026, 2, 1))
    assert action == "matched"
    assert second.event_date == date(2026, 2, 1)


async def test_two_facts_from_one_page_are_two_signals_when_typed_differently(ctx) -> None:
    """The key includes the type, so one press release announcing an expansion AND a
    hiring push is two signals, not one overwriting the other."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await _record(conn, ctx, org_id)
        await _record(
            conn,
            ctx,
            org_id,
            signal_type="hiring",
            claim="Northwind is hiring warehouse staff for the new site.",
        )
    async with session_for(ctx) as conn:
        signals, _ = await store.list_signals(conn, ctx, organization_id=org_id)
    assert {s.signal_type for s in signals} == {"expansion", "hiring"}


async def test_a_different_fact_on_the_same_page_and_type_reports_it_was_not_stored(ctx) -> None:
    """The dedupe key assumes one fact per (page, type) and nothing enforces that: a
    release announcing two expansions collides with itself. The second claim is not
    stored — so the caller is handed a signal it did not write, and must be told,
    or it cites a record whose quote does not support its point."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        first, _, first_stored = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        second, action, stored = await _record(
            conn,
            ctx,
            org_id,
            claim="Northwind is entering the Canadian market.",
            evidence_quote="We will begin serving Canadian customers this year.",
            event_date=date(2026, 2, 1),
        )
    assert first_stored is True
    assert (action, second.id) == ("matched", first.id)
    assert stored is False
    # The caller gets the row that exists, unchanged — the discarded claim never
    # partially overwrites the recorded one, and its event date never dates it.
    assert second.claim == first.claim
    assert second.evidence_quote == first.evidence_quote
    assert first.event_date is None
    assert second.event_date is None


async def test_a_matching_claim_with_a_different_quote_also_reports_not_stored(ctx) -> None:
    """The quote is half of what makes the row evidence, so a same-worded claim
    backed by different words is still not the record the caller submitted."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        _, action, stored = await _record(
            conn, ctx, org_id, evidence_quote="A second site is planned for the spring."
        )
    assert (action, stored) == ("matched", False)


async def test_the_same_fact_about_two_accounts_stays_two_signals(ctx) -> None:
    """The key is scoped per organization: one industry article can be evidence about
    more than one company."""
    org_a = await _org(ctx)
    org_b = await _org(ctx, name="Contoso Haulage", domain="contoso.example")
    async with session_for(ctx) as conn:
        a, _, _ = await _record(conn, ctx, org_a)
        b, _, _ = await _record(conn, ctx, org_b)
    assert a.id != b.id


async def test_a_source_url_a_reviewer_cannot_open_is_rejected(ctx) -> None:
    org_id = await _org(ctx)
    with pytest.raises(ValueError, match="source_url"):
        async with session_for(ctx) as conn:
            await _record(conn, ctx, org_id, source_url="their blog")


async def test_a_claim_without_the_words_that_support_it_is_rejected(ctx) -> None:
    """The verbatim quote is what makes the row evidence rather than an assertion
    with a link stapled to it."""
    org_id = await _org(ctx)
    with pytest.raises(ValueError, match="evidence_quote"):
        async with session_for(ctx) as conn:
            await _record(conn, ctx, org_id, evidence_quote="   ")


async def test_a_contact_from_another_organization_is_rejected(ctx) -> None:
    """A signal's optional person must work at the account it is about, or the row
    would assert a relationship that does not exist."""
    org_a = await _org(ctx)
    org_b = await _org(ctx, name="Contoso Haulage", domain="contoso.example")
    async with session_for(ctx) as conn:
        other, _action, _promoted = await store.find_or_create_contact(
            conn, ctx, name="Someone Else", email="someone@contoso.example", organization_id=org_b
        )
    with pytest.raises(ValueError, match="contact_id"):
        async with session_for(ctx) as conn:
            await _record(conn, ctx, org_a, contact_id=other.id)


async def test_withdrawn_signals_are_excluded_from_the_default_listing(ctx) -> None:
    """The default read must not hand back a signal someone retracted — and the row
    is still there, because there is no delete."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _, _ = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        withdrawn = await store.withdraw_signal(
            conn, ctx, signal_id=sig.id, reason="the quote was from a different company"
        )
    assert withdrawn.status == "withdrawn"
    assert withdrawn.withdrawn_at is not None
    assert withdrawn.withdrawn_reason == "the quote was from a different company"

    async with session_for(ctx) as conn:
        default_page, _ = await store.list_signals(conn, ctx, organization_id=org_id)
        opted_in, _ = await store.list_signals(
            conn, ctx, organization_id=org_id, status="withdrawn"
        )
        by_id = await store.get_signal(conn, ctx, signal_id=sig.id)
    assert default_page == []
    assert [s.id for s in opted_in] == [sig.id]
    assert by_id.id == sig.id  # a held id still reads; only the listings hide it


async def test_withdrawing_twice_keeps_the_first_withdrawal(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _, _ = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        first = await store.withdraw_signal(conn, ctx, signal_id=sig.id, reason="misread")
    async with session_for(ctx) as conn:
        again = await store.withdraw_signal(conn, ctx, signal_id=sig.id, reason="second thoughts")
    assert again.withdrawn_at == first.withdrawn_at
    assert again.withdrawn_reason == "misread"


async def test_re_recording_does_not_revive_a_withdrawn_signal(ctx) -> None:
    """Re-finding the source is not grounds to undo the judgment that the fact was
    wrong. There is no verb that resurrects one."""
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _, _ = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=sig.id, reason="misread")
    async with session_for(ctx) as conn:
        again, action, _ = await _record(conn, ctx, org_id)
    assert (action, again.id, again.status) == ("matched", sig.id, "withdrawn")

    async with session_for(ctx) as conn:
        default_page, _ = await store.list_signals(conn, ctx, organization_id=org_id)
    assert default_page == []


async def test_signal_filters_narrow_the_listing(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await _record(conn, ctx, org_id)
        await _record(
            conn,
            ctx,
            org_id,
            signal_type="regulatory",
            source_url="https://news.example/northwind-rule",
        )
    async with session_for(ctx) as conn:
        by_type, _ = await store.list_signals(conn, ctx, signal_type="regulatory")
        other, _ = await store.list_signals(conn, ctx, signal_type="expansion")
    assert [s.signal_type for s in by_type] == ["regulatory"]
    assert [s.signal_type for s in other] == ["expansion"]


async def test_signal_mutations_land_in_the_audit_table(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        sig, _, _ = await _record(conn, ctx, org_id)
    async with session_for(ctx) as conn:
        await store.withdraw_signal(conn, ctx, signal_id=sig.id)
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.action, people_audit.c.outcome).where(
                        people_audit.c.entity == "signal"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert {(r["action"], r["outcome"]) for r in rows} == {("record", "ok"), ("withdraw", "ok")}


async def test_an_absent_signal_id_is_not_found(ctx) -> None:
    with pytest.raises(SignalNotFoundError):
        async with session_for(ctx) as conn:
            await store.get_signal(conn, ctx, signal_id="sg_does_not_exist")


async def test_no_signal_row_survives_a_rejected_write(ctx) -> None:
    """A type the workspace does not define aborts before the insert, so the table
    never holds a row nobody can filter by its type."""
    org_id = await _org(ctx)
    with pytest.raises(ValueError, match="not in this workspace's vocabulary"):
        async with session_for(ctx) as conn:
            await _record(conn, ctx, org_id, signal_type="vibes")
    async with session_for(ctx) as conn:
        count = len((await conn.execute(select(signal_table.c.id))).all())
    assert count == 0


# --------------------------------------------------------------------------- #
# Proof points                                                                 #
# --------------------------------------------------------------------------- #


async def test_a_proof_point_starts_unverified(ctx) -> None:
    """A claim becomes evidence when someone attests it, not when it is written down."""
    async with session_for(ctx) as conn:
        proof, action = await store.record_proof_point(
            conn,
            ctx,
            claim="Brought first-response time under an hour.",
            engagement="a logistics engagement",
            metric="under 1 hour",
            timeframe="six weeks",
        )
    assert action == "created"
    assert proof.id.startswith("pp_")
    assert proof.verification == "unverified"
    assert proof.attested_by is None
    assert proof.attested_at is None


async def test_recording_as_verified_requires_naming_the_attester(ctx) -> None:
    """A verified claim with nobody behind it is the laundering hole this table
    exists to close."""
    with pytest.raises(ValueError, match="attested_by"):
        async with session_for(ctx) as conn:
            await store.record_proof_point(
                conn, ctx, claim="Tripled qualified pipeline.", verification="verified"
            )


async def test_a_verified_create_stamps_the_attestation(ctx) -> None:
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(
            conn,
            ctx,
            claim="Tripled qualified pipeline.",
            verification="verified",
            attested_by="the engagement lead",
            scope_notes="figures may ship; client may not be named",
        )
    assert proof.verification == "verified"
    assert proof.attested_by == "the engagement lead"
    assert proof.attested_at is not None
    assert proof.scope_notes == "figures may ship; client may not be named"


async def test_a_placeholder_is_a_legitimate_recordable_state(ctx) -> None:
    """Copy gets workshopped against outcomes we have not earned. Marking that is
    what stops the number being read as evidence later — and the set of them is the
    punch list of case studies to obtain."""
    async with session_for(ctx) as conn:
        placeholder, _ = await store.record_proof_point(
            conn,
            ctx,
            claim="Cut freight reconciliation from four days to one.",
            verification="fabricated-placeholder",
        )
        await store.record_proof_point(
            conn,
            ctx,
            claim="Halved onboarding time.",
            verification="verified",
            attested_by="the engagement lead",
        )
    assert placeholder.verification == "fabricated-placeholder"

    async with session_for(ctx) as conn:
        punch_list, _ = await store.list_proof_points(
            conn, ctx, verification="fabricated-placeholder"
        )
        everything, _ = await store.list_proof_points(conn, ctx)
    assert [p.id for p in punch_list] == [placeholder.id]
    # Nothing is hidden by default — an unearned claim is what a reader most needs
    # to see, which is the deliberate mirror image of the signal listing.
    assert len(everything) == 2


async def test_verify_moves_a_claim_in_both_directions_and_names_who(ctx) -> None:
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(conn, ctx, claim="Halved onboarding time.")
    async with session_for(ctx) as conn:
        promoted = await store.verify_proof_point(
            conn,
            ctx,
            proof_point_id=proof.id,
            verification="verified",
            attested_by="the engagement lead",
            scope_notes="client may be named",
        )
    assert (promoted.verification, promoted.attested_by) == ("verified", "the engagement lead")
    assert promoted.scope_notes == "client may be named"

    async with session_for(ctx) as conn:
        demoted = await store.verify_proof_point(
            conn,
            ctx,
            proof_point_id=proof.id,
            verification="fabricated-placeholder",
            attested_by="the reviewer",
        )
    assert demoted.verification == "fabricated-placeholder"
    assert demoted.attested_by == "the reviewer"
    assert promoted.attested_at is not None and demoted.attested_at is not None
    assert demoted.attested_at >= promoted.attested_at
    # scope_notes was omitted, so it is left alone — partial-update semantics.
    assert demoted.scope_notes == "client may be named"


async def test_an_attestation_must_name_someone(ctx) -> None:
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(conn, ctx, claim="Halved onboarding time.")
    with pytest.raises(ValueError, match="attested_by"):
        async with session_for(ctx) as conn:
            await store.verify_proof_point(
                conn, ctx, proof_point_id=proof.id, verification="verified", attested_by="  "
            )


async def test_an_unrecognized_verification_is_rejected(ctx) -> None:
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(conn, ctx, claim="Halved onboarding time.")
    with pytest.raises(ValueError, match="verification"):
        async with session_for(ctx) as conn:
            await store.verify_proof_point(
                conn,
                ctx,
                proof_point_id=proof.id,
                verification="probably-true",
                attested_by="the engagement lead",
            )


async def test_a_repeated_idempotency_key_converges_to_one_proof_point(ctx) -> None:
    async with session_for(ctx) as conn:
        first, first_action = await store.record_proof_point(
            conn, ctx, claim="Halved onboarding time.", idempotency_key="import-legacy-7"
        )
    async with session_for(ctx) as conn:
        second, second_action = await store.record_proof_point(
            conn, ctx, claim="Halved onboarding time.", idempotency_key="import-legacy-7"
        )
    assert (first_action, second_action) == ("created", "matched")
    assert second.id == first.id

    async with session_for(ctx) as conn:
        rows, _ = await store.list_proof_points(conn, ctx)
    assert len(rows) == 1


async def test_a_proof_point_needs_no_organization_record(ctx) -> None:
    """Past work predates the CRM, and absence of a record is not absence of the
    fact — the engagement label carries it."""
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(
            conn, ctx, claim="Halved onboarding time.", engagement="a 2024 logistics engagement"
        )
    assert proof.organization_id is None
    assert proof.engagement == "a 2024 logistics engagement"


async def test_proof_points_filter_by_client(ctx) -> None:
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        theirs, _ = await store.record_proof_point(
            conn, ctx, claim="Halved onboarding time.", organization_id=org_id
        )
        await store.record_proof_point(conn, ctx, claim="Something for someone else.")
    async with session_for(ctx) as conn:
        rows, _ = await store.list_proof_points(conn, ctx, organization_id=org_id)
    assert [p.id for p in rows] == [theirs.id]


async def test_proof_point_mutations_land_in_the_audit_table(ctx) -> None:
    async with session_for(ctx) as conn:
        proof, _ = await store.record_proof_point(conn, ctx, claim="Halved onboarding time.")
    async with session_for(ctx) as conn:
        await store.verify_proof_point(
            conn,
            ctx,
            proof_point_id=proof.id,
            verification="verified",
            attested_by="the engagement lead",
        )
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.action, people_audit.c.outcome).where(
                        people_audit.c.entity == "proof_point"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert {(r["action"], r["outcome"]) for r in rows} == {("record", "ok"), ("verify", "ok")}


async def test_an_absent_proof_point_id_is_not_found(ctx) -> None:
    with pytest.raises(ProofPointNotFoundError):
        async with session_for(ctx) as conn:
            await store.get_proof_point(conn, ctx, proof_point_id="pp_does_not_exist")
