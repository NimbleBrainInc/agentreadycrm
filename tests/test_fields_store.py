"""Custom fields in the store.

What these pin: a definition is idempotent on its key and only ever grows; a
value is written key by key, validated against its definition, and keeps who set
it and why; a find-or-create fills where an update overwrites; and the email
status follows the address it grades.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from conftest import make_ctx, session_for

from mcp_people.db import store
from mcp_people.fields import MAX_FIELDS_PER_ENTITY, FieldError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _define(ctx, **kwargs):
    async with session_for(ctx) as conn:
        return await store.define_field(conn, ctx, **kwargs)


async def _fit(ctx):
    return await _define(
        ctx,
        entity="organization",
        key="fit",
        label="Fit",
        field_type="select",
        options=["Strong", "Moderate"],
        description="Rubric v2.",
    )


async def _org(ctx, **kwargs) -> str:
    async with session_for(ctx) as conn:
        org, _action, _promoted = await store.find_or_create_organization(
            conn, ctx, name="Larkfield", domain="larkfield.example", **kwargs
        )
    return org.id


async def _contact(ctx, **kwargs):
    async with session_for(ctx) as conn:
        found, _action, _promoted = await store.find_or_create_contact(conn, ctx, **kwargs)
    return found


# --------------------------------------------------------------------------- #
# Definitions                                                                  #
# --------------------------------------------------------------------------- #


async def test_define_field_is_idempotent_and_only_grows(ctx) -> None:
    field, action, added = await _fit(ctx)
    assert (action, added) == ("created", [])
    assert (field.entity, field.key, field.type) == ("organization", "fit", "select")
    assert field.options == ["Strong", "Moderate"]

    again, action, added = await _define(
        ctx,
        entity="organization",
        key="fit",
        label="A different label",
        field_type="select",
        options=["strong", "No"],
        description="A different description.",
    )
    assert (action, added) == ("matched", ["No"])
    assert again.options == ["Strong", "Moderate", "No"]
    # A match never rewrites what rows and readers already rely on.
    assert again.label == "Fit"
    assert again.description == "Rubric v2."

    async with session_for(ctx) as conn:
        described = await store.describe_fields(conn, ctx)
    assert [f.key for f in described["organization"]] == ["fit"]
    assert described["contact"] == []


async def test_a_match_fills_an_empty_description(ctx) -> None:
    await _define(ctx, entity="contact", key="audience", label="Audience", field_type="text")
    again, action, _added = await _define(
        ctx,
        entity="contact",
        key="audience",
        label="Audience",
        field_type="text",
        description="The message angle.",
    )
    assert action == "matched"
    assert again.description == "The message angle."


async def test_a_fields_type_cannot_change(ctx) -> None:
    await _fit(ctx)
    with pytest.raises(ValueError, match="already defined as select"):
        await _define(ctx, entity="organization", key="fit", label="Fit", field_type="text")


async def test_the_same_key_is_a_different_field_on_each_entity(ctx) -> None:
    await _define(ctx, entity="contact", key="tier", label="Tier", field_type="number")
    _field, action, _added = await _define(
        ctx, entity="organization", key="tier", label="Tier", field_type="text"
    )
    assert action == "created"


async def test_concurrent_defines_of_one_key_converge(ctx) -> None:
    results = await asyncio.gather(*(_fit(ctx) for _ in range(4)))
    assert sorted(action for _f, action, _a in results) == [
        "created",
        "matched",
        "matched",
        "matched",
    ]
    async with session_for(ctx) as conn:
        described = await store.describe_fields(conn, ctx, entity="organization")
    assert len(described["organization"]) == 1


async def test_a_workspace_holds_a_bounded_number_of_fields(ctx) -> None:
    async with session_for(ctx) as conn:
        for n in range(MAX_FIELDS_PER_ENTITY):
            await store.define_field(
                conn, ctx, entity="contact", key=f"f{n}", label=f"F{n}", field_type="text"
            )
        with pytest.raises(ValueError, match="the maximum"):
            await store.define_field(
                conn, ctx, entity="contact", key="one_more", label="One more", field_type="text"
            )
        # Re-defining an existing key is not a new field, so the cap does not bar it.
        _f, action, _a = await store.define_field(
            conn, ctx, entity="contact", key="f0", label="F0", field_type="text"
        )
        assert action == "matched"


# --------------------------------------------------------------------------- #
# Retirement                                                                   #
# --------------------------------------------------------------------------- #


async def _update_field(ctx, **kwargs):
    async with session_for(ctx) as conn:
        return await store.update_field(conn, ctx, **kwargs)


async def _fit_grade(ctx):
    return await _define(
        ctx,
        entity="organization",
        key="fit_grade",
        label="Fit grade",
        field_type="select",
        options=["A", "B", "C"],
    )


async def _described(ctx, **kwargs) -> dict[str, list[str]]:
    async with session_for(ctx) as conn:
        described = await store.describe_fields(conn, ctx, **kwargs)
    return {entity: [f.key for f in defs] for entity, defs in described.items()}


async def test_retire_and_restore_round_trip(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)

    retired, action = await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    assert action == "retired"
    assert retired.retired_at is not None
    assert retired.replaced_by == "fit_grade"
    assert (await _described(ctx))["organization"] == ["fit_grade"]

    restored, action = await _update_field(ctx, entity="organization", key="fit", retired=False)
    assert action == "restored"
    assert (restored.retired_at, restored.replaced_by) == (None, None)
    assert (await _described(ctx))["organization"] == ["fit", "fit_grade"]


async def test_retiring_again_keeps_the_first_stamp(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    first, _ = await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    again, action = await _update_field(ctx, entity="organization", key="fit", retired=True)
    assert action == "unchanged"
    assert again.retired_at == first.retired_at
    # Retiring again without naming a replacement keeps the one it has.
    assert again.replaced_by == "fit_grade"


async def test_a_retired_fields_replacement_can_change(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    await _define(ctx, entity="organization", key="tier", label="Tier", field_type="text")
    first, _ = await _update_field(ctx, entity="organization", key="fit", retired=True)
    assert first.replaced_by is None
    moved, action = await _update_field(ctx, entity="organization", key="fit", replaced_by="tier")
    assert action == "retired"
    assert moved.replaced_by == "tier"
    assert moved.retired_at == first.retired_at


async def test_a_replacement_may_live_on_another_entity(ctx) -> None:
    await _fit_grade(ctx)
    await _define(ctx, entity="contact", key="fit", label="Fit", field_type="text")
    retired, _ = await _update_field(
        ctx, entity="contact", key="fit", retired=True, replaced_by="organization.fit_grade"
    )
    assert retired.replaced_by == "organization.fit_grade"
    async with session_for(ctx) as conn:
        found = await store.live_replacement(conn, "contact", "fit")
    assert found.live == ("organization", "fit_grade")


@pytest.mark.parametrize(
    ("replaced_by", "match"),
    [
        ("fit", "cannot replace itself"),
        ("organization.fit", "cannot replace itself"),
        ("undefined_key", "is not defined"),
        ("contact.fit_grade", "is not defined"),
        ("opportunity.fit_grade", "names no custom field"),
        ("Fit Grade", "names no custom field"),
        ("organization.", "names no custom field"),
        ("name", "names no custom field"),
        ("retired_one", "is retired"),
    ],
)
async def test_a_replacement_must_be_another_live_field(ctx, replaced_by, match) -> None:
    await _fit(ctx)
    await _define(ctx, entity="organization", key="retired_one", label="Old", field_type="text")
    await _update_field(ctx, entity="organization", key="retired_one", retired=True)
    with pytest.raises(FieldError, match=match) as refused:
        await _update_field(
            ctx, entity="organization", key="fit", retired=True, replaced_by=replaced_by
        )
    assert refused.value.next_step
    # A refused call retires nothing.
    assert "fit" in (await _described(ctx))["organization"]


async def test_only_a_retired_field_has_a_replacement(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    with pytest.raises(FieldError, match="only to a retired field") as refused:
        await _update_field(ctx, entity="organization", key="fit", replaced_by="fit_grade")
    assert "retired=true" in refused.value.next_step
    with pytest.raises(FieldError, match="only to a retired field"):
        await _update_field(
            ctx, entity="organization", key="fit", retired=False, replaced_by="fit_grade"
        )


async def test_updating_an_undefined_field_names_describe_fields(ctx) -> None:
    with pytest.raises(FieldError, match="no organization field named 'fit'") as refused:
        await _update_field(ctx, entity="organization", key="fit", retired=True)
    assert "list_fields" in refused.value.next_step


async def test_update_field_is_audited_by_key_name(ctx) -> None:
    from sqlalchemy import select

    from mcp_people.db.tables import people_audit

    fit, _action, _added = await _fit(ctx)
    await _fit_grade(ctx)
    await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    async with session_for(ctx) as conn:
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.entity, people_audit.c.detail).where(
                        people_audit.c.action == "update",
                        people_audit.c.entity == "field_def",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [r["detail"] for r in rows] == [
        {
            "entity": "organization",
            "key": "fit",
            "retired": True,
            "replaced_by": "fit_grade",
            "default_column": False,
            "options_removed": 0,
            "minimum": None,
            "maximum": None,
            "changed": True,
        }
    ]


async def test_defining_a_retired_key_is_refused_and_changes_nothing(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    retired, _ = await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    with pytest.raises(FieldError, match="'fit' is retired") as refused:
        await _define(
            ctx,
            entity="organization",
            key="fit",
            label="Fit",
            field_type="select",
            options=["Strong", "Weak"],
        )
    assert "'fit_grade'" in refused.value.next_step
    assert "update_field(entity='organization', key='fit', retired=false)" in (
        refused.value.next_step
    )
    async with session_for(ctx) as conn:
        described = await store.describe_fields(conn, ctx, include_retired=True)
    (fit,) = [f for f in described["organization"] if f.key == "fit"]
    assert fit.options == ["Strong", "Moderate"]
    assert fit.retired_at == retired.retired_at


async def test_a_refusal_follows_the_replacement_chain(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    await _define(ctx, entity="organization", key="tier", label="Tier", field_type="text")
    await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    await _update_field(
        ctx, entity="organization", key="fit_grade", retired=True, replaced_by="tier"
    )
    async with session_for(ctx) as conn:
        assert (await store.live_replacement(conn, "organization", "fit")).live == (
            "organization",
            "tier",
        )
    with pytest.raises(FieldError) as refused:
        await _fit(ctx)
    assert "'tier'" in refused.value.next_step

    # Retiring the end of the chain leaves no live replacement, and the refusal says so.
    await _update_field(ctx, entity="organization", key="tier", retired=True)
    async with session_for(ctx) as conn:
        found = await store.live_replacement(conn, "organization", "fit")
    assert (found.live, found.retired) == (None, ("organization", "tier"))
    with pytest.raises(FieldError) as refused:
        await _fit(ctx)
    assert "'tier', which is retired too" in refused.value.next_step


async def test_a_retired_field_with_no_replacement_says_so(ctx) -> None:
    await _fit(ctx)
    await _update_field(ctx, entity="organization", key="fit", retired=True)
    async with session_for(ctx) as conn:
        found = await store.live_replacement(conn, "organization", "fit")
    assert (found.live, found.retired) == (None, None)
    with pytest.raises(FieldError) as refused:
        await _fit(ctx)
    assert "names no replacement" in refused.value.next_step


async def test_describe_fields_shows_retired_fields_only_when_asked(ctx) -> None:
    await _fit(ctx)
    await _fit_grade(ctx)
    await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    assert (await _described(ctx, entity="organization")) == {"organization": ["fit_grade"]}
    async with session_for(ctx) as conn:
        described = await store.describe_fields(
            conn, ctx, entity="organization", include_retired=True
        )
    by_key = {f.key: f for f in described["organization"]}
    assert set(by_key) == {"fit", "fit_grade"}
    assert by_key["fit"].replaced_by == "fit_grade"
    assert by_key["fit"].retired_at is not None
    assert by_key["fit_grade"].retired_at is None


async def test_retiring_a_field_frees_a_slot_and_restoring_needs_one(ctx) -> None:
    async with session_for(ctx) as conn:
        for n in range(MAX_FIELDS_PER_ENTITY):
            await store.define_field(
                conn, ctx, entity="contact", key=f"f{n}", label=f"F{n}", field_type="text"
            )
        await store.update_field(conn, ctx, entity="contact", key="f0", retired=True)
        _f, action, _a = await store.define_field(
            conn, ctx, entity="contact", key="one_more", label="One more", field_type="text"
        )
        assert action == "created"
        # The workspace is full of live fields again, so the retired one cannot return.
        with pytest.raises(ValueError, match="the maximum"):
            await store.update_field(conn, ctx, entity="contact", key="f0", retired=False)


# --------------------------------------------------------------------------- #
# Values                                                                       #
# --------------------------------------------------------------------------- #


async def test_update_writes_key_by_key_and_keeps_provenance(ctx) -> None:
    await _fit(ctx)
    await _define(
        ctx, entity="organization", key="headcount", label="Headcount", field_type="number"
    )
    org_id = await _org(ctx)

    async with session_for(ctx) as conn:
        org = await store.update_organization(
            conn,
            ctx,
            org_id=org_id,
            fields={"fit": {"value": "strong", "why": "B2B, large market."}, "headcount": 27},
            fields_by="rubric-v2",
        )
    # A listing-shaped read carries flat values and no provenance.
    assert org.fields == {"fit": "Strong", "headcount": 27}
    assert org.field_details is None

    async with session_for(ctx) as conn:
        # Writing one key leaves the other alone.
        await store.update_organization(
            conn, ctx, org_id=org_id, fields={"headcount": {"value": 31, "by": "a person"}}
        )
        got, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert got.fields == {"fit": "Strong", "headcount": 31}
    assert got.field_details is not None
    assert got.field_details["fit"]["by"] == "rubric-v2"
    assert got.field_details["fit"]["why"] == "B2B, large market."
    assert got.field_details["headcount"]["by"] == "a person"
    assert "why" not in got.field_details["headcount"]
    assert datetime.fromisoformat(got.field_details["fit"]["at"]).tzinfo is not None


async def test_a_null_value_removes_the_key(ctx) -> None:
    await _fit(ctx)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"fit": "strong"})
        org = await store.update_organization(conn, ctx, org_id=org_id, fields={"fit": None})
    assert org.fields is None


async def test_an_undefined_key_is_refused_and_writes_nothing(ctx) -> None:
    await _fit(ctx)
    org_id = await _org(ctx)
    with pytest.raises(FieldError, match="no organization field named 'fitt'") as caught:
        async with session_for(ctx) as conn:
            await store.update_organization(
                conn, ctx, org_id=org_id, region="EMEA", fields={"fit": "strong", "fitt": "x"}
            )
    assert "define_field(entity='organization'" in caught.value.next_step
    async with session_for(ctx) as conn:
        org, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    # The whole write rolled back: neither the valid key nor the core field landed.
    assert org.fields is None
    assert org.region is None


async def test_a_value_is_validated_against_its_definition(ctx) -> None:
    await _fit(ctx)
    org_id = await _org(ctx)
    with pytest.raises(FieldError, match="must be one of: Strong, Moderate"):
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=org_id, fields={"fit": "great"})


async def test_a_contact_field_is_not_an_organization_field(ctx) -> None:
    """A key defined on organizations cannot be written to a contact: the company's
    fit lives on the company, once."""
    await _fit(ctx)
    contact = await _contact(ctx, name="Dana", email="dana@larkfield.example")
    with pytest.raises(FieldError, match="no contact field named 'fit'"):
        async with session_for(ctx) as conn:
            await store.update_contact(conn, ctx, contact_id=contact.id, fields={"fit": "strong"})


async def test_concurrent_writes_to_different_keys_both_land(ctx) -> None:
    """The merge runs in SQL, so two writers on one row do not overwrite each other."""
    for key in ("a", "b"):
        await _define(ctx, entity="organization", key=key, label=key, field_type="number")
    org_id = await _org(ctx)

    async def write(key: str) -> None:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=org_id, fields={key: 1})

    await asyncio.gather(write("a"), write("b"))
    async with session_for(ctx) as conn:
        org, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert org.fields == {"a": 1, "b": 1}


async def test_find_or_create_fills_and_does_not_overwrite(ctx) -> None:
    await _fit(ctx)
    await _define(
        ctx, entity="organization", key="headcount", label="Headcount", field_type="number"
    )
    org_id = await _org(ctx, fields={"fit": "strong"}, fields_by="import")
    # A second call matches: it fills the key the row lacks and leaves the one it holds.
    assert await _org(ctx, fields={"fit": "moderate", "headcount": 27}) == org_id
    async with session_for(ctx) as conn:
        org, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert org.fields == {"fit": "Strong", "headcount": 27}
    assert org.field_details is not None
    assert org.field_details["fit"]["by"] == "import"


async def test_find_or_create_cannot_clear_a_field(ctx) -> None:
    await _fit(ctx)
    with pytest.raises(ValueError, match="cannot clear 'fit'"):
        await _org(ctx, fields={"fit": None})


async def _retired_fit(ctx) -> str:
    """An organization holding a ``fit`` value, with ``fit`` then retired for ``fit_grade``."""
    await _fit(ctx)
    await _fit_grade(ctx)
    org_id = await _org(ctx, fields={"fit": "Strong"})
    await _update_field(
        ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    return org_id


async def test_a_value_for_a_retired_field_is_refused_with_its_replacement(ctx) -> None:
    org_id = await _retired_fit(ctx)
    with pytest.raises(FieldError, match="organization field 'fit' is retired") as caught:
        async with session_for(ctx) as conn:
            await store.update_organization(
                conn, ctx, org_id=org_id, fields={"fit": "Moderate", "fit_grade": "A"}
            )
    assert "Use 'fit_grade' instead" in caught.value.next_step
    async with session_for(ctx) as conn:
        org, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    # The whole write rolled back: the stale value stays and the live key was not set.
    assert org.fields == {"fit": "Strong"}


async def test_a_null_clears_a_retired_fields_value(ctx) -> None:
    org_id = await _retired_fit(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org_id, fields={"fit": None, "fit_grade": "A"}
        )
        org, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert org.fields == {"fit_grade": "A"}
    assert org.field_details is not None
    assert "fit" not in org.field_details


async def test_find_or_create_refuses_a_retired_field(ctx) -> None:
    await _retired_fit(ctx)
    with pytest.raises(FieldError, match="organization field 'fit' is retired") as caught:
        await _org(ctx, fields={"fit": "Moderate"})
    assert "Use 'fit_grade' instead" in caught.value.next_step


async def test_an_import_skips_a_row_writing_a_retired_field(ctx) -> None:
    await _retired_fit(ctx)
    async with session_for(ctx) as conn:
        result = await store.import_organizations(
            conn,
            ctx,
            rows=[
                {"name": "Stale", "domain": "stale.example", "fields": {"fit": "Strong"}},
                {"name": "Live", "domain": "live.example", "fields": {"fit_grade": "B"}},
            ],
        )
    assert result["created"] == 1
    (group,) = result["skipped"]
    assert group["rows"] == [0]
    assert "'fit' is retired" in group["reason"]


async def test_a_retired_contact_field_names_its_replacement_on_another_entity(ctx) -> None:
    await _fit_grade(ctx)
    await _define(ctx, entity="contact", key="fit", label="Fit", field_type="text")
    found = await _contact(ctx, name="Dana", email="dana@larkfield.example")
    await _update_field(
        ctx, entity="contact", key="fit", retired=True, replaced_by="organization.fit_grade"
    )
    with pytest.raises(FieldError, match="contact field 'fit' is retired") as caught:
        async with session_for(ctx) as conn:
            await store.update_contact(conn, ctx, contact_id=found.id, fields={"fit": "Strong"})
    # A contact write cannot set an organization field, so the refusal names the
    # update that can.
    assert (
        "update_organization(organization_id=..., fields={'fit_grade': ...})"
        in caught.value.next_step
    )


async def test_a_field_write_is_audited_by_key_name_only(ctx) -> None:
    from sqlalchemy import select

    from mcp_people.db.tables import people_audit

    await _fit(ctx)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(
            conn, ctx, org_id=org_id, fields={"fit": {"value": "strong", "why": "a secret"}}
        )
        rows = (
            (
                await conn.execute(
                    select(people_audit.c.action, people_audit.c.detail).where(
                        people_audit.c.entity_id == org_id, people_audit.c.action == "update"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [r["detail"] for r in rows] == [{"fields": ["fit"]}]


# --------------------------------------------------------------------------- #
# Reads of a retired field                                                     #
# --------------------------------------------------------------------------- #


async def _retire_fit_on(ctx, entity: str) -> None:
    """Retire ``entity``'s ``fit`` for ``fit_grade``, after its records hold both."""
    await _update_field(ctx, entity=entity, key="fit", retired=True, replaced_by="fit_grade")


