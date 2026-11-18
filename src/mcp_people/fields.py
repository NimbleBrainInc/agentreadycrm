"""Custom fields — the definitions and value rules.

A field is a fact someone needs to filter, sort, or show in a column; prose that
only needs to be read stays in the dossier. Two parts, both here as pure logic
(no database, no I/O), so the store and the tests share one statement of each
rule:

  * a **definition** (``field_def``): one per ``(workspace, entity, key)``, naming
    the field's label, type, options and an agent-facing description;
  * a **value**: stored on the entity row's ``fields`` JSONB as an envelope
    ``{"v": value, "by": who, "at": when, "why": rationale}``, so a value an agent
    wrote stays attributable from the record itself.

The vocabularies follow the evidence records' rule: the
columns stay open ``Text``/``JSONB`` with no ``CHECK``, and an unrecognized value
is refused at the write seam. A field type, like a signal grade, is not a state a
caller may invent: a value typed ``vibes`` cannot be filtered or sorted, and that
is the whole point of making it a field.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

__all__ = [
    "clean_bounds",
    "CORE_FIELDS",
    "EMAIL_STATUSES",
    "ENTITIES",
    "FIELD_TYPES",
    "MAX_FIELDS_PER_ENTITY",
    "FieldError",
    "FieldInput",
    "clean_options",
    "coerce_value",
    "envelope",
    "field_details",
    "flatten",
    "merge_options",
    "normalize_entity",
    "normalize_key",
    "normalize_type",
    "parse_input",
]

# The entities that carry custom fields.
ENTITIES = ("contact", "organization", "opportunity")

FIELD_TYPES = frozenset({"text", "number", "boolean", "date", "select", "multi_select", "list"})

# Types whose values come from the definition's options.
_OPTION_TYPES = frozenset({"select", "multi_select"})

# Whether an address can receive mail. `unknown` is a check that could not decide
# (the server would not say); never-checked is the absence of a status, not a value.
EMAIL_STATUSES = frozenset({"valid", "invalid", "risky", "unknown"})

# A field key is a stable identifier, written into filters and saved views.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

MAX_FIELDS_PER_ENTITY = 100
_MAX_OPTIONS = 100
_MAX_LIST_ITEMS = 50
_CAP_LABEL = 128
_CAP_OPTION = 128
_CAP_DESCRIPTION = 1024
_CAP_TEXT = 4096
_CAP_LIST_ITEM = 1024
_CAP_WHY = 2048
_CAP_BY = 128

# The core fields of each entity, as `list_fields` reports them, each with the
# kind it filters and sorts as (`timestamp` is a core-only kind: a custom field
# holds a `date`). A custom key
# may not shadow one: a filter path names a field by key, so `title` has to mean
# one thing. `test_fields.py` holds these equal to the wire models.
CORE_FIELDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "contact": (
        ("name", "text", "Display name."),
        ("email", "text", "Primary dedupe key, lowercased."),
        (
            "email_status",
            "select",
            "Whether the address can receive mail: valid / invalid / risky / unknown. "
            "Absent until someone checks it.",
        ),
        ("email_checked_at", "timestamp", "When email_status was last set."),
        ("phone", "text", "Phone number."),
        ("title", "text", "Role at the organization."),
        ("organization_id", "text", "The organization this person works at."),
        (
            "status",
            "text",
            "Lifecycle stage: sourced / enriched / contacted / engaged, or your own.",
        ),
        ("archived_at", "timestamp", "When the contact was archived; absent when not."),
        (
            "effective_status",
            "text",
            "The stage to display and filter on, computed from status and history.",
        ),
        ("lead_source", "text", "How the lead arrived: referral / inbound / outbound / …"),
        ("source", "text", "Provenance of the record."),
        ("dossier", "text", "Markdown research notes."),
        ("dossier_updated_at", "timestamp", "When the dossier was last written."),
        ("created_at", "timestamp", "When the record was created."),
        ("updated_at", "timestamp", "When the record last changed."),
    ),
    "organization": (
        ("name", "text", "Display name."),
        ("domain", "text", "Primary dedupe key, a registrable lowercased domain."),
        ("website", "text", "Website URL."),
        ("industry", "text", "Industry, as an open string."),
        ("region", "text", "Coarse region."),
        ("employee_range", "text", "Headcount band, e.g. 51-200."),
        ("revenue_range", "text", "Revenue band."),
        ("headquarters", "text", "Headquarters location."),
        ("status", "text", "discovered (an unpromoted lookalike) / active."),
        ("archived_at", "timestamp", "When the organization was archived; absent when not."),
        ("source", "text", "Provenance of the record."),
        ("dossier", "text", "Markdown research notes."),
        ("dossier_updated_at", "timestamp", "When the dossier was last written."),
        ("created_at", "timestamp", "When the record was created."),
        ("updated_at", "timestamp", "When the record last changed."),
    ),
    "opportunity": (
        ("title", "text", "The deal's name."),
        ("organization_id", "text", "The account."),
        ("primary_contact_id", "text", "The main contact."),
        ("stage", "select", "The pipeline stage key (get_pipeline lists them)."),
        ("outcome", "select", "open / won / lost, from the stage."),
        ("value", "number", "Deal size."),
        ("currency", "text", "Currency of the value, e.g. USD."),
        (
            "probability",
            "number",
            "0-100. The deal's own number when set, otherwise the stage's default.",
        ),
        ("weighted_value", "number", "value × probability / 100."),
        ("close_date", "date", "Target or actual close date."),
        ("closed_at", "timestamp", "When the deal entered a won or lost stage."),
        ("source", "text", "Provenance of the record."),
        ("dossier", "text", "Markdown research notes."),
        ("dossier_updated_at", "timestamp", "When the dossier was last written."),
        ("created_at", "timestamp", "When the record was created."),
        ("updated_at", "timestamp", "When the record last changed."),
    ),
}

# Names a custom key may not take: the core fields, plus the wire model's own
# structural keys.
_RESERVED = {
    entity: frozenset(name for name, _t, _d in core)
    | {"id", "fields", "field_details", "probability_is_own"}
    for entity, core in CORE_FIELDS.items()
}


class FieldError(ValueError):
    """A field write the caller can correct, with the correction spelled out.

    A ``ValueError`` so every existing handler maps it to ``invalid_request``;
    ``next_step`` replaces the generic hint with the one call that fixes it.
    """

    def __init__(self, message: str, *, next_step: str) -> None:
        super().__init__(message)
        self.next_step = next_step


@dataclass(frozen=True)
class FieldInput:
    """One submitted value, split from its optional provenance."""

    value: Any
    why: str | None = None
    by: str | None = None


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _capped(what: str, value: str, limit: int) -> str:
    if _byte_len(value) > limit:
        raise ValueError(f"{what} exceeds the maximum length of {limit} bytes")
    return value


def normalize_entity(entity: str) -> str:
    """The entity a field belongs to, or ``ValueError`` listing the accepted ones."""
    clean = entity.strip().lower() if isinstance(entity, str) else ""
    if clean not in ENTITIES:
        raise ValueError(f"entity must be one of: {', '.join(ENTITIES)}")
    return clean


def normalize_type(field_type: str) -> str:
    """A field type from the closed list, or ``ValueError`` listing it."""
    clean = field_type.strip().lower() if isinstance(field_type, str) else ""
    if clean not in FIELD_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(FIELD_TYPES))}")
    return clean


def normalize_key(entity: str, key: str) -> str:
    """A well-formed key that does not shadow one of ``entity``'s core fields."""
    clean = key.strip() if isinstance(key, str) else ""
    if not _KEY_RE.match(clean):
        raise ValueError(
            "key must be lowercase letters, digits and underscores, start with a letter, "
            "and be at most 48 characters"
        )
    if clean in _RESERVED[entity]:
        raise ValueError(
            f"{clean!r} is a core {entity} field; set it with its own argument, not as a "
            "custom field"
        )
    return clean


