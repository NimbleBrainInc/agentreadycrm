"""Custom-field tool contract.

Invokes the registered tool callables directly, as the local runtime's one
principal. The asserts are on what an agent
sees: the shapes, the ``next_step`` that names the fixing call, and that a
refused write tells the agent enough to succeed on the next turn.
"""

from __future__ import annotations

import json

import pytest

import mcp_people.server as server
from mcp_people.fields import CORE_FIELDS

pytestmark = pytest.mark.asyncio


@pytest.fixture
def headers(app_engine) -> dict[str, str]:
    """The tools act as the local runtime's one principal; this suite needs no identity
    of its own, so the mapping is empty."""
    return {}


async def _define_fit() -> dict:
    return await server.define_field(
        entity="organization",
        key="fit",
        label="Fit",
        type="select",
        options=["Strong", "Moderate"],
        description="Rubric v2: B2B, large market, reachable buyers.",
        defined_by="rubric-v2",
    )


async def test_define_then_describe(headers) -> None:
    defined = await _define_fit()
    assert defined["action"] == "created"
    assert defined["field"]["key"] == "fit"
    assert "update_organization(organization_id=..., fields={'fit': ...})" in defined["next_step"]

    again = await server.define_field(
        entity="organization", key="fit", label="Fit", type="select", options=["No"]
    )
    assert (again["action"], again["options_added"]) == ("matched", ["No"])

    described = await server.list_fields()
    assert set(described["fields"]) == {"contact", "organization", "opportunity"}
    assert described["next_step"].strip()
    org = described["fields"]["organization"]
    assert [c["key"] for c in org["core"]] == [k for k, _t, _d in CORE_FIELDS["organization"]]
    assert [(f["key"], f["type"], f["options"]) for f in org["custom"]] == [
        ("fit", "select", ["Strong", "Moderate", "No"])
    ]
    assert described["fields"]["contact"]["custom"] == []
    blob = json.dumps(described, default=str)
    assert "tenant_id" not in blob and "workspace_id" not in blob

    only = await server.list_fields(entity="contact")
    assert set(only["fields"]) == {"contact"}


async def test_fields_round_trip_through_the_tools(headers) -> None:
    await _define_fit()
    created = await server.find_or_create_organization(
        name="Larkfield", domain="larkfield.example", fields={"fit": "strong"}, fields_by="import"
    )
    org_id = created["organization"]["id"]
    assert created["organization"]["fields"] == {"fit": "Strong"}

    updated = await server.update_organization(
        organization_id=org_id,
        fields={"fit": {"value": "moderate", "why": "Leans on custom services."}},
        fields_by="rubric-v2",
    )
    assert updated["organization"]["fields"] == {"fit": "Moderate"}
    assert "field_details" not in updated["organization"]

    got = await server.get_organization(organization_id=org_id)
    details = got["organization"]["field_details"]["fit"]
    assert (details["by"], details["why"]) == ("rubric-v2", "Leans on custom services.")

    listed = await server.list_organizations()
    assert listed["organizations"][0]["fields"] == {"fit": "Moderate"}
    assert "field_details" not in listed["organizations"][0]


async def test_an_undefined_field_says_how_to_define_it(headers) -> None:
    created = await server.find_or_create_organization(name="Larkfield")
    refused = await server.update_organization(
        organization_id=created["organization"]["id"], fields={"fit": "strong"}
    )
    assert refused["error"]["code"] == "invalid_request"
    assert "no organization field named 'fit'" in refused["error"]["message"]
    assert "define_field(entity='organization'" in refused["next_step"]


async def test_an_unknown_option_lists_the_options(headers) -> None:
    await _define_fit()
    created = await server.find_or_create_organization(name="Larkfield")
    refused = await server.update_organization(
        organization_id=created["organization"]["id"], fields={"fit": "excellent"}
    )
    assert refused["error"]["code"] == "invalid_request"
    assert "Strong, Moderate" in refused["error"]["message"]
    assert "options=" in refused["next_step"]


async def test_define_field_refuses_a_bad_definition(headers) -> None:
    for entity, key, field_type, expected in [
        ("interaction", "x", "text", "entity must be"),
        ("contact", "title", "text", "core contact"),
        ("contact", "x", "vibes", "type must be"),
        ("contact", "x", "select", "at least one option"),
    ]:
        # The schema refuses an entity or type outside its enum before a client's call runs;
        # the tool body refuses it too, for a caller that reaches the function directly.
        refused = await server.define_field(
            entity=entity,  # ty: ignore[invalid-argument-type]
            key=key,
            label="X",
            type=field_type,  # ty: ignore[invalid-argument-type]
        )
        assert refused["error"]["code"] == "invalid_request", refused
        assert expected in refused["error"]["message"]
    assert (await server.list_fields())["fields"]["contact"]["custom"] == []