async def _define_fit_pair(ctx, entity: str) -> None:
    for key, options in (("fit", ["Strong", "Moderate"]), ("fit_grade", ["A", "B", "C"])):
        await _define(ctx, entity=entity, key=key, label=key, field_type="select", options=options)


_BOTH = {"fit": "Strong", "fit_grade": "A"}


def _assert_marked(record) -> None:
    """A by-id read: both values kept, ``fit`` marked retired, ``fit_grade`` not."""
    assert record.fields == _BOTH
    details = record.field_details
    assert details is not None
    assert isinstance(details["fit"]["retired_at"], datetime)
    assert details["fit"]["replaced_by"] == "fit_grade"
    # The mark sits beside the value's provenance, not in place of it.
    assert "at" in details["fit"]
    assert "retired_at" not in details["fit_grade"]
    assert "replaced_by" not in details["fit_grade"]


async def test_a_contact_listing_drops_a_retired_field_a_get_marks_it(ctx) -> None:
    await _define_fit_pair(ctx, "contact")
    org_id = await _org(ctx)
    found = await _contact(
        ctx, name="Dana", email="dana@larkfield.example", organization_id=org_id, fields=_BOTH
    )
    await _contact(ctx, name="Eli", email="eli@larkfield.example", fields={"fit": "Moderate"})
    await _retire_fit_on(ctx, "contact")

    async with session_for(ctx) as conn:
        listed, _cursor = await store.list_contacts(conn, ctx)
        filtered = await store.page_contacts(
            conn, ctx, filter={"field": "fit_grade", "op": "eq", "value": "A"}
        )
        roster, _ = await store.list_contacts(conn, ctx, organization_id=org_id)
        got, _counts = await store.get_contact(conn, ctx, contact_id=found.id)

    by_name = {c.name: c.fields for c in listed}
    assert by_name == {"Dana": {"fit_grade": "A"}, "Eli": None}
    assert [c.fields for c in filtered.rows] == [{"fit_grade": "A"}]
    assert [c.fields for c in roster] == [{"fit_grade": "A"}]
    _assert_marked(got)