def clean_label(label: str) -> str:
    """A non-empty display label within its cap."""
    clean = label.strip() if isinstance(label, str) else ""
    if not clean:
        raise ValueError("label must not be empty")
    return _capped("label", clean, _CAP_LABEL)


def clean_description(description: str | None) -> str | None:
    """An optional agent-facing description within its cap."""
    if description is None:
        return None
    clean = description.strip()
    return _capped("description", clean, _CAP_DESCRIPTION) or None


def clean_options(field_type: str, options: Sequence[str] | None) -> list[str]:
    """The options a definition declares: trimmed, distinct ignoring case, capped.

    A select with no options could hold nothing, so one is required; a type that
    takes no options refuses them rather than storing a list nothing reads.
    """
    if field_type not in _OPTION_TYPES:
        if options:
            raise ValueError(f"a {field_type} field takes no options")
        return []
    cleaned = merge_options([], options or [])
    if not cleaned:
        raise ValueError(f"a {field_type} field needs at least one option")
    return cleaned


def merge_options(existing: Sequence[str], added: Sequence[str]) -> list[str]:
    """``existing`` followed by each new option not already present, ignoring case.

    The first spelling of an option is the canonical one; a later spelling that
    differs only by case names the same option and adds nothing.
    """
    merged = list(existing)
    seen = {o.casefold() for o in merged}
    for raw in added:
        if not isinstance(raw, str):
            raise ValueError("options must be strings")
        option = raw.strip()
        if not option:
            continue
        _capped("option", option, _CAP_OPTION)
        if option.casefold() not in seen:
            seen.add(option.casefold())
            merged.append(option)
    if len(merged) > _MAX_OPTIONS:
        raise ValueError(f"a field holds at most {_MAX_OPTIONS} options")
    return merged


