"""Pydantic wire contracts for People — Organization, Contact, Interaction.

These models are the **public contract**: the exact shapes the MCP tools return
to the agent. They are deliberately decoupled from the database schema. The
precise rules:

- **Opaque, public string ids only** (``org_<ulid>`` / ``ct_<ulid>`` / ``ix_<ulid>``).
  Never a DB sequence id, never a raw row id.
- **Identity is implicit and never returned.** ``tenant_id`` and ``workspace_id``
  exist on every row in the database but MUST NOT appear in any wire model — the
  agent never sees who/where it is scoped to.
- **No SQLAlchemy types, no internal columns.** No ORM rows cross this boundary.
- **Open strings, not enums.** ``status`` / ``lead_source`` / ``kind`` /
  ``industry`` are plain ``str`` / ``str | None`` so the agents writing them can
  invent states without forcing a People schema migration. There are no closed
  enums here by design.
- **Datetimes are ISO-8601.** Pydantic v2 serializes ``datetime`` to ISO-8601;
  store UTC-aware values.

Serialization note (``exclude_none``):
    Pydantic v2's ``ConfigDict`` has **no** ``exclude_none`` key — it is a
    ``model_dump`` argument, not model config. To omit ``None`` fields on the
    wire, serialize via ``model_dump(exclude_none=True)`` /
    ``model_dump_json(exclude_none=True)``, or use the :func:`dump` /
    :func:`dump_json` helpers below, which bake that in. ``model_config`` only
    sets ``extra="forbid"`` so a stray DB column (e.g. ``tenant_id``) is rejected
    rather than silently leaking onto the wire.

Each model carries a ``from_row`` classmethod that maps a SQLAlchemy row/mapping
(or any attribute- or key-bearing object) to the wire model. The store layer
uses these as the single seam from DB row → public shape; only the listed wire
fields are read, so internal columns never escape.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from mcp_people.addresses import record_uri
from mcp_people.fields import field_details, flatten


def _get(row: Any, key: str) -> Any:
    """Read ``key`` from a SQLAlchemy row, mapping, or plain attribute object.

    Supports ``Mapping`` access (``row["key"]``), SQLAlchemy ``Row._mapping`` /
    ORM attribute access, and falls back to ``None`` for absent optional fields.
    """
    # Mapping-like (dict, RowMapping, SQLAlchemy Row via ._mapping).
    mapping = getattr(row, "_mapping", row)
    try:
        if key in mapping:  # type: ignore[operator]
            return mapping[key]  # type: ignore[index]
    except TypeError:
        pass
    # Attribute-like (ORM instance, namedtuple-ish).
    return getattr(row, key, None)


class Organization(BaseModel):
    """An Organization wire contract (``org_<ulid>``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id — the ecosystem join key.")

    @computed_field(
        description="The organization's address (people://…): the view that shows it opens there.",
    )
    @property
    def uri(self) -> str:
        return record_uri(self.id) or ""

    name: str
    domain: str | None = Field(
        default=None, description="Primary dedupe key (normalized, lowercased registrable domain)."
    )
    website: str | None = None
    industry: str | None = Field(default=None, description="Open string, not an enum.")
    region: str | None = None
    employee_range: str | None = Field(
        default=None, description="Firmographic headcount band (e.g. 51-200)."
    )
    revenue_range: str | None = Field(default=None, description="Firmographic revenue band.")
    headquarters: str | None = Field(
        default=None, description="HQ location (finer than the coarser region)."
    )
    source: str | None = Field(
        default=None,
        description="Provenance: research / manual / import / similar_companies / bundle id.",
    )
    status: str = Field(
        description=(
            "Provenance lifecycle: discovered (auto-persisted lookalike, hidden from "
            "the default list_organizations) / active (real CRM org). Open string; "
            "default active, promoted from discovered by promote_organization."
        )
    )
    archived_at: datetime | None = Field(
        default=None,
        description="When the organization was archived; absent when it is not.",
    )
    fields: dict[str, Any] | None = Field(
        default=None,
        description="Custom field values by key. Omitted when none are set.",
    )
    field_details: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Who set each custom field, when, and why. Returned by get_organization only. "
            "A retired field's entry adds retired_at, and replaced_by when it names the "
            "field that replaces it (list_fields' path form)."
        ),
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(
        cls,
        row: Any,
        *,
        details: bool = False,
        retired: Mapping[str, Mapping[str, Any]] | None,
    ) -> Organization:
        """Map a SQLAlchemy row/mapping to the wire model (store seam).

        ``details`` adds ``field_details``; a by-id read passes it, a listing does not.
        ``retired`` maps the entity's retired keys to their marks, and every caller passes
        it so none returns a stale value unmarked: with ``details`` the values stay and
        are marked in ``field_details``; without, they are left out of ``fields``.
        """
        stored = _get(row, "fields")
        return cls(
            id=_get(row, "id"),
            name=_get(row, "name"),
            domain=_get(row, "domain"),
            website=_get(row, "website"),
            industry=_get(row, "industry"),
            region=_get(row, "region"),
            employee_range=_get(row, "employee_range"),
            revenue_range=_get(row, "revenue_range"),
            headquarters=_get(row, "headquarters"),
            source=_get(row, "source"),
            status=_get(row, "status"),
            archived_at=_get(row, "archived_at"),
            fields=flatten(stored, omit=() if details else (retired or ())) or None,
            field_details=(field_details(stored, retired=retired) or None) if details else None,
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class Contact(BaseModel):
    """A Contact wire contract (``ct_<ulid>``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")

    @computed_field(
        description="The contact's address (people://…): the view that shows it opens there.",
    )
    @property
    def uri(self) -> str:
        return record_uri(self.id) or ""

    name: str
    email: str | None = Field(
        default=None, description="Primary dedupe key (normalized lowercase)."
    )
    email_status: str | None = Field(
        default=None,
        description=(
            "Whether the address can receive mail: valid / invalid / risky / unknown. "
            "Absent until someone checks it."
        ),
    )
    email_checked_at: datetime | None = Field(
        default=None, description="When email_status was last set."
    )
    phone: str | None = None
    title: str | None = Field(default=None, description="Role at the org.")
    organization_id: str | None = Field(
        default=None, description="FK → Organization (the works_at edge); opaque id."
    )
    lead_source: str | None = Field(
        default=None,
        description="Open string: referral / inbound / outbound / event / import / research / other / …",
    )
    status: str = Field(
        description=(
            "The contact's stage: sourced / enriched / contacted / engaged, or a stage "
            "of the workspace's own. Open string; People stores, does not drive "
            "transitions. Archiving is archived_at, not a status."
        )
    )
    archived_at: datetime | None = Field(
        default=None,
        description="When the contact was archived; absent when they are not.",
    )
    effective_status: str = Field(
        description=(
            "Read-only floor projection of status: a contact with an outbound touch "
            "reads at least contacted, one with an inbound touch at least engaged, even if "
            "status was never advanced. "
            "Derived from stored facts; People never writes it. Use this for "
            "display and worklist filters; status stays the engine's raw value."
        )
    )
    source: str | None = Field(default=None, description="Provenance.")
    fields: dict[str, Any] | None = Field(
        default=None,
        description="Custom field values by key. Omitted when none are set.",
    )
    field_details: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Who set each custom field, when, and why. Returned by get_contact only. "
            "A retired field's entry adds retired_at, and replaced_by when it names the "
            "field that replaces it (list_fields' path form)."
        ),
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(
        cls,
        row: Any,
        *,
        details: bool = False,
        retired: Mapping[str, Mapping[str, Any]] | None,
    ) -> Contact:
        """Map a SQLAlchemy row/mapping to the wire model (store seam).

        ``details`` adds ``field_details``; a by-id read passes it, a listing does not.
        ``retired`` maps the entity's retired keys to their marks, and every caller passes
        it so none returns a stale value unmarked: with ``details`` the values stay and
        are marked in ``field_details``; without, they are left out of ``fields``.
        """
        stored = _get(row, "fields")
        return cls(
            id=_get(row, "id"),
            name=_get(row, "name"),
            email=_get(row, "email"),
            email_status=_get(row, "email_status"),
            email_checked_at=_get(row, "email_checked_at"),
            phone=_get(row, "phone"),
            title=_get(row, "title"),
            organization_id=_get(row, "organization_id"),
            lead_source=_get(row, "lead_source"),
            status=_get(row, "status"),
            # Derived in SQL; every contact SELECT carries the column.
            effective_status=_get(row, "effective_status"),
            archived_at=_get(row, "archived_at"),
            source=_get(row, "source"),
            fields=flatten(stored, omit=() if details else (retired or ())) or None,
            field_details=(field_details(stored, retired=retired) or None) if details else None,
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class Interaction(BaseModel):
    """An Interaction wire contract (``ix_<ulid>``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")
    contact_id: str = Field(description="FK → Contact; opaque id.")
    organization_id: str | None = Field(
        default=None,
        description=(
            "FK → Organization, denormalized from the contact at log time — "
            "an immutable id edge, never a denormalized name."
        ),
    )
    kind: str = Field(
        description="meeting / email / call / note / research — open string, graceful fallthrough."
    )
    direction: str | None = Field(
        default=None,
        description="inbound / outbound — open string. None for direction-agnostic kinds.",
    )
    summary: str
    body: str | None = Field(default=None, description="Markdown longer detail.")
    occurred_at: datetime = Field(description="Stamped at create if absent.")
    follow_up_at: datetime | None = Field(default=None, description="A follow-up tool reads this.")
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> Interaction:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        return cls(
            id=_get(row, "id"),
            contact_id=_get(row, "contact_id"),
            organization_id=_get(row, "organization_id"),
            kind=_get(row, "kind"),
            direction=_get(row, "direction"),
            summary=_get(row, "summary"),
            body=_get(row, "body"),
            occurred_at=_get(row, "occurred_at"),
            follow_up_at=_get(row, "follow_up_at"),
            created_at=_get(row, "created_at"),
        )


class OrganizationEdge(BaseModel):
    """An org↔org graph edge (``oe_<ulid>``).

    A store-layer return object: no tool serializes it directly on the wire
    (``link_similar_organizations`` returns counts; ``list_similar_organizations`` returns
    the joined target Organization + ``similarity``/``discovered_at``), so its field
    names track the DB columns rather than the wire names.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")
    from_organization_id: str = Field(description="FK → Organization (the seed); opaque id.")
    to_organization_id: str = Field(description="FK → Organization (the related org); opaque id.")
    relation: str = Field(description="similar_to / competitor_of — open string, not an enum.")
    score: float | None = Field(
        default=None, description="The discovering provider's score (a lookalike's similarity)."
    )
    source: str | None = Field(default=None, description="Which discovery produced the edge.")
    created_at: datetime = Field(description="Edge creation time (the discovered_at signal).")
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> OrganizationEdge:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        return cls(
            id=_get(row, "id"),
            from_organization_id=_get(row, "from_organization_id"),
            to_organization_id=_get(row, "to_organization_id"),
            relation=_get(row, "relation"),
            score=_get(row, "score"),
            source=_get(row, "source"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class Opportunity(BaseModel):
    """An Opportunity wire contract (``op_<ulid>``).

    A first-class pipeline entity, peer to Contact/Interaction. ``stage`` is the
    single open-string pipeline axis; ``outcome`` is the read-only won/lost/open
    projection of it (derived in SQL, never stored, never writable).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")

    @computed_field(
        description="The opportunity's address (people://…): the view that shows it opens there.",
    )
    @property
    def uri(self) -> str:
        return record_uri(self.id) or ""

    title: str
    organization_id: str | None = Field(
        default=None, description="FK → Organization (the account); opaque id."
    )
    primary_contact_id: str | None = Field(
        default=None, description="FK → Contact (the main contact); opaque id."
    )
    stage: str = Field(
        description=(
            "The pipeline stage's key. The workspace defines its stages "
            "(get_pipeline); move_opportunity refuses one it does not hold."
        )
    )
    outcome: str = Field(
        description=(
            "Read-only: open / won / lost, from the stage. "
            "Derived in SQL; never stored, never written by a caller."
        )
    )
    value: float | None = Field(
        default=None,
        description="Optional deal size — a display-level figure, not an accounting ledger.",
    )
    currency: str | None = Field(default=None, description="Open string, e.g. USD.")
    probability: int | None = Field(
        default=None,
        description=(
            "0-100: the deal's own number when one was set, otherwise the stage's "
            "default. Null only when the stage is unknown to the pipeline."
        ),
    )
    probability_is_own: bool = Field(
        default=False,
        description="True when probability was set on the deal rather than taken from the stage.",
    )
    weighted_value: float | None = Field(
        default=None,
        description="value × probability / 100; null when either is missing.",
    )
    fields: dict[str, Any] | None = Field(
        default=None,
        description="Custom field values by key. Omitted when none are set.",
    )
    field_details: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Who set each custom field, when, and why. Returned by get_opportunity only. "
            "A retired field's entry adds retired_at, and replaced_by when it names the "
            "field that replaces it (list_fields' path form)."
        ),
    )
    close_date: date | None = Field(
        default=None, description="Target/actual close date (ISO-8601)."
    )
    closed_at: datetime | None = Field(
        default=None, description="Set when the opportunity entered a terminal (won/lost) stage."
    )
    source: str | None = Field(default=None, description="Provenance.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(
        cls,
        row: Any,
        *,
        details: bool = False,
        retired: Mapping[str, Mapping[str, Any]] | None,
    ) -> Opportunity:
        """Map a SQLAlchemy row/mapping to the wire model (store seam).

        ``outcome``, ``effective_probability`` and ``weighted_value`` are derived in
        SQL from the stage join; every opportunity SELECT carries them. ``details``
        adds ``field_details``; a by-id read passes it, a listing does not.
        ``retired`` maps the entity's retired keys to their marks, and every caller passes
        it so none returns a stale value unmarked: with ``details`` the values stay and
        are marked in ``field_details``; without, they are left out of ``fields``.
        """
        stored = _get(row, "fields")
        own = _get(row, "probability")
        return cls(
            id=_get(row, "id"),
            title=_get(row, "title"),
            organization_id=_get(row, "organization_id"),
            primary_contact_id=_get(row, "primary_contact_id"),
            stage=_get(row, "stage"),
            outcome=_get(row, "outcome"),
            value=_get(row, "value"),
            currency=_get(row, "currency"),
            probability=_get(row, "effective_probability"),
            probability_is_own=own is not None,
            weighted_value=_get(row, "weighted_value"),
            fields=flatten(stored, omit=() if details else (retired or ())) or None,
            field_details=(field_details(stored, retired=retired) or None) if details else None,
            close_date=_get(row, "close_date"),
            closed_at=_get(row, "closed_at"),
            source=_get(row, "source"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class PipelineStage(BaseModel):
    """One stage of a workspace's pipeline.

    ``key`` is what a deal stores in ``stage``; everything else is the stage's own
    and changes without touching a deal.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")
    key: str = Field(description="What a deal stores in stage; stable, named in filters.")
    label: str
    position: int = Field(description="Order on the board, lowest first.")
    probability: int = Field(description="0-100, the default for a deal entering the stage.")
    outcome: str = Field(description="open / won / lost: whether the stage closes a deal.")
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = Field(
        default=None, description="When the stage was retired; absent while it is live."
    )
    replaced_by: str | None = Field(
        default=None, description="The live stage that takes a retired one's place."
    )

    @classmethod
    def from_row(cls, row: Any) -> PipelineStage:
        return cls(
            id=_get(row, "id"),
            key=_get(row, "key"),
            label=_get(row, "label"),
            position=_get(row, "position"),
            probability=_get(row, "probability"),
            outcome=_get(row, "outcome"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
            retired_at=_get(row, "retired_at"),
            replaced_by=_get(row, "replaced_by_key"),
        )


class SignalType(BaseModel):
    """One key of a workspace's signal vocabulary.

    ``key`` is what a signal stores in ``signal_type``; the label and description
    are the type's own and change without touching a signal.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")
    key: str = Field(description="What a signal stores in signal_type; stable, named in filters.")
    label: str
    description: str | None = Field(
        default=None,
        description="What the type means, for the agent deciding whether a fact is one.",
    )
    signal_count: int = Field(description="How many active signals carry this type.")
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = Field(
        default=None, description="When the type was retired; absent while it is live."
    )
    replaced_by: str | None = Field(
        default=None, description="The live type that takes a retired one's place."
    )

    @classmethod
    def from_row(cls, row: Any, *, signal_count: int) -> SignalType:
        return cls(
            id=_get(row, "id"),
            key=_get(row, "key"),
            label=_get(row, "label"),
            description=_get(row, "description"),
            signal_count=signal_count,
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
            retired_at=_get(row, "retired_at"),
            replaced_by=_get(row, "replaced_by_key"),
        )


class Signal(BaseModel):
    """A Signal wire contract (``sg_<ulid>``).

    A dated, URL-attested fact about an account: ``claim`` is the fact in one sentence, ``evidence_quote`` +
    ``source_url`` are what a reviewer checks it against.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")

    @computed_field(
        description="The signal's address (people://signals/…): cite the signal by it.",
    )
    @property
    def uri(self) -> str:
        return record_uri(self.id) or ""

    organization_id: str = Field(description="FK → Organization (the account); opaque id.")
    contact_id: str | None = Field(
        default=None, description="FK → Contact when the fact is about one person; opaque id."
    )
    claim: str = Field(
        description="One plain-English sentence, readable without opening the source."
    )
    evidence_quote: str = Field(description="Verbatim words from the source supporting the claim.")
    source_url: str = Field(description="Where it was read — shape-validated and normalized.")
    source_date: date | None = Field(
        default=None, description="The source's own date (ISO-8601), when it carries one."
    )
    event_date: date | None = Field(
        default=None,
        description=(
            "The date the event itself happened (ISO-8601), as the evidence states it; not "
            "the source's date. A month-only statement is the 1st of that month. Null when "
            "the source states none."
        ),
    )
    signal_type: str = Field(
        description="A key of the workspace's signal vocabulary (list_signal_types)."
    )
    found_by: str | None = Field(default=None, description="Provenance: which run, agent, human.")
    status: str = Field(description="active / withdrawn. There is no delete.")
    withdrawn_at: datetime | None = None
    withdrawn_reason: str | None = None
    evidence_for: list[str] = Field(
        default_factory=list,
        description=(
            'Whom the fact is evidence for: "workspace" (this workspace\'s own pipeline) '
            "and/or organization ids it was researched for. Empty when not stated."
        ),
    )
    created_at: datetime
    updated_at: datetime = Field(
        description="When the signal or what it is evidence for last changed (database clock)."
    )

    @classmethod
    def from_row(cls, row: Any, *, evidence_for: list[str] | None = None) -> Signal:
        """Map a SQLAlchemy row/mapping, and its live evidence-for parties, to the wire
        model (store seam)."""
        return cls(
            id=_get(row, "id"),
            organization_id=_get(row, "organization_id"),
            contact_id=_get(row, "contact_id"),
            claim=_get(row, "claim"),
            evidence_quote=_get(row, "evidence_quote"),
            source_url=_get(row, "source_url"),
            source_date=_get(row, "source_date"),
            event_date=_get(row, "event_date"),
            signal_type=_get(row, "signal_type"),
            found_by=_get(row, "found_by"),
            status=_get(row, "status"),
            withdrawn_at=_get(row, "withdrawn_at"),
            withdrawn_reason=_get(row, "withdrawn_reason"),
            evidence_for=list(evidence_for or []),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class ProofPoint(BaseModel):
    """A ProofPoint wire contract (``pp_<ulid>``).

    An attested claim about our own work. ``verification`` is the load-bearing
    field: ``fabricated-placeholder`` is a legitimate state (copy workshopped
    against an outcome we have not yet earned), and the set of them is the punch
    list of case studies still to obtain.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, opaque, public id.")

    @computed_field(
        description="The proof point's address (people://proof-points/…): cite it by it.",
    )
    @property
    def uri(self) -> str:
        return record_uri(self.id) or ""

    organization_id: str | None = Field(
        default=None,
        description="FK → Organization (the client), when they are in the CRM; opaque id.",
    )
    engagement: str | None = Field(
        default=None, description="Free-text client/engagement label when there is no org record."
    )
    claim: str = Field(description="The citable sentence.")
    metric: str | None = Field(default=None, description="The number, as it would be cited.")
    timeframe: str | None = Field(default=None, description="Over what period it was achieved.")
    verification: str = Field(
        description=(
            "verified / unverified / fabricated-placeholder. Only `verified` is "
            "evidence; `fabricated-placeholder` is a legitimate, marked state."
        )
    )
    attested_by: str | None = Field(default=None, description="Who stood behind it.")
    attested_at: datetime | None = Field(default=None, description="When they did.")
    scope_notes: str | None = Field(
        default=None, description="Naming permissions — NDA, attribution, which figures may ship."
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> ProofPoint:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        return cls(
            id=_get(row, "id"),
            organization_id=_get(row, "organization_id"),
            engagement=_get(row, "engagement"),
            claim=_get(row, "claim"),
            metric=_get(row, "metric"),
            timeframe=_get(row, "timeframe"),
            verification=_get(row, "verification"),
            attested_by=_get(row, "attested_by"),
            attested_at=_get(row, "attested_at"),
            scope_notes=_get(row, "scope_notes"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
        )


class FieldDef(BaseModel):
    """A custom field's definition.

    ``(entity, key)`` is its public identity: the key is what a filter, a saved
    view and a ``fields={…}`` write name.
    """

    model_config = ConfigDict(extra="forbid")

    entity: str = Field(
        description="contact / organization / opportunity — what the field describes."
    )
    key: str = Field(description="Stable identifier; the name used in fields={…} writes.")
    label: str = Field(description="Display label.")
    type: str = Field(description="text / number / boolean / date / select / multi_select / list.")
    options: list[str] | None = Field(
        default=None, description="The values a select or multi_select may hold."
    )
    description: str | None = Field(
        default=None, description="What the field means and how to fill it, for the agent."
    )
    defined_by: str | None = Field(default=None, description="Provenance: who defined it.")
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = Field(
        default=None, description="When the field was retired; absent while it is live."
    )
    replaced_by: str | None = Field(
        default=None,
        description="The field that replaces a retired one: its key on the same entity, "
        "else organization.<key> / contact.<key>.",
    )
    default_column: bool = Field(
        default=False, description="Whether the field opens as a column in its lists."
    )
    minimum: float | None = Field(
        default=None, description="The lowest value a number field accepts, inclusive."
    )
    maximum: float | None = Field(
        default=None, description="The highest value a number field accepts, inclusive."
    )

    @classmethod
    def from_row(cls, row: Any) -> FieldDef:
        """Map a SQLAlchemy row/mapping to the wire model (store seam)."""
        entity = _get(row, "entity")
        by_entity, by_key = _get(row, "replaced_by_entity"), _get(row, "replaced_by_key")
        replaced_by = None
        if by_key is not None:
            replaced_by = by_key if by_entity in (None, entity) else f"{by_entity}.{by_key}"
        return cls(
            entity=entity,
            key=_get(row, "key"),
            label=_get(row, "label"),
            type=_get(row, "type"),
            options=list(_get(row, "options") or []) or None,
            description=_get(row, "description"),
            defined_by=_get(row, "defined_by"),
            created_at=_get(row, "created_at"),
            updated_at=_get(row, "updated_at"),
            retired_at=_get(row, "retired_at"),
            replaced_by=replaced_by,
            default_column=bool(_get(row, "default_column")),
            minimum=_get(row, "minimum"),
            maximum=_get(row, "maximum"),
        )


def dump(model: BaseModel) -> dict[str, Any]:
    """Serialize a model to a dict with ``None`` fields omitted.

    Convenience wrapper for ``model.model_dump(exclude_none=True)`` — the
    documented wire shape.
    """
    return model.model_dump(exclude_none=True)


def dump_json(model: BaseModel) -> str:
    """Serialize a model to JSON with ``None`` fields omitted.

    Convenience wrapper for ``model.model_dump_json(exclude_none=True)``.
    """
    return model.model_dump_json(exclude_none=True)