async def test_an_organization_listing_drops_a_retired_field_a_get_marks_it(ctx) -> None:
    await _define_fit_pair(ctx, "organization")
    org_id = await _org(ctx, fields=_BOTH)
    await _retire_fit_on(ctx, "organization")

    async with session_for(ctx) as conn:
        listed, _cursor = await store.list_organizations(conn, ctx)
        got, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert [o.fields for o in listed] == [{"fit_grade": "A"}]
    _assert_marked(got)


async def test_an_opportunity_listing_drops_a_retired_field_a_get_marks_it(ctx) -> None:
    await _define_fit_pair(ctx, "opportunity")
    async with session_for(ctx) as conn:
        deal, _action = await store.find_or_create_opportunity(
            conn, ctx, title="Larkfield pilot", fields=_BOTH
        )
    await _retire_fit_on(ctx, "opportunity")

    async with session_for(ctx) as conn:
        listed, _cursor = await store.list_opportunities(conn, ctx)
        got, _counts = await store.get_opportunity(conn, ctx, opportunity_id=deal.id)
    assert [o.fields for o in listed] == [{"fit_grade": "A"}]
    _assert_marked(got)


async def test_a_write_result_drops_a_retired_field(ctx) -> None:
    # A record returned without field_details cannot mark a retired value, so it
    # leaves it out: setting the replacement never echoes the stale value beside it.
    for entity in ("contact", "organization", "opportunity"):
        await _define_fit_pair(ctx, entity)
    org_id = await _org(ctx, fields=_BOTH)
    found = await _contact(ctx, name="Dana", email="dana@larkfield.example", fields=_BOTH)
    async with session_for(ctx) as conn:
        deal, _action = await store.find_or_create_opportunity(
            conn, ctx, title="Larkfield pilot", fields=_BOTH
        )
    for entity in ("contact", "organization", "opportunity"):
        await _retire_fit_on(ctx, entity)

    swap = {"fit_grade": "B"}
    async with session_for(ctx) as conn:
        contact = await store.update_contact(conn, ctx, contact_id=found.id, fields=swap)
        org = await store.update_organization(conn, ctx, org_id=org_id, fields=swap)
        opp = await store.update_opportunity(conn, ctx, opportunity_id=deal.id, fields=swap)

    for record in (contact, org, opp):
        assert record.fields == swap