def parse_input(key: str, raw: Any) -> FieldInput:
    """Split one submitted field into its value and optional provenance.

    A field is written either as a bare value, or as ``{"value": …, "why": …,
    "by": …}`` when the writer has a rationale to keep beside it. No field type is
    an object, so a mapping without ``value`` is a malformed envelope rather than a
    value to store.
    """
    if not isinstance(raw, Mapping):
        return FieldInput(value=raw)
    unknown = set(raw) - {"value", "why", "by"}
    if "value" not in raw or unknown:
        raise ValueError(
            f"field {key!r}: pass a plain value, or an object with 'value' and optional "
            "'why' and 'by'"
        )
    why, by = raw.get("why"), raw.get("by")
    for name, part, limit in (("why", why, _CAP_WHY), ("by", by, _CAP_BY)):
        if part is not None:
            if not isinstance(part, str):
                raise ValueError(f"field {key!r}: {name} must be a string")
            _capped(f"field {key!r} {name}", part, limit)
    return FieldInput(
        value=raw["value"],
        why=(why.strip() or None) if isinstance(why, str) else None,
        by=(by.strip() or None) if isinstance(by, str) else None,
    )


def _match_option(key: str, entity: str, value: Any, options: Sequence[str]) -> str:
    if isinstance(value, str):
        folded = value.strip().casefold()
        for option in options:
            if option.casefold() == folded:
                return option
    raise FieldError(
        f"field {key!r} must be one of: {', '.join(options)}",
        next_step=(
            "Use one of the listed options, or add a new one with "
            f"define_field(entity={entity!r}, key={key!r}, ..., options=[<new option>]). "
            "If you cannot call define_field, tell the user which option is missing."
        ),
    )