async def test_contact_email_status_through_the_tools(headers) -> None:
    created = await server.find_or_create_contact(
        name="Dana", email="dana@larkfield.example", email_status="valid"
    )
    contact = created["contact"]
    assert contact["email_status"] == "valid"
    assert contact["email_checked_at"]

    moved = await server.update_contact(contact_id=contact["id"], email="dana@elsewhere.example")
    assert "email_status" not in moved["contact"]
    assert "email_checked_at" not in moved["contact"]

    refused = await server.update_contact(
        contact_id=contact["id"],
        email_status="fine",  # ty: ignore[invalid-argument-type]
    )
    assert refused["error"]["code"] == "invalid_request"
    assert "invalid, risky, unknown, valid" in refused["error"]["message"]


async def test_list_and_count_through_the_tools(headers) -> None:
    await _define_fit()
    for name, fit in (("Larkfield", "strong"), ("Quillstack", "moderate"), ("Brightforge", None)):
        await server.find_or_create_organization(
            name=name, domain=f"{name.lower()}.example", fields={"fit": fit} if fit else None
        )
    for person, org in (("Dana", "Larkfield"), ("Miles", "Larkfield"), ("Priya", "Quillstack")):
        await server.find_or_create_contact(
            name=person, email=f"{person.lower()}@people.example", organization_name=org
        )

    listed = await server.list_contacts(
        filter={"field": "organization.fit", "op": "eq", "value": "strong"},
        sort="name",
        columns=["name", "organization.name", "organization.fit"],
        limit=1,
    )
    assert listed["total"] == 2
    assert listed["contacts"] == [
        {
            "id": listed["contacts"][0]["id"],
            # A projected row carries the record's address too, so the agent can open it.
            "uri": f"people://contacts/{listed['contacts'][0]['id']}",
            "name": "Dana",
            "organization.name": "Larkfield",
            "organization.fit": "Strong",
        }
    ]
    assert "same filter, sort and columns" in listed["next_step"]

    rest = await server.list_contacts(
        filter={"field": "organization.fit", "op": "eq", "value": "strong"},
        sort="name",
        columns=["name"],
        limit=1,
        cursor=listed["next_cursor"],
    )
    assert [c["name"] for c in rest["contacts"]] == ["Miles"]
    assert rest["next_cursor"] is None
    assert "total" not in rest

    # Without `columns` a row is the full record, as it always was.
    full = await server.list_contacts(limit=1)
    assert {"id", "name", "status", "effective_status", "created_at"} <= set(full["contacts"][0])
    assert full["total"] == 3

    orgs = await server.list_organizations(
        filter={"field": "fit", "op": "is_set", "value": True}, sort="-contacts.count"
    )
    assert [o["name"] for o in orgs["organizations"]] == ["Larkfield", "Quillstack"]

    counted = await server.count_records(entity="organization", field="fit")
    assert counted["total"] == 3
    assert {v["value"]: v["count"] for v in counted["values"]} == {
        "Strong": 1,
        "Moderate": 1,
        None: 1,
    }
    assert "list_organizations(filter=" in counted["next_step"]


async def test_a_refused_filter_reaches_the_agent_with_its_hint(headers) -> None:
    await _define_fit()
    refused = await server.list_contacts(filter={"field": "organization.fitt", "value": "strong"})
    assert refused["error"]["code"] == "invalid_request"
    assert "no contact field named 'organization.fitt'" in refused["error"]["message"]
    assert "organization.fit" in refused["next_step"]

    wrong_op = await server.list_organizations(filter={"field": "fit", "op": "gt", "value": "x"})
    assert "eq, ne, in, not_in, is_set" in wrong_op["next_step"]

    bad_count = await server.count_records(
        entity="deal",  # ty: ignore[invalid-argument-type]
        field="fit",
    )
    assert bad_count["error"]["code"] == "invalid_request"

    still_a_bad_cursor = await server.list_contacts(cursor="not-a-cursor")
    assert still_a_bad_cursor["error"]["code"] == "invalid_request"


async def test_describe_fields_names_the_rollup_paths(headers) -> None:
    described = await server.list_fields()
    assert {r["path"] for r in described["fields"]["contact"]["rollups"]} == {
        "signals.active",
        "interactions.count",
        "interactions.last_at",
    }
    assert {r["path"] for r in described["fields"]["organization"]["rollups"]} == {
        "signals.active",
        "contacts.count",
    }