async def test_query_rows_drop_a_retired_field(ctx) -> None:
    await _define_fit_pair(ctx, "contact")
    await _define_fit_pair(ctx, "organization")
    await _org(ctx, fields=_BOTH)
    await _contact(ctx, name="Larkfield Dana", email="dana@larkfield.example", fields=_BOTH)
    await _retire_fit_on(ctx, "contact")
    await _retire_fit_on(ctx, "organization")

    async with session_for(ctx) as conn:
        contacts = await store.page_contacts(conn, ctx, terms="Larkfield")
        orgs = await store.page_organizations(conn, ctx, terms="Larkfield")
    assert [r.fields for r in contacts.rows + orgs.rows] == [{"fit_grade": "A"}, {"fit_grade": "A"}]


async def test_retiring_on_one_entity_leaves_the_same_key_elsewhere_listed(ctx) -> None:
    await _define_fit_pair(ctx, "contact")
    await _define_fit_pair(ctx, "organization")
    await _org(ctx, fields=_BOTH)
    await _contact(ctx, name="Dana", email="dana@larkfield.example", fields=_BOTH)
    await _retire_fit_on(ctx, "organization")

    async with session_for(ctx) as conn:
        orgs, _cursor = await store.list_organizations(conn, ctx)
        contacts, _cursor = await store.list_contacts(conn, ctx)
    assert [o.fields for o in orgs] == [{"fit_grade": "A"}]
    assert [c.fields for c in contacts] == [_BOTH]