def _strings(key: str, value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"field {key!r} takes a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"field {key!r} takes a list of strings")
    return [item.strip() for item in value if item.strip()]


def clean_bounds(
    field_type: str, minimum: float | None, maximum: float | None
) -> tuple[float | None, float | None]:
    """A number field's inclusive range: finite, low not above high; other types take none."""
    if minimum is None and maximum is None:
        return None, None
    if field_type != "number":
        raise ValueError(f"a {field_type} field takes no minimum or maximum; only a number does")
    bounds: list[float | None] = []
    for name, bound in (("minimum", minimum), ("maximum", maximum)):
        if bound is None:
            bounds.append(None)
            continue
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(bound):
            raise ValueError(f"{name} must be a finite number")
        bounds.append(float(bound))
    low, high = bounds
    if low is not None and high is not None and low > high:
        raise ValueError(f"minimum {minimum} is above maximum {maximum}")
    return low, high


def coerce_value(
    *,
    entity: str,
    key: str,
    field_type: str,
    options: Sequence[str],
    value: Any,
    minimum: float | None = None,
    maximum: float | None = None,
) -> Any:
    """Validate ``value`` against the definition and return its stored form.

    The stored form is what a filter compares against, so each type has exactly
    one: a number is a JSON number, a date an ISO ``YYYY-MM-DD`` string, a select
    the option's canonical spelling. ``None`` is not handled here: it means
    "remove the value" and never reaches a type check.
    """
    if field_type == "text":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"field {key!r} takes a non-empty string")
        return _capped(f"field {key!r}", value.strip(), _CAP_TEXT)
    if field_type == "number":
        number: Any = value
        if isinstance(value, str):
            try:
                number = float(value) if any(c in value for c in ".eE") else int(value)
            except ValueError:
                number = None
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"field {key!r} takes a number")
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError(f"field {key!r} takes a finite number")
        if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
            raise ValueError(f"field {key!r} takes a number {_range(minimum, maximum)}")
        return number
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"field {key!r} takes true or false")
        return value
    if field_type == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except (AttributeError, ValueError):
            raise ValueError(f"field {key!r} takes a date as YYYY-MM-DD") from None
    if field_type == "select":
        return _match_option(key, entity, value, options)
    if field_type == "multi_select":
        chosen: list[str] = []
        for item in _strings(key, value):
            option = _match_option(key, entity, item, options)
            if option not in chosen:
                chosen.append(option)
        if not chosen:
            raise ValueError(f"field {key!r} takes at least one option; pass null to clear it")
        return chosen
    # list: free strings, in the order given.
    items: list[str] = []
    for item in _strings(key, value):
        _capped(f"field {key!r} item", item, _CAP_LIST_ITEM)
        if item not in items:
            items.append(item)
    if not items:
        raise ValueError(f"field {key!r} takes at least one item; pass null to clear it")
    if len(items) > _MAX_LIST_ITEMS:
        raise ValueError(f"field {key!r} holds at most {_MAX_LIST_ITEMS} items")
    return items


def _range(minimum: float | None, maximum: float | None) -> str:
    """A bound for a refusal: ``from 1 to 5``, ``of at least 0``, ``of at most 100``."""

    def show(n: float) -> str:
        return str(int(n)) if float(n).is_integer() else str(n)

    if minimum is not None and maximum is not None:
        return f"from {show(minimum)} to {show(maximum)}"
    if minimum is not None:
        return f"of at least {show(minimum)}"
    return f"of at most {show(maximum)}" if maximum is not None else ""


def envelope(value: Any, *, by: str | None, at: datetime, why: str | None) -> dict[str, Any]:
    """The stored form of one value: the value, and who set it, when, and why."""
    stored: dict[str, Any] = {"v": value, "at": at.isoformat()}
    if by:
        stored["by"] = by
    if why:
        stored["why"] = why
    return stored


def flatten(stored: Mapping[str, Any] | None, *, omit: Collection[str] = ()) -> dict[str, Any]:
    """``{key: value}`` from a row's stored envelopes — the shape a list returns.

    ``omit`` names keys to leave out: a listing row drops its retired fields, so an
    agent scanning rows never reads a stale value beside its replacement.
    """
    return {key: env["v"] for key, env in (stored or {}).items() if key not in omit}


def field_details(
    stored: Mapping[str, Any] | None,
    *,
    retired: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """``{key: {by, at, why}}`` from a row's stored envelopes — a by-id read's extra.

    ``retired`` maps each retired key to its mark (``retired_at``, and
    ``replaced_by`` when it names one), added to that key's entry; a live key's
    entry is only its provenance.
    """
    marks = retired or {}
    return {
        key: {name: env[name] for name in ("by", "at", "why") if name in env}
        | dict(marks.get(key, {}))
        for key, env in (stored or {}).items()
    }