async def test_update_field_retires_then_restores(headers) -> None:
    await _define_fit()
    await server.define_field(
        entity="organization", key="fit_grade", label="Fit grade", type="select", options=["A"]
    )

    retired = await server.update_field(
        entity="organization", key="fit", retired=True, replaced_by="fit_grade"
    )
    assert retired["action"] == "retired"
    assert retired["field"]["retired_at"]
    assert retired["field"]["replaced_by"] == "fit_grade"
    assert "retired=false" in retired["next_step"]

    live = await server.list_fields(entity="organization")
    assert [f["key"] for f in live["fields"]["organization"]["custom"]] == ["fit_grade"]
    assert "retired_at" not in live["fields"]["organization"]["custom"][0]

    everything = await server.list_fields(entity="organization", include_retired=True)
    custom = {f["key"]: f for f in everything["fields"]["organization"]["custom"]}
    assert set(custom) == {"fit", "fit_grade"}
    assert custom["fit"]["retired_at"] == retired["field"]["retired_at"]
    assert custom["fit"]["replaced_by"] == "fit_grade"

    again = await server.update_field(entity="organization", key="fit", retired=True)
    assert again["action"] == "unchanged"
    assert again["field"]["retired_at"] == retired["field"]["retired_at"]

    restored = await server.update_field(entity="organization", key="fit", retired=False)
    assert restored["action"] == "restored"
    assert "retired_at" not in restored["field"] and "replaced_by" not in restored["field"]
    assert "fields={'fit': ...}" in restored["next_step"]
    keys = [f["key"] for f in (await server.list_fields())["fields"]["organization"]["custom"]]
    assert keys == ["fit", "fit_grade"]


async def test_update_field_default_column_sets_and_clears_the_flag(headers) -> None:
    await _define_fit()

    marked = await server.update_field(entity="organization", key="fit", default_column=True)
    assert marked["action"] == "updated"
    assert marked["field"]["default_column"] is True
    live = await server.list_fields(entity="organization")
    assert live["fields"]["organization"]["custom"][0]["default_column"] is True

    again = await server.update_field(entity="organization", key="fit", default_column=True)
    assert again["action"] == "unchanged"

    cleared = await server.update_field(entity="organization", key="fit", default_column=False)
    assert cleared["action"] == "updated"
    assert cleared["field"]["default_column"] is False


async def test_update_field_default_column_refused_on_a_retired_field(headers) -> None:
    await _define_fit()
    await server.update_field(entity="organization", key="fit", retired=True)

    refused = await server.update_field(entity="organization", key="fit", default_column=True)
    assert "retired" in refused["error"]["message"]
    described = await server.list_fields(entity="organization", include_retired=True)
    assert described["fields"]["organization"]["custom"][0]["default_column"] is False


async def test_a_retired_field_keeps_its_default_column_through_restoring(headers) -> None:
    await _define_fit()
    await server.update_field(entity="organization", key="fit", default_column=True)

    retired = await server.update_field(entity="organization", key="fit", retired=True)
    assert retired["action"] == "retired"
    assert retired["field"]["default_column"] is True

    restored = await server.update_field(entity="organization", key="fit", retired=False)
    assert restored["action"] == "restored"
    assert restored["field"]["default_column"] is True
    live = await server.list_fields(entity="organization")
    assert live["fields"]["organization"]["custom"][0]["default_column"] is True


async def test_update_field_label_changes_the_name_and_leaves_key_and_values(headers) -> None:
    await _define_fit()
    org = await server.find_or_create_organization(
        name="Acme", domain="acme.example", fields={"fit": "Strong"}
    )

    renamed = await server.update_field(entity="organization", key="fit", label="  Fit score  ")
    assert renamed["action"] == "updated"
    assert renamed["field"]["label"] == "Fit score"
    assert renamed["field"]["key"] == "fit"

    again = await server.update_field(entity="organization", key="fit", label="Fit score")
    assert again["action"] == "unchanged"

    org_id = org["organization"]["id"]
    read = await server.get_organization(organization_id=org_id)
    assert read["organization"]["fields"]["fit"] == "Strong"
    listed = await server.list_organizations(
        filter={"field": "fit", "op": "eq", "value": "Strong"}, columns=["fit"]
    )
    assert [row["fit"] for row in listed["organizations"]] == ["Strong"]


async def test_update_field_refuses_a_blank_label(headers) -> None:
    await _define_fit()
    refused = await server.update_field(entity="organization", key="fit", label="   ")
    assert "error" in refused
    described = await server.list_fields(entity="organization")
    assert described["fields"]["organization"]["custom"][0]["label"] == "Fit"


async def test_update_field_on_an_undefined_key_is_a_classified_refusal(headers) -> None:
    refused = await server.update_field(entity="organization", key="fit", retired=True)
    assert refused["error"]["code"] == "invalid_request", refused
    assert "no organization field named 'fit'" in refused["error"]["message"]
    assert "list_fields(" in refused["next_step"]


async def test_update_field_refuses_a_replacement_that_is_not_live(headers) -> None:
    await _define_fit()
    refused = await server.update_field(
        entity="organization", key="fit", retired=True, replaced_by="contact.nope"
    )
    assert refused["error"]["code"] == "invalid_request", refused
    assert "is not defined" in refused["error"]["message"]
    assert refused["next_step"].strip()
    # The refusal left the field live.
    custom = (await server.list_fields(entity="organization"))["fields"]["organization"]
    assert [f["key"] for f in custom["custom"]] == ["fit"]


async def test_update_field_is_announced_as_a_destructive_write() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}
    hints = tools["update_field"].annotations
    assert hints is not None
    assert hints.read_only_hint is False
    assert hints.destructive_hint is True