async def test_a_restored_field_is_listed_and_unmarked_again(ctx) -> None:
    await _define_fit_pair(ctx, "organization")
    org_id = await _org(ctx, fields=_BOTH)
    await _retire_fit_on(ctx, "organization")
    await _update_field(ctx, entity="organization", key="fit", retired=False)

    async with session_for(ctx) as conn:
        listed, _cursor = await store.list_organizations(conn, ctx)
        got, _counts = await store.get_organization(conn, ctx, org_id=org_id)
    assert [o.fields for o in listed] == [_BOTH]
    assert got.field_details is not None
    assert "retired_at" not in got.field_details["fit"]


# --------------------------------------------------------------------------- #
# Email status                                                                 #
# --------------------------------------------------------------------------- #


async def test_email_status_is_stamped_and_follows_the_address(ctx) -> None:
    contact = await _contact(ctx, name="Dana", email="dana@larkfield.example")
    assert contact.email_status is None

    checked = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    async with session_for(ctx) as conn:
        graded = await store.update_contact(
            conn, ctx, contact_id=contact.id, email_status="Valid", email_checked_at=checked
        )
        assert (graded.email_status, graded.email_checked_at) == ("valid", checked)

        # Re-sending the same address is not a change, so the status stays.
        same = await store.update_contact(
            conn, ctx, contact_id=contact.id, email="DANA@larkfield.example"
        )
        assert same.email_status == "valid"

        # A new address has not been checked.
        moved = await store.update_contact(
            conn, ctx, contact_id=contact.id, email="dana@elsewhere.example"
        )
        assert (moved.email_status, moved.email_checked_at) == (None, None)

        # Unless the same call grades it.
        regraded = await store.update_contact(
            conn, ctx, contact_id=contact.id, email="d@third.example", email_status="risky"
        )
        assert regraded.email_status == "risky"
        assert regraded.email_checked_at is not None


