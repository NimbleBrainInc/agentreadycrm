"""Custom-field rules — the vocabularies and the value coercion, with no database.

``mcp_people.fields`` is pure logic, so these pin what a definition may be and
what each type stores. The stored form is what a filter compares against, so a
type that stored two spellings of one value would make a filter miss rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from mcp_people import fields
from mcp_people.fields import FieldError
from mcp_people.models import Contact, FieldDef, Opportunity, Organization

_MODELS = {"contact": Contact, "organization": Organization, "opportunity": Opportunity}
# Wire keys that are structure, not a core field `describe_fields` reports.
_STRUCTURAL = {"id", "fields", "field_details", "probability_is_own"}


@pytest.mark.parametrize("entity", fields.ENTITIES)
def test_core_fields_are_exactly_the_wire_model(entity: str) -> None:
    """``CORE_FIELDS`` restates the wire model, so it is held equal to it: a core
    field missing here could be shadowed by a custom key of the same name."""
    declared = [key for key, _type, _description in fields.CORE_FIELDS[entity]]
    assert len(declared) == len(set(declared))
    assert set(declared) == set(_MODELS[entity].model_fields) - _STRUCTURAL


@pytest.mark.parametrize("key", ["fit", "icp_sentence_1", "a"])
def test_a_well_formed_key_is_accepted(key: str) -> None:
    assert fields.normalize_key("contact", key) == key


@pytest.mark.parametrize("key", ["", "Fit", "1st", "fit score", "fit-score", "x" * 49])
def test_a_malformed_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="key must be"):
        fields.normalize_key("contact", key)


@pytest.mark.parametrize(
    ("entity", "key"),
    [("contact", "title"), ("contact", "email_status"), ("organization", "domain")],
)
def test_a_key_cannot_shadow_a_core_field(entity: str, key: str) -> None:
    with pytest.raises(ValueError, match="core"):
        fields.normalize_key(entity, key)


def test_a_core_name_of_the_other_entity_is_free() -> None:
    """``domain`` is core on an organization only; a contact may define it."""
    assert fields.normalize_key("contact", "domain") == "domain"


def test_entity_and_type_come_from_closed_lists() -> None:
    assert fields.normalize_entity(" Organization ") == "organization"
    assert fields.normalize_type("Multi_Select") == "multi_select"
    assert fields.normalize_entity("opportunity") == "opportunity"
    with pytest.raises(ValueError, match="entity must be one of"):
        fields.normalize_entity("interaction")
    with pytest.raises(ValueError, match="type must be one of"):
        fields.normalize_type("vibes")


def test_a_select_needs_options_and_other_types_refuse_them() -> None:
    assert fields.clean_options("select", [" Strong ", "moderate", "STRONG", ""]) == [
        "Strong",
        "moderate",
    ]
    with pytest.raises(ValueError, match="at least one option"):
        fields.clean_options("select", [])
    with pytest.raises(ValueError, match="takes no options"):
        fields.clean_options("number", ["1"])
    assert fields.clean_options("text", None) == []


def test_merging_options_appends_only_what_is_new_ignoring_case() -> None:
    assert fields.merge_options(["Strong", "Moderate"], ["strong", "No"]) == [
        "Strong",
        "Moderate",
        "No",
    ]


def _coerce(field_type: str, value: object, options: tuple[str, ...] = ()) -> object:
    return fields.coerce_value(
        entity="organization", key="k", field_type=field_type, options=options, value=value
    )


@pytest.mark.parametrize(
    ("field_type", "given", "stored"),
    [
        ("text", "  procurement teams  ", "procurement teams"),
        ("number", 27, 27),
        ("number", 2.5, 2.5),
        ("number", "27", 27),
        ("number", "2.5", 2.5),
        ("boolean", True, True),
        ("date", "2026-08-28", "2026-08-28"),
        ("date", date(2026, 8, 28), "2026-08-28"),
        ("date", datetime(2026, 8, 28, 9, 30, tzinfo=UTC), "2026-08-28"),
        ("list", ["a", " b ", "a", ""], ["a", "b"]),
    ],
)
def test_each_type_has_one_stored_form(field_type: str, given: object, stored: object) -> None:
    assert _coerce(field_type, given) == stored


@pytest.mark.parametrize(
    ("field_type", "given"),
    [
        ("text", ""),
        ("text", 5),
        ("number", True),
        ("number", "many"),
        ("number", float("nan")),
        ("boolean", "true"),
        ("date", "28/08/2026"),
        ("date", 20260828),
        ("list", "one string"),
        ("list", []),
        ("list", [1, 2]),
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(field_type: str, given: object) -> None:
    with pytest.raises(ValueError, match="field 'k'"):
        _coerce(field_type, given)


def test_a_select_stores_the_canonical_spelling_of_its_option() -> None:
    """Case-insensitive on the way in, one spelling on the way out — the property
    that keeps a select from growing ``Strong`` and ``strong`` as two values."""
    options = ("Strong", "Moderate")
    assert _coerce("select", "STRONG", options) == "Strong"
    assert _coerce("multi_select", ["moderate", "Strong", "MODERATE"], options) == [
        "Moderate",
        "Strong",
    ]


def test_an_unknown_option_names_the_options_and_how_to_add_one() -> None:
    with pytest.raises(FieldError, match="must be one of: Strong, Moderate") as caught:
        _coerce("select", "excellent", ("Strong", "Moderate"))
    assert "define_field(entity='organization', key='k'" in caught.value.next_step


def test_a_bare_value_and_an_envelope_parse_to_the_same_value() -> None:
    assert fields.parse_input("fit", "strong") == fields.FieldInput(value="strong")
    assert fields.parse_input(
        "fit", {"value": "strong", "why": " B2B, large market ", "by": "rubric-v2"}
    ) == fields.FieldInput(value="strong", why="B2B, large market", by="rubric-v2")
    assert fields.parse_input("fit", None) == fields.FieldInput(value=None)


@pytest.mark.parametrize("raw", [{"why": "no value"}, {"value": 1, "source": "x"}, {}])
def test_an_object_that_is_not_an_envelope_is_refused(raw: dict) -> None:
    with pytest.raises(ValueError, match="plain value, or an object with 'value'"):
        fields.parse_input("fit", raw)


def test_the_envelope_round_trips_to_a_flat_value_and_its_details() -> None:
    at = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    stored = {
        "fit": fields.envelope("Strong", by="rubric-v2", at=at, why="B2B"),
        "headcount": fields.envelope(27, by=None, at=at, why=None),
    }
    assert stored["headcount"] == {"v": 27, "at": at.isoformat()}
    assert fields.flatten(stored) == {"fit": "Strong", "headcount": 27}
    assert fields.field_details(stored) == {
        "fit": {"by": "rubric-v2", "at": at.isoformat(), "why": "B2B"},
        "headcount": {"at": at.isoformat()},
    }
    assert fields.flatten(None) == {}


def _definition_row(**retirement: object) -> dict[str, object]:
    now = datetime(2026, 10, 26, tzinfo=UTC)
    row: dict[str, object] = {
        "entity": "contact",
        "key": "fit",
        "label": "Fit",
        "type": "text",
        "options": [],
        "created_at": now,
        "updated_at": now,
    }
    return row | retirement


@pytest.mark.parametrize(
    ("replaced_by_entity", "replaced_by_key", "path"),
    [
        (None, None, None),
        ("contact", "fit_grade", "fit_grade"),
        ("organization", "fit_grade", "organization.fit_grade"),
    ],
)
def test_a_definition_names_its_replacement_by_path(
    replaced_by_entity: str | None, replaced_by_key: str | None, path: str | None
) -> None:
    """The replacement reads as a path a filter takes: the bare key on the same
    entity, else qualified by the entity that holds it."""
    retired = datetime(2026, 10, 26, 12, tzinfo=UTC)
    definition = FieldDef.from_row(
        _definition_row(
            retired_at=retired,
            replaced_by_entity=replaced_by_entity,
            replaced_by_key=replaced_by_key,
        )
    )
    assert definition.retired_at == retired
    assert definition.replaced_by == path


def test_a_live_definition_carries_no_retired_state_on_the_wire() -> None:
    definition = FieldDef.from_row(_definition_row())
    wire = definition.model_dump(exclude_none=True)
    assert "retired_at" not in wire
    assert "replaced_by" not in wire