async def test_email_status_needs_an_address_and_a_known_value(ctx) -> None:
    nobody = await _contact(ctx, name="No Email")
    with pytest.raises(ValueError, match="this contact has none"):
        async with session_for(ctx) as conn:
            await store.update_contact(conn, ctx, contact_id=nobody.id, email_status="valid")
    # find_or_create grades after it creates, so the refusal rolls the new row back.
    with pytest.raises(ValueError, match="this contact has none"):
        await _contact(ctx, name="Ungraded", email_status="valid")
    async with session_for(ctx) as conn:
        _found, action, _promoted = await store.find_or_create_contact(conn, ctx, name="Ungraded")
    assert action == "created"
    with pytest.raises(ValueError, match="email_status must be one of"):
        await _contact(ctx, name="Dana", email="dana@larkfield.example", email_status="fine")
    with pytest.raises(ValueError, match="together with email_status"):
        async with session_for(ctx) as conn:
            await store.update_contact(
                conn, ctx, contact_id=nobody.id, email_checked_at=datetime.now(UTC)
            )


async def test_find_or_create_fills_email_status_and_contact_fields(ctx) -> None:
    await _define(
        ctx,
        entity="contact",
        key="audience",
        label="Audience",
        field_type="select",
        options=["sales leaders", "B2B founders"],
    )
    created = await _contact(
        ctx,
        name="Dana",
        email="dana@larkfield.example",
        email_status="valid",
        fields={"audience": "Sales Leaders"},
        fields_by="import",
    )
    assert created.email_status == "valid"
    assert created.fields == {"audience": "sales leaders"}

    # A match keeps the status and the value the contact already has.
    matched = await _contact(
        ctx,
        email="dana@larkfield.example",
        email_status="invalid",
        fields={"audience": "B2B founders"},
    )
    assert matched.id == created.id
    assert matched.email_status == "valid"
    assert matched.fields == {"audience": "sales leaders"}


# --------------------------------------------------------------------------- #
# Merges                                                                       #
# --------------------------------------------------------------------------- #


async def test_merge_contacts_carries_fields_and_the_adopted_emails_status(ctx) -> None:
    for key in ("audience", "prior_sends"):
        await _define(ctx, entity="contact", key=key, label=key, field_type="text")
    target = await _contact(ctx, name="Dana Whitlock", fields={"audience": "kept"})
    source = await _contact(
        ctx,
        name="D. Whitlock",
        email="dana@larkfield.example",
        email_status="valid",
        fields={"audience": "dropped", "prior_sends": "6"},
    )
    async with session_for(ctx) as conn:
        merged, summary = await store.merge_contacts(
            conn, ctx, target_id=target.id, source_id=source.id
        )
        archived, _counts = await store.get_contact(conn, ctx, contact_id=source.id)
    assert merged.fields == {"audience": "kept", "prior_sends": "6"}
    assert merged.email == "dana@larkfield.example"
    assert merged.email_status == "valid"
    assert "fields.prior_sends" in summary["fields_filled"]
    assert "fields.audience" not in summary["fields_filled"]
    # The archived source holds no address, so it holds no grade for one.
    assert (archived.email, archived.email_status) == (None, None)


async def test_merge_organizations_carries_fields_the_target_lacks(ctx) -> None:
    await _fit(ctx)
    await _define(
        ctx, entity="organization", key="headcount", label="Headcount", field_type="number"
    )
    async with session_for(ctx) as conn:
        target, _a, _p = await store.find_or_create_organization(
            conn, ctx, name="Larkfield", domain="larkfield.example", fields={"fit": "strong"}
        )
        source, _a, _p = await store.find_or_create_organization(
            conn,
            ctx,
            name="Larkfield Inc",
            domain="larkfield-inc.example",
            fields={"fit": "moderate", "headcount": 27},
        )
        merged, summary = await store.merge_organizations(
            conn, ctx, target_id=target.id, source_id=source.id
        )
    assert merged.fields == {"fit": "Strong", "headcount": 27}
    assert "fields.headcount" in summary["fields_filled"]


# --------------------------------------------------------------------------- #
# Descriptions and options                                                     #
# --------------------------------------------------------------------------- #


async def test_update_field_replaces_and_clears_the_description(ctx) -> None:
    await _fit(ctx)
    field, action = await _update_field(
        ctx, entity="organization", key="fit", description="  Rubric v3. "
    )
    assert (action, field.description) == ("updated", "Rubric v3.")
    _same, again = await _update_field(
        ctx, entity="organization", key="fit", description="Rubric v3."
    )
    assert again == "unchanged"
    cleared, _action = await _update_field(ctx, entity="organization", key="fit", description="")
    assert cleared.description is None


async def test_remove_options_drops_an_option_no_record_holds(ctx) -> None:
    await _define(
        ctx,
        entity="organization",
        key="score",
        label="Score",
        field_type="select",
        options=["5", "4", "1-2", "3"],
    )
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"score": "4"})

    field, action = await _update_field(
        ctx, entity="organization", key="score", remove_options=["1-2"]
    )
    assert (action, field.options) == ("updated", ["5", "4", "3"])

    with pytest.raises(FieldError, match=r"1 organization record\(s\) still hold"):
        await _update_field(ctx, entity="organization", key="score", remove_options=["4"])
    with pytest.raises(FieldError, match="has no option '9'"):
        await _update_field(ctx, entity="organization", key="score", remove_options=["9"])
    with pytest.raises(FieldError, match="with no options"):
        await _update_field(ctx, entity="organization", key="score", remove_options=["5", "4", "3"])


async def test_remove_options_matches_case_and_counts_a_multi_select_value(ctx) -> None:
    await _define(
        ctx,
        entity="organization",
        key="tags",
        label="Tags",
        field_type="multi_select",
        options=["Alpha", "Beta", "Gamma"],
    )
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"tags": ["Beta"]})

    with pytest.raises(FieldError, match="still hold"):
        await _update_field(ctx, entity="organization", key="tags", remove_options=["beta"])
    field, _action = await _update_field(
        ctx, entity="organization", key="tags", remove_options=["gamma"]
    )
    assert field.options == ["Alpha", "Beta"]


async def test_remove_options_counts_a_retired_fields_values(ctx) -> None:
    await _fit(ctx)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"fit": "moderate"})
    await _update_field(ctx, entity="organization", key="fit", retired=True)
    with pytest.raises(FieldError, match="still hold"):
        await _update_field(ctx, entity="organization", key="fit", remove_options=["Moderate"])


async def test_remove_options_refuses_a_field_without_options(ctx) -> None:
    await _define(
        ctx, entity="organization", key="headcount", label="Headcount", field_type="number"
    )
    with pytest.raises(FieldError, match="has no options"):
        await _update_field(ctx, entity="organization", key="headcount", remove_options=["1"])


async def test_remove_options_waits_for_a_write_in_flight_and_sees_its_value(ctx) -> None:
    await _fit(ctx)
    org_id = await _org(ctx)
    written = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=org_id, fields={"fit": "moderate"})
            written.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await written.wait()
    remover = asyncio.create_task(
        _update_field(ctx, entity="organization", key="fit", remove_options=["Moderate"])
    )
    await asyncio.sleep(0.3)
    assert not remover.done(), "removal must wait for the write holding the definition"
    release.set()
    await task
    with pytest.raises(FieldError, match="still hold"):
        await remover


async def _held_removal(ctx, option: str):
    """A removal of ``option`` from ``tags`` that holds its transaction open until released."""
    removed = asyncio.Event()
    release = asyncio.Event()

    async def remover() -> None:
        async with session_for(ctx) as conn:
            await store.update_field(
                conn, ctx, entity="organization", key="tags", remove_options=[option]
            )
            removed.set()
            await release.wait()

    task = asyncio.create_task(remover())
    await removed.wait()
    return task, release


async def _tags(ctx) -> None:
    await _define(
        ctx,
        entity="organization",
        key="tags",
        label="Tags",
        field_type="multi_select",
        options=["A", "B", "C"],
    )


async def test_concurrent_option_removals_both_stand(ctx) -> None:
    await _tags(ctx)
    first, release = await _held_removal(ctx, "A")
    second = asyncio.create_task(
        _update_field(ctx, entity="organization", key="tags", remove_options=["B"])
    )
    await asyncio.sleep(0.3)
    assert not second.done(), "a second removal must wait for the first"
    release.set()
    await first
    field, _action = await second
    assert field.options == ["C"]


async def test_an_option_added_during_a_removal_keeps_the_removal(ctx) -> None:
    await _tags(ctx)
    remover, release = await _held_removal(ctx, "A")
    adder = asyncio.create_task(
        _define(
            ctx,
            entity="organization",
            key="tags",
            label="Tags",
            field_type="multi_select",
            options=["D"],
        )
    )
    await asyncio.sleep(0.3)
    assert not adder.done(), "an option added must wait for the removal"
    release.set()
    await remover
    field, _action, added = await adder
    assert (field.options, added) == (["B", "C", "D"], ["D"])


# --------------------------------------------------------------------------- #
# Number ranges                                                                #
# --------------------------------------------------------------------------- #


async def _score(ctx, **bounds):
    return await _define(
        ctx, entity="organization", key="score", label="Score", field_type="number", **bounds
    )


async def test_a_number_field_refuses_a_write_outside_its_range(ctx) -> None:
    field, _action, _added = await _score(ctx, minimum=1, maximum=5)
    assert (field.minimum, field.maximum) == (1, 5)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        org = await store.update_organization(conn, ctx, org_id=org_id, fields={"score": "4"})
    assert org.fields == {"score": 4}
    for bad in (0, 7, "5.5", 5.0001):
        with pytest.raises(ValueError, match="takes a number from 1 to 5"):
            async with session_for(ctx) as conn:
                await store.update_organization(conn, ctx, org_id=org_id, fields={"score": bad})
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"score": 5})


async def test_a_range_is_for_numbers_and_must_be_ordered(ctx) -> None:
    with pytest.raises(ValueError, match="only a number does"):
        await _define(
            ctx, entity="organization", key="tier", label="Tier", field_type="text", minimum=1
        )
    with pytest.raises(ValueError, match="above maximum"):
        await _score(ctx, minimum=5, maximum=1)
    with pytest.raises(ValueError, match="finite"):
        await _score(ctx, maximum=float("inf"))


async def test_define_field_never_changes_a_range(ctx) -> None:
    await _score(ctx, minimum=1, maximum=5)
    field, action, _added = await _score(ctx, minimum=0, maximum=10)
    assert (action, field.minimum, field.maximum) == ("matched", 1, 5)


async def test_update_field_changes_a_range_only_when_every_value_fits(ctx) -> None:
    await _score(ctx)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"score": 3})

    field, action = await _update_field(
        ctx, entity="organization", key="score", minimum=1, maximum=5
    )
    assert (action, field.minimum, field.maximum) == ("updated", 1, 5)
    with pytest.raises(FieldError, match=r"1 organization record\(s\) hold"):
        await _update_field(ctx, entity="organization", key="score", maximum=2)
    # One side given keeps the other.
    field, _action = await _update_field(ctx, entity="organization", key="score", maximum=4)
    assert (field.minimum, field.maximum) == (1, 4)
    # clear_bounds lifts both, then applies what is given.
    field, _action = await _update_field(
        ctx, entity="organization", key="score", clear_bounds=True, minimum=0
    )
    assert (field.minimum, field.maximum) == (0, None)
    field, _action = await _update_field(ctx, entity="organization", key="score", clear_bounds=True)
    assert (field.minimum, field.maximum) == (None, None)


async def test_update_field_range_counts_a_retired_fields_values_and_refuses_other_types(
    ctx,
) -> None:
    await _score(ctx)
    org_id = await _org(ctx)
    async with session_for(ctx) as conn:
        await store.update_organization(conn, ctx, org_id=org_id, fields={"score": 9})
    await _update_field(ctx, entity="organization", key="score", retired=True)
    with pytest.raises(FieldError, match="hold a 'score' value outside"):
        await _update_field(ctx, entity="organization", key="score", maximum=5)
    await _fit(ctx)
    with pytest.raises(FieldError, match="takes no minimum or maximum"):
        await _update_field(ctx, entity="organization", key="fit", minimum=1)


async def test_a_range_change_waits_for_a_write_in_flight_and_sees_its_value(ctx) -> None:
    await _score(ctx)
    org_id = await _org(ctx)
    written = asyncio.Event()
    release = asyncio.Event()

    async def writer() -> None:
        async with session_for(ctx) as conn:
            await store.update_organization(conn, ctx, org_id=org_id, fields={"score": 9})
            written.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await written.wait()
    change = asyncio.create_task(_update_field(ctx, entity="organization", key="score", maximum=5))
    await asyncio.sleep(0.3)
    assert not change.done(), "a range change must wait for the write holding the definition"
    release.set()
    await task
    with pytest.raises(FieldError, match="outside"):
        await change


async def test_a_range_restored_during_a_concurrent_change_stands(ctx) -> None:
    await _score(ctx, maximum=5)
    changed = asyncio.Event()
    release = asyncio.Event()

    async def changer() -> None:
        async with session_for(ctx) as conn:
            await store.update_field(conn, ctx, entity="organization", key="score", maximum=10)
            changed.set()
            await release.wait()

    task = asyncio.create_task(changer())
    await changed.wait()
    restore = asyncio.create_task(_update_field(ctx, entity="organization", key="score", maximum=5))
    await asyncio.sleep(0.3)
    assert not restore.done(), "a range change must wait for the one holding the definition"
    release.set()
    await task
    field, action = await restore
    assert (action, field.maximum) == ("updated", 5)
