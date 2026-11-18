"""People data-access layer — CRUD + the idempotent dedupe/upsert contract.

This module is the **only door** to the People database: nothing reads the rows
directly. Every function:

  * takes ``conn`` — an :class:`AsyncConnection` already inside the runtime's
    :meth:`~mcp_people.runtime.Runtime.session`, so every query runs under RLS
    scoped to one ``(tenant_id, workspace_id)``. A forgotten ``WHERE`` cannot
    leak; the policy is default-deny.
  * takes ``ctx`` — the verified :class:`~mcp_people.runtime.Principal`. Identity columns
    (``tenant_id`` / ``workspace_id``) are stamped from ``ctx`` on every insert
    (the RLS ``WITH CHECK`` validates the active workspace); they are NEVER read
    from a tool argument, and NEVER returned on the wire.
  * returns Pydantic **wire models** (via ``Model.from_row``), never raw rows —
    ``tenant_id`` / ``workspace_id`` never cross the boundary.
  * records access on a split path. **Mutations** (find_or_create, update,
    log_interaction, set_*_dossier) append a durable :data:`people_audit` row via
    :func:`write_audit` — the change record. **Denied/error attempts** (including
    failed reads, written from the server layer) also land in the table — they are
    the security-relevant events. **Successful reads** do NOT write a table row:
    they emit a structured ``people_access`` log line instead, avoiding write
    amplification on a read-heavy loop and an unbounded table.

The load-bearing contract is **idempotent upsert**:
``find_or_create_contact`` / ``find_or_create_organization`` converge to one row
under the partial unique indexes ``uq_contact_email`` / ``uq_org_domain`` even
under a concurrent double-insert. We do NOT do a race-y check-then-insert: we
use ``INSERT ... ON CONFLICT DO NOTHING`` against the dedupe index and re-select,
so a lost race resolves to the row the winner inserted — one row, never a
duplicate, never a 500. See :func:`_upsert_returning` for the exact mechanism.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from pgkeyset import Listing, Sort
from sqlalchemy import (
    ColumnElement,
    Float,
    Numeric,
    RowMapping,
    Select,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
)
from sqlalchemy import Text as SAText
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_people import query
from mcp_people.db import dialect
from mcp_people.db.dialect import as_real, db_now, insert_for
from mcp_people.db.tables import (
    contact,
    external_ref,
    field_def,
    interaction,
    opportunity,
    organization,
    organization_edge,
    people_audit,
    pipeline_stage,
    proof_point,
    signal,
    signal_evidence_for,
)
from mcp_people.db.tables import signal_type as signal_type_table
from mcp_people.evidence import (
    ACTIVE,
    ANY_STATUS,
    SIGNAL_STATUSES,
    UNSTATED,
    VERIFICATION_STATES,
    VERIFIED,
    WITHDRAWN,
    WORKSPACE,
    dropped_hedge,
    normalize_source_url,
    parse_parties,
    validate_choice,
)
from mcp_people.fields import (
    EMAIL_STATUSES,
    ENTITIES,
    MAX_FIELDS_PER_ENTITY,
    FieldError,
    clean_bounds,
    clean_description,
    clean_label,
    clean_options,
    coerce_value,
    envelope,
    merge_options,
    normalize_entity,
    normalize_key,
    normalize_type,
    parse_input,
)
from mcp_people.keys import address_key, match_key
from mcp_people.lifecycle import LADDER, effective_status_sql
from mcp_people.models import (
    Contact,
    FieldDef,
    Interaction,
    Opportunity,
    Organization,
    OrganizationEdge,
    PipelineStage,
    ProofPoint,
    Signal,
    SignalType,
    dump,
)
from mcp_people.pipeline import (
    DEFAULT_STAGES,
    LOST,
    MAX_STAGES,
    OPEN,
    WON,
    clean_stage_label,
    normalize_outcome,
    normalize_probability,
    normalize_stage_key,
)
from mcp_people.refs import MAX_REFS_PER_LOOKUP, MAX_REFS_SHOWN, clean_refs
from mcp_people.runtime import Principal
from mcp_people.signal_types import (
    MAX_SIGNAL_TYPES,
    clean_signal_type_description,
    clean_signal_type_label,
    normalize_signal_type_key,
)

__all__ = [
    "ConflictError",
    "ContactNotFoundError",
    "Page",
    "OpportunityNotFoundError",
    "OrganizationNotFoundError",
    "ProofPointNotFoundError",
    "SIGNAL_CHANGE_SETTLE",
    "SIGNAL_WRITE_DEADLINE",
    "SignalNotFoundError",
    "SignalWithdrawnError",
    "add_signal_for",
    "archive_record",
    "count_by",
    "define_field",
    "describe_fields",
    "update_field",
    "live_replacement",
    "Replacement",
    "define_pipeline_stage",
    "get_pipeline",
    "update_pipeline_stage",
    "describe_signal_types",
    "define_signal_type",
    "update_signal_type",
    "find_or_create_contact",
    "find_or_create_opportunity",
    "find_or_create_organization",
    "get_contact",
    "get_opportunity",
    "get_organization",
    "get_proof_point",
    "get_signal",
    "link_similar_companies",
    "list_contacts",
    "list_interactions",
    "list_opportunities",
    "list_organizations",
    "list_proof_points",
    "list_signal_changes",
    "list_signals",
    "list_similar_companies",
    "log_interaction",
    "merge_contacts",
    "normalize_domain",
    "normalize_email",
    "page_contacts",
    "page_opportunities",
    "page_organizations",
    "pipeline_summary",
    "promote_organization",
    "record_proof_point",
    "record_signal",
    "remove_signal_for",
    "rollups",
    "set_contact_dossier",
    "set_opportunity_dossier",
    "set_opportunity_stage",
    "set_organization_dossier",
    "update_contact",
    "update_opportunity",
    "update_organization",
    "verify_proof_point",
    "withdraw_signal",
    "write_audit",
]

# Default + ceiling page sizes for the list/search reads.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

# --------------------------------------------------------------------------- #
# Input size caps (MVP row-bloat insurance)                                    #
# --------------------------------------------------------------------------- #
#
# The columns stay open ``Text`` (NO DB CHECK — a CHECK is schema rigidity and a
# migration every time a cap moves). We enforce length app-side at the start of
# every write so an over-length value raises ``ValueError`` — which the tool
# layer already maps to the structured ``invalid_request`` shape — rather than
# silently bloating a row. The DOSSIER cap is the one that matters (it is the
# only unbounded free-text store, 256 KiB); the rest are cheap insurance against
# a runaway field. Values are checked AFTER trimming where the write trims.

_CAP_NAME = 512
_CAP_EMAIL = 320  # RFC 5321 max addressable length
_CAP_DOMAIN = 253  # RFC 1035 max hostname length
_CAP_PHONE = 64
_CAP_TITLE = 256
_CAP_SHORT = 256  # the short open strings: industry/region/lead_source/status/source/kind
_CAP_SUMMARY = 1024
_CAP_BODY = 65536  # 64 KiB markdown detail
_CAP_DOSSIER = 262144  # 256 KiB markdown research store — the load-bearing cap
# Evidence records. A claim is one sentence and a quote is a
# passage, not a page: caps that a real signal never approaches, so an
# over-length value is a sign the caller is storing the article, not the evidence.
_CAP_CLAIM = 1024
_CAP_QUOTE = 4096
_CAP_URL = 2048
_CAP_SCOPE_NOTES = 4096


def _check_len(field: str, value: str | None, limit: int) -> None:
    """Raise ``ValueError`` if ``value`` exceeds ``limit`` UTF-8 bytes.

    A no-op for ``None``. The caps express a row-bloat budget in bytes/KiB, so the
    check measures the UTF-8 byte length — a multibyte string can be up to ~4×
    its character count. The message names the field and the limit but never the
    value, so an over-length input fails as a clean ``invalid_request`` at the
    tool boundary instead of a DB write that bloats a row.
    """
    if value is not None and len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds the maximum length of {limit} bytes")


class OrganizationNotFoundError(LookupError):
    """An organization id is not visible in this (account, workspace) — 404, not 403.

    Raised for both genuinely-absent ids and out-of-scope ids (RLS returns
    nothing): the caller can never tell the difference, so there is no existence
    oracle.
    """


class ContactNotFoundError(LookupError):
    """A contact id is not visible in this (account, workspace) — 404, not 403."""


class OpportunityNotFoundError(LookupError):
    """An opportunity id is not visible in this (account, workspace) — 404, not 403."""


class SignalNotFoundError(LookupError):
    """A signal id is not visible in this (account, workspace) — 404, not 403."""


class ProofPointNotFoundError(LookupError):
    """A proof-point id is not visible in this (account, workspace) — 404, not 403."""


class ConflictError(Exception):
    """A unique-dedupe collision on an explicit update — 409, not 500.

    Raised when an ``update_*`` would set an indexed dedupe column
    (``email`` / ``domain``) to a value already held by ANOTHER row in the same
    ``(tenant_id, workspace_id)`` — i.e. the partial unique index
    (``uq_contact_email`` / ``uq_org_domain``) would be violated. The bare
    ``UPDATE`` raises asyncpg ``UniqueViolationError`` → SQLAlchemy
    :class:`~sqlalchemy.exc.IntegrityError`; we translate it HERE into a domain
    conflict so the tool layer can return a structured ``conflict`` result
    instead of leaking an opaque 500 and the constraint/index name.

    The message names the offending FIELD (``email`` / ``domain``) only — NEVER
    the constraint or index name (that is a schema-shape leak).
    """


class SignalWithdrawnError(ConflictError):
    """A change to what a withdrawn signal is evidence for.

    The withdrawal already settles the signal, so its parties stay as they were.
    ``str(exc)`` is the field, ``evidence_for``; ``message`` and ``next_step`` say why
    and what to do instead.
    """

    def __init__(self, signal_id: str) -> None:
        super().__init__("evidence_for")
        self.signal_id = signal_id
        self.message = (
            f"Signal {signal_id!r} is withdrawn: the fact was judged wrong, so what it is "
            "evidence for no longer changes."
        )
        self.next_step = (
            "Record the fact again from a source that supports it, with evidence_for, or "
            "leave it withdrawn."
        )


class RefConflictError(ConflictError):
    """An external reference that would name two records.

    ``str(exc)`` stays the field, ``external_refs``, as every ConflictError's does; the
    ``message`` says which record holds what, and ``next_step`` how to settle it.
    ``retryable`` marks the one case a second pass resolves: this call created the record
    and a concurrent call attached the reference first, so a fresh lookup finds the winner.
    """

    def __init__(self, message: str, *, next_step: str, retryable: bool = False) -> None:
        super().__init__("external_refs")
        self.message = message
        self.next_step = next_step
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# Normalization helpers                                                        #
# --------------------------------------------------------------------------- #

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


def normalize_email(email: str | None) -> str | None:
    """Lowercase + trim an email to its dedupe form, or ``None`` if empty.

    This is the stored address; its dedupe key is :func:`~mcp_people.keys.address_key`
    of it (``email_key``, under ``uq_contact_email``). We trim surrounding whitespace
    and lowercase the whole address. We deliberately do
    NOT strip ``+tag`` parts or normalize the domain — two addresses that differ
    only by case are the same person; anything more aggressive risks merging
    distinct people.
    """
    if email is None:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


def name_from_email(email: str) -> str:
    """A display name for a contact known only by an email address.

    ``jane.doe+crm@acme.com`` → ``Jane Doe``. The local part is split on ``.``,
    ``_`` and ``-`` after dropping any ``+tag``; a local part with no letters is
    returned as written, and one with no words gives the whole address. The name is a
    placeholder the caller can correct with ``update_contact``, and it never
    affects dedupe, which keys on the email.
    """
    local = email.split("@", 1)[0].split("+", 1)[0]
    words = [w.capitalize() for w in re.split(r"[._-]+", local) if w]
    if not words:
        return email
    return " ".join(words) if any(c.isalpha() for c in local) else local


def normalize_domain(domain: str | None) -> str | None:
    """Reduce a domain or URL to its registrable, lowercased dedupe form.

    This is the stored domain; its dedupe key is :func:`~mcp_people.keys.address_key`
    of it (``domain_key``, under ``uq_org_domain``). Steps:
    strip a leading scheme (``https://``), drop a leading ``www.``, cut any
    path/query/port, then lowercase and trim a trailing dot. ``"https://www.Acme.com/about"``
    and ``"acme.com"`` collapse to the same key, so the retrying host loop
    dedupes the org. Returns ``None`` for empty input.
    """
    if domain is None:
        return None
    value = domain.strip()
    if not value:
        return None
    value = _SCHEME_RE.sub("", value)  # strip scheme://
    value = value.split("/", 1)[0]  # drop path/query/fragment
    value = value.split("@", 1)[-1]  # tolerate a user@host slip
    value = value.split(":", 1)[0]  # drop :port
    value = value.lower().strip().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return value or None


def _with_keys(table: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` plus the match key of each keyed column it writes.

    The key columns (``domain_key`` / ``name_key`` on organization, ``email_key`` /
    ``name_key`` on contact) are plain columns the database never computes, so every
    INSERT and UPDATE of ``organization`` or ``contact`` that sets ``name``,
    ``domain`` or ``email`` passes its values through here; a write that skipped it
    would leave a stale key the dedupe indexes and lookups read. A value for a
    column it does not set is left alone.
    """
    out = dict(values)
    for column, key, fold in _KEYED.get(table.name, ()):
        if column in out:
            out[key] = fold(out[column])
    return out


# Each keyed table's (source column, key column, key function).
_KEYED: dict[str, tuple[tuple[str, str, Any], ...]] = {
    "organization": (("name", "name_key", match_key), ("domain", "domain_key", address_key)),
    "contact": (("name", "name_key", match_key), ("email", "email_key", address_key)),
}


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #

# Successful reads emit a structured access log here rather than a table row.
_access_logger = structlog.stdlib.get_logger("mcp_people.audit")


def _log_access(
    ctx: Principal,
    *,
    entity: str,
    action: str,
    entity_id: str | None = None,
) -> None:
    """Emit one structured ``people_access`` log line for a SUCCESSFUL read.

    Successful reads do not write a :data:`people_audit` table row (that would be
    write amplification on a read-heavy loop and an unbounded table); they log
    instead. The line carries the verified scope and identifiers from ``ctx`` so
    the access is still attributable. Denied/error attempts and all mutations
    still go to the durable table.
    """
    _access_logger.info(
        "people_access",
        tenant_id=ctx.tenant_id,
        workspace_id=ctx.workspace_id,
        entity=entity,
        action=action,
        entity_id=entity_id,
        correlation_id=ctx.correlation_id,
    )


async def write_audit(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str,
    action: str,
    outcome: str,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one :data:`people_audit` row — the durable change/security record.

    Called for **mutations** (outcome ``ok``, from this module) and for
    **denied/error attempts** (from the server layer, including failed reads).
    Successful reads do NOT call this — they emit a structured access log via
    :func:`_log_access` instead (no table row).

    Identity (``tenant_id`` / ``workspace_id`` / ``user_id`` / ``correlation_id``)
    comes only from ``ctx``. The row is written inside the caller's already-scoped
    transaction, so it is subject to the same RLS policy as the data it describes.
    ``outcome`` ∈ {``ok``, ``denied``, ``error``}; ``entity`` ∈ {``organization``,
    ``contact``, ``interaction``}.

    Optional ``detail`` stores structured context for the change in the JSONB
    ``detail`` column (e.g. what a merge moved where). It must carry ONLY ids,
    counts, and field NAMES — never PII/values.

    One value is exempt, and the test for adding another is the same: a write
    that DESTROYS a value may record it, because the log is then the only place
    it survives. ``merge_organizations`` records ``source_domain`` for that
    reason — archiving clears the source's domain, and ``fields_filled`` names
    fields without their contents, so the discarded domain would otherwise be
    unrecoverable. A value that still exists on the row is a second copy and
    does not qualify.
    """
    await conn.execute(
        people_audit.insert().values(
            tenant_id=ctx.tenant_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            outcome=outcome,
            correlation_id=ctx.correlation_id,
            detail=detail,
        )
    )


# --------------------------------------------------------------------------- #
# Small internal helpers                                                       #
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(value: str | None) -> str | None:
    """Trim a free-text value; treat empty/whitespace as ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Coerce a tz-naive datetime to UTC; pass tz-aware (and ``None``) through.

    A client may send a tz-naive ``occurred_at``/``follow_up_at``. Without this,
    Postgres interprets it in the session TZ, silently shifting the instant. We
    pin the contract: naive means UTC.
    """
    return dt.replace(tzinfo=UTC) if (dt is not None and dt.tzinfo is None) else dt


async def _one(conn: AsyncConnection, stmt: Select) -> RowMapping | None:
    row = (await conn.execute(stmt)).mappings().first()
    return row


async def _all(conn: AsyncConnection, stmt: Select) -> Sequence[RowMapping]:
    return (await conn.execute(stmt)).mappings().all()


async def _org_row(conn: AsyncConnection, org_id: str) -> RowMapping:
    row = await _one(
        conn, select(organization).where(organization.c.id == org_id, _live(organization))
    )
    if row is None:
        raise OrganizationNotFoundError(org_id)
    return row


async def _contact_row(conn: AsyncConnection, contact_id: str) -> RowMapping:
    row = await _one(conn, _contact_select().where(contact.c.id == contact_id, _live(contact)))
    if row is None:
        raise ContactNotFoundError(contact_id)
    return row


def _check_stage(status: str | None) -> None:
    """Refuse ``archived`` as a contact's stage: archiving is ``archived_at``."""
    if status is not None and status.strip().lower() == "archived":
        raise FieldError(
            "archived is not a stage: status holds the contact's stage, and archiving is "
            "its own flag",
            next_step="Archive the contact with archive_contact(contact_id=...) instead.",
        )


def _merge_empty_only(existing: RowMapping, candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the fields where ``existing`` is empty and ``candidate`` has a value.

    The dedupe-merge rule: a match **fills empty fields only**; it
    never overwrites data the row already has. Use :func:`update_*` for explicit
    edits.
    """
    updates: dict[str, Any] = {}
    for key, new_value in candidate.items():
        if new_value is None:
            continue
        current = existing.get(key)
        if current is None or (isinstance(current, str) and current.strip() == ""):
            updates[key] = new_value
    return updates


async def _upsert_returning(
    conn: AsyncConnection,
    *,
    table: Any,
    insert_values: dict[str, Any],
    conflict_index_elements: list[Any],
    conflict_where: Any,
    reselect_predicate: Any,
) -> tuple[RowMapping, bool]:
    """Race-safe idempotent insert: ``INSERT ... ON CONFLICT DO NOTHING`` then re-select.

    This is the mechanism behind the load-bearing idempotent upsert.
    We never check-then-insert (two requests could both see "absent" and both
    insert, double-writing under the unique index). Instead:

      1. ``INSERT ... ON CONFLICT (<dedupe index>) DO NOTHING RETURNING *`` — at
         most one of any number of concurrent inserts wins; the losers no-op
         instead of raising.
      2. If the insert returned a row, we created it. If it returned nothing, a
         concurrent (or prior) insert already holds the dedupe key, so we
         **re-select** by the same key and return that row.

    Either way the caller converges to exactly one row, with no duplicate and no
    ``IntegrityError`` surfacing as a 500. The conflict target is the partial
    unique index (``conflict_where`` mirrors the index's ``postgresql_where``),
    so the upsert only dedupes on a populated key. On a table whose rows a delete marks,
    every unique index also leaves marked rows out (``deleted_at IS NULL``), so the conflict
    target and the re-select do too: a deleted record never matches.
    """
    if "deleted_at" in table.c:
        conflict_where = (
            _live(table) if conflict_where is None else and_(conflict_where, _live(table))
        )
        reselect_predicate = and_(reselect_predicate, _live(table))
    stmt = (
        insert_for(conn.dialect.name, table)
        .values(**_with_keys(table, insert_values))
        .on_conflict_do_nothing(
            index_elements=conflict_index_elements,
            index_where=conflict_where,
        )
        .returning(table)
    )
    inserted = (await conn.execute(stmt)).mappings().first()
    if inserted is not None:
        return inserted, True  # we created the row
    # Lost the race (or a prior call already inserted): re-select the winner.
    existing = await _one(conn, select(table).where(reselect_predicate))
    if existing is None:  # pragma: no cover - only if the row vanished mid-txn
        raise RuntimeError("upsert conflict but the conflicting row is not visible")
    return existing, False  # matched an existing row


# --------------------------------------------------------------------------- #
# Organization writes                                                          #
# --------------------------------------------------------------------------- #


async def _promote_if_discovered(
    conn: AsyncConnection, ctx: Principal, row: RowMapping
) -> tuple[RowMapping, bool]:
    """Flip a matched org's ``status`` ``discovered`` → ``active``; re-select.

    The symmetric half of the discovered-org guardrail: explicit
    creation/addition of an org is a promotion signal — an agent or human curating
    a lookalike by hand means it is real, so it enters the default views. Returns
    ``(row, promoted)`` — ``row`` unchanged (and ``promoted=False``) unless it was
    ``discovered``, in which case the freshly-flipped row and ``True``. Editing /
    researching is NOT a trigger — only find_or_create paths and promote_organization
    call this.

    A real promotion writes a durable ``promote`` audit row (the same record
    :func:`promote_organization` writes), so a match-triggered flip is as observable
    as an explicit one.
    """
    if row["status"] != "discovered":
        return row, False
    await conn.execute(
        organization.update()
        .where(organization.c.id == row["id"])
        .values(status="active", updated_at=_now())
    )
    await write_audit(
        conn, ctx, entity="organization", action="promote", outcome="ok", entity_id=row["id"]
    )
    return await _org_row(conn, row["id"]), True


async def find_or_create_organization(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    name: str,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    source: str | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
    external_refs: Sequence[str] | None = None,
) -> tuple[Organization, str, bool]:
    """Dedupe-or-create an organization, then fill its custom ``fields``.

    The dedupe contract is :func:`_find_or_create_organization`'s, with one step before
    it: an organization one of ``external_refs`` already names is the match,
    whatever its domain or name say. The refs it does not hold yet are then attached.
    ``fields`` follows the same fill-empty rule as every other argument here: a key the
    organization already holds is left alone (``update_organization`` overwrites).
    """
    refs = clean_refs(external_refs)
    patch = await _field_fill_patch(conn, entity="organization", submitted=fields, by=fields_by)
    for attempt in (1, 2):
        try:
            async with conn.begin_nested():
                held = await _record_by_refs(conn, "organization", refs)
                org, action, promoted = await _find_or_create_organization(
                    conn,
                    ctx,
                    name=name,
                    domain=domain,
                    website=website,
                    industry=industry,
                    region=region,
                    source=source,
                    match_id=held,
                )
                if refs and held is None and action == "matched" and domain is None:
                    await _refuse_name_only_match(
                        conn, entity="organization", record_id=org.id, name=org.name, refs=refs
                    )
                await _attach_refs(
                    conn,
                    ctx,
                    entity="organization",
                    record_id=org.id,
                    refs=refs,
                    created=action == "created",
                )
            break
        except RefConflictError as exc:
            if attempt == 1 and exc.retryable:
                continue
            raise
    new_keys = [k for k in patch if k not in (org.fields or {})]
    if new_keys:
        await conn.execute(
            organization.update()
            .where(organization.c.id == org.id)
            .values(
                fields=_fields_filled(conn, organization, {k: patch[k] for k in new_keys}),
                updated_at=_now(),
            )
        )
        await write_audit(
            conn,
            ctx,
            entity="organization",
            action="set_fields",
            outcome="ok",
            entity_id=org.id,
            detail={"fields": sorted(new_keys)},
        )
        org = Organization.from_row(
            await _org_row(conn, org.id), retired=await _retired_for(conn, "organization")
        )
    return org, action, promoted


async def _find_or_create_organization(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    name: str,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    source: str | None = None,
    match_id: str | None = None,
) -> tuple[Organization, str, bool]:
    """Dedupe-or-create an organization. Returns ``(Organization, action, promoted)``.

    Dedupe order:
      1. by normalized ``domain`` (the real identity of a company) via
         ``uq_org_domain``;
      2. else by ``name``'s match key (:func:`~mcp_people.keys.match_key`), against every organization in
         scope — including ones that DO hold a domain, so a domain-less call for
         a company already on file finds it instead of minting a second row.

    On a match we **fill empty fields only** and return ``action="matched"``; on
    a create, ``action="created"``.

    **Both paths are idempotent under a unique index** via ON CONFLICT (see
    :func:`_upsert_returning`) — ``uq_org_domain`` for the first,
    ``uq_org_name_when_domainless`` for the second — so any number of concurrent
    calls for one company converge on one row. A read-then-insert is not a lock:
    a batch that resolves several contacts at one company would have every call
    see "absent" and every call insert. Never put a bare INSERT on either path.

    Note the asymmetry the second index's partiality creates: it constrains only
    domain-less rows, so it cannot stop two *domained* rows that share a name
    from existing — correctly, since those are different companies. Step 2's read
    is what keeps a domain-less call from adding to them, and a read is
    best-effort against a concurrent domained create.

    **Promotion:** a match on a ``discovered`` lookalike flips its
    ``status`` to ``active`` — explicit creation by a human or agent outweighs
    auto-discovery — and the third return element ``promoted`` is ``True`` so the
    caller sees the mutation. A create is never a promotion (``promoted=False``).
    """
    _check_len("name", name, _CAP_NAME)
    _check_len("domain", domain, _CAP_DOMAIN)
    _check_len("website", website, _CAP_SHORT)
    _check_len("industry", industry, _CAP_SHORT)
    _check_len("region", region, _CAP_SHORT)
    _check_len("source", source, _CAP_SHORT)
    name = name.strip()
    norm_domain = normalize_domain(domain)
    candidate = {
        "domain": norm_domain,
        "website": _clean(website),
        "industry": _clean(industry),
        "region": _clean(region),
        "source": _clean(source),
    }

    # Path 0: an external reference already names the organization. It comes
    # before the domain upsert, which would mint a second row for a company whose domain
    # changed since the last pull. The fill is the match's usual one, except that a domain
    # another organization holds is a conflict naming both, not a bare collision.
    if match_id is not None:
        row = await _org_row(conn, match_id)
        if row["domain"] is None and norm_domain is not None:
            other = await _one(
                conn,
                select(organization.c.id).where(
                    organization.c.domain_key == address_key(norm_domain), _live(organization)
                ),
            )
            if other is not None:
                raise RefConflictError(
                    f"the reference names {match_id}, and the domain {norm_domain} belongs to "
                    f"{other['id']}",
                    next_step="If they are one organization, join them with merge_organizations.",
                )
        row = await _fill_empty(conn, organization, row, candidate)
        row, promoted = await _promote_if_discovered(conn, ctx, row)
        await write_audit(
            conn,
            ctx,
            entity="organization",
            action="find_or_create",
            outcome="ok",
            entity_id=row["id"],
        )
        return (
            Organization.from_row(row, retired=await _retired_for(conn, "organization")),
            "matched",
            promoted,
        )

    # Path 1: dedupe by normalized domain (the unique index). Idempotent insert.
    if norm_domain is not None:
        now = _now()
        row, created = await _upsert_returning(
            conn,
            table=organization,
            insert_values={
                "id": _new_org_id(),
                **ctx.identity_columns(),
                "name": name,
                "domain": norm_domain,
                "website": candidate["website"],
                "industry": candidate["industry"],
                "region": candidate["region"],
                "source": candidate["source"],
                "created_at": now,
                "updated_at": now,
            },
            conflict_index_elements=[
                organization.c.tenant_id,
                organization.c.workspace_id,
                organization.c.domain_key,
            ],
            conflict_where=organization.c.domain.isnot(None),
            reselect_predicate=and_(
                organization.c.tenant_id == ctx.tenant_id,
                organization.c.workspace_id == ctx.workspace_id,
                organization.c.domain_key == address_key(norm_domain),
            ),
        )
        action = "created" if created else "matched"
        promoted = False
        if not created:
            row = await _fill_empty(conn, organization, row, candidate)
            row, promoted = await _promote_if_discovered(conn, ctx, row)
        await write_audit(
            conn,
            ctx,
            entity="organization",
            action="find_or_create",
            outcome="ok",
            entity_id=row["id"],
        )
        return (
            Organization.from_row(row, retired=await _retired_for(conn, "organization")),
            action,
            promoted,
        )

    # Path 2: no domain → match on the normalized name, across EVERY org in
    # scope. Deliberately not restricted to domain-less rows: calling without a
    # domain for a company already on file WITH one must find it, or every such
    # call would mint a second row for a company we can already identify. This
    # read cannot be folded into the upsert below, whose index only covers the
    # domain-less rows.
    existing = await _one(
        conn,
        select(organization)
        .where(organization.c.name_key == match_key(name), _live(organization))
        .order_by(organization.c.created_at, organization.c.id),
    )
    if existing is not None:
        row = await _fill_empty(conn, organization, existing, candidate)
        row, promoted = await _promote_if_discovered(conn, ctx, row)
        await write_audit(
            conn,
            ctx,
            entity="organization",
            action="find_or_create",
            outcome="ok",
            entity_id=row["id"],
        )
        return (
            Organization.from_row(row, retired=await _retired_for(conn, "organization")),
            "matched",
            promoted,
        )

    # Nothing on file under that name: create, idempotently. Same
    # upsert-then-reselect as the domain path above, here against
    # `uq_org_name_when_domainless`. It has to be the upsert and not a bare
    # INSERT, because the read above is not a lock: concurrent calls naming the
    # same absent org — an agent resolving several contacts at one company in a
    # single batch is the ordinary case — all see "absent" together, and a bare
    # INSERT would make one company several rows. With the upsert at most one
    # insert wins and the losers re-select it.
    now = _now()
    row, created = await _upsert_returning(
        conn,
        table=organization,
        insert_values={
            "id": _new_org_id(),
            **ctx.identity_columns(),
            "name": name,
            "domain": None,
            "website": candidate["website"],
            "industry": candidate["industry"],
            "region": candidate["region"],
            "source": candidate["source"],
            "created_at": now,
            "updated_at": now,
        },
        conflict_index_elements=[
            organization.c.tenant_id,
            organization.c.workspace_id,
            organization.c.name_key,
        ],
        conflict_where=organization.c.domain.is_(None),
        reselect_predicate=and_(
            organization.c.tenant_id == ctx.tenant_id,
            organization.c.workspace_id == ctx.workspace_id,
            organization.c.name_key == match_key(name),
            organization.c.domain.is_(None),
        ),
    )
    action = "created" if created else "matched"
    promoted = False
    if not created:
        row = await _fill_empty(conn, organization, row, candidate)
        row, promoted = await _promote_if_discovered(conn, ctx, row)
    await write_audit(
        conn,
        ctx,
        entity="organization",
        action="find_or_create",
        outcome="ok",
        entity_id=row["id"],
    )
    return (
        Organization.from_row(row, retired=await _retired_for(conn, "organization")),
        action,
        promoted,
    )


async def update_organization(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    org_id: str,
    name: str | None = None,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    source: str | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
    add_external_refs: Sequence[str] | None = None,
    remove_external_refs: Sequence[str] | None = None,
) -> Organization:
    """Partial update. Omitted (``None``) fields are unchanged; bumps ``updated_at``.

    ``add_external_refs`` attaches references to this organization (one another record
    holds is a conflict, never moved) and ``remove_external_refs`` detaches the ones it
    holds.

    Only the non-``None`` values passed are applied; an omitted field is left as
    it is (the public tool surface has no way to clear a core field). Editing is
    NOT a promotion trigger: a ``discovered`` lookalike stays
    ``discovered`` through an ``update_organization`` — annotating a lookalike is
    evaluation, not commitment. (Promotion happens only via find_or_create /
    promote_organization.)

    ``fields`` writes custom fields key by key: each key given
    replaces that key's value, a ``None`` value removes it, and every other key is
    untouched.
    """
    _check_len("name", name, _CAP_NAME)
    _check_len("domain", domain, _CAP_DOMAIN)
    _check_len("website", website, _CAP_SHORT)
    _check_len("industry", industry, _CAP_SHORT)
    _check_len("region", region, _CAP_SHORT)
    _check_len("source", source, _CAP_SHORT)
    await _org_row(conn, org_id)  # 404 if out of scope
    candidate = {
        "name": name.strip() if isinstance(name, str) else name,
        "domain": normalize_domain(domain) if (domain is not None) else None,
        "website": _clean(website),
        "industry": _clean(industry),
        "region": _clean(region),
        "source": _clean(source),
    }
    updates = _partial_updates(candidate)
    patch, removed = await _field_patch(conn, entity="organization", submitted=fields, by=fields_by)
    if patch or removed:
        updates["fields"] = _fields_written(conn, organization, patch, removed)
    if updates:
        updates["updated_at"] = _now()
        try:
            await conn.execute(
                organization.update()
                .where(organization.c.id == org_id)
                .values(**_with_keys(organization, updates))
            )
        except IntegrityError as exc:
            # A new ``domain`` OR a new ``name`` collided with another org in this
            # (account, workspace) — the org has two partial unique indexes now, and a rename
            # onto a domain-less namesake trips the second. Name whichever field the
            # caller actually sent; never the index name (schema leak).
            raise ConflictError(_conflicting_field(updates)) from exc
    await _detach_refs(
        conn, ctx, entity="organization", record_id=org_id, refs=clean_refs(remove_external_refs)
    )
    await _attach_refs(
        conn, ctx, entity="organization", record_id=org_id, refs=clean_refs(add_external_refs)
    )
    row = await _org_row(conn, org_id)
    await write_audit(
        conn,
        ctx,
        entity="organization",
        action="update",
        outcome="ok",
        entity_id=org_id,
        detail=_fields_detail(patch, removed),
    )
    return Organization.from_row(row, retired=await _retired_for(conn, "organization"))


# --------------------------------------------------------------------------- #
# Contact writes                                                               #
# --------------------------------------------------------------------------- #


async def _promote_contact_org(conn: AsyncConnection, ctx: Principal, org_id: str | None) -> bool:
    """Promote the org a contact just wired to, if it is a ``discovered`` lookalike.

    Attaching a real contact to a discovered org is a promotion signal:
    the org enters the default CRM views. A no-op (returns ``False``) when there is
    no wired org, it is out of scope, or it is already ``active`` / off-status.
    Reuses :func:`_promote_if_discovered`.
    """
    if org_id is None:
        return False
    row = await _one(conn, select(organization).where(organization.c.id == org_id))
    if row is None:
        return False
    _fresh, promoted = await _promote_if_discovered(conn, ctx, row)
    return promoted


async def find_or_create_contact(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    organization_id: str | None = None,
    organization_name: str | None = None,
    organization_domain: str | None = None,
    lead_source: str | None = None,
    source: str | None = None,
    status: str | None = None,
    email_status: str | None = None,
    email_checked_at: datetime | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
    external_refs: Sequence[str] | None = None,
) -> tuple[Contact, str, bool]:
    """Dedupe-or-create a contact, then fill its email status and custom ``fields``.

    The dedupe contract is :func:`_find_or_create_contact`'s, with one step before it: a
    contact one of ``external_refs`` already names is the match. The refs it
    does not hold yet are then attached. ``email_status`` and ``fields`` follow the same
    fill-empty rule as every other argument here: a status the contact already has, or a
    key it already holds, is left alone (``update_contact`` overwrites).
    """
    refs = clean_refs(external_refs)
    graded = _email_grade(email_status, email_checked_at)
    patch = await _field_fill_patch(conn, entity="contact", submitted=fields, by=fields_by)
    for attempt in (1, 2):
        try:
            async with conn.begin_nested():
                held = await _record_by_refs(conn, "contact", refs)
                found, action, org_promoted = await _find_or_create_contact(
                    conn,
                    ctx,
                    name=name,
                    email=email,
                    phone=phone,
                    title=title,
                    organization_id=organization_id,
                    organization_name=organization_name,
                    organization_domain=organization_domain,
                    lead_source=lead_source,
                    source=source,
                    status=status,
                    match_id=held,
                )
                if refs and held is None and action == "matched" and normalize_email(email) is None:
                    await _refuse_name_only_match(
                        conn, entity="contact", record_id=found.id, name=found.name, refs=refs
                    )
                await _attach_refs(
                    conn,
                    ctx,
                    entity="contact",
                    record_id=found.id,
                    refs=refs,
                    created=action == "created",
                )
            break
        except RefConflictError as exc:
            if attempt == 1 and exc.retryable:
                continue
            raise
    sets: dict[str, Any] = {}
    if graded is not None:
        if found.email is None:
            raise ValueError("email_status grades an email address, and this contact has none")
        # A submitted email is the dedupe key, so `found` holds exactly that
        # address; with none submitted, the grade is for the one it already has.
        if found.email_status is None:
            sets["email_status"], sets["email_checked_at"] = graded
    new_keys = [k for k in patch if k not in (found.fields or {})]
    if new_keys:
        sets["fields"] = _fields_filled(conn, contact, {k: patch[k] for k in new_keys})
    if sets:
        await conn.execute(
            contact.update().where(contact.c.id == found.id).values(**sets, updated_at=_now())
        )
        await write_audit(
            conn,
            ctx,
            entity="contact",
            action="set_fields",
            outcome="ok",
            entity_id=found.id,
            detail={"fields": sorted([*new_keys, *(k for k in sets if k == "email_status")])},
        )
        found = Contact.from_row(
            await _contact_row(conn, found.id), retired=await _retired_for(conn, "contact")
        )
    return found, action, org_promoted


async def _find_or_create_contact(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    organization_id: str | None = None,
    organization_name: str | None = None,
    organization_domain: str | None = None,
    lead_source: str | None = None,
    source: str | None = None,
    status: str | None = None,
    match_id: str | None = None,
) -> tuple[Contact, str, bool]:
    """Dedupe-or-create a contact — the idempotent upsert the ecosystem leans on.

    Dedupe order:
      1. by normalized ``email`` (primary key) via ``uq_contact_email``;
      2. else by ``name`` + resolved ``organization_id`` within scope.

    Org wiring: if ``organization_name`` is given without ``organization_id``,
    the org is resolve-or-created (:func:`find_or_create_organization`) and its
    **id** wired onto the contact (the ``works_at`` edge — an immutable id, never
    a name). Returns ``(Contact, action, org_promoted)`` with ``action`` ∈
    {created, matched}.

    **Org promotion:** when the contact resolves/wires to an existing
    ``discovered`` org, that org is promoted to ``active`` — attaching a real
    contact means the org is real — and ``org_promoted`` is ``True`` so the caller
    sees the mutation. A freshly-created org is already ``active``, so wiring to it
    is not a promotion.

    Idempotency: re-running with the same email converges to one row under the
    unique index via ON CONFLICT (see :func:`_upsert_returning`); a concurrent
    double-insert resolves to a single row, never a duplicate, never a 500.

    ``name`` may be omitted when ``email`` is given: a match never rewrites the
    name, and a create takes :func:`name_from_email`. With neither, there is
    nothing to dedupe on or display, so it raises ``ValueError``.
    """
    _check_len("name", name, _CAP_NAME)
    _check_len("email", email, _CAP_EMAIL)
    _check_len("phone", phone, _CAP_PHONE)
    _check_len("title", title, _CAP_TITLE)
    _check_len("organization_name", organization_name, _CAP_NAME)
    _check_len("organization_domain", organization_domain, _CAP_DOMAIN)
    _check_len("lead_source", lead_source, _CAP_SHORT)
    _check_len("source", source, _CAP_SHORT)
    _check_len("status", status, _CAP_SHORT)
    _check_stage(status)
    norm_email = normalize_email(email)
    name = _clean(name)
    if name is None and match_id is None:
        if norm_email is None:
            raise ValueError("find_or_create_contact needs a name or an email")
        name = name_from_email(norm_email)

    # Resolve the works_at edge to an immutable org id (never store a name).
    # S4: resolution is LAZY. A directly-given organization_id is validated
    # eagerly (cheap, creates nothing). An organization_name (which may CREATE an
    # org) is resolved only when the edge is actually wired onto a row — i.e. on a
    # genuine create or when filling an empty org on a matched contact — so a
    # contact that turns out to match an existing row never orphans a fresh org.
    # An organization_domain resolves the same way, by the org's real identity; a
    # created org is named by organization_name when given, else by its domain, and
    # carries the contact's source.
    org_domain = normalize_domain(organization_domain)
    if organization_id is not None:
        await _org_row(conn, organization_id)  # validate in-scope; 404 otherwise

    _org_cache: list[str | None] = []
    # Tracks whether resolving an org BY NAME promoted a discovered lookalike (the
    # nested find_or_create_organization surfaces its own promotion). ORed with the
    # direct-id promotion below so org_promoted is correct on every path.
    _org_promoted_by_name = [False]

    async def resolve_org() -> str | None:
        if organization_id is not None:
            return organization_id
        if _org_cache:
            return _org_cache[0]
        org_name = _clean(organization_name) or org_domain
        resolved: str | None = None
        if org_name is not None:
            org, _action, promoted = await find_or_create_organization(
                conn, ctx, name=org_name, domain=org_domain, source=source
            )
            resolved = org.id
            if promoted:
                _org_promoted_by_name[0] = True
        _org_cache.append(resolved)
        return resolved

    def candidate_without_org() -> dict[str, str | None]:
        return {
            "email": norm_email,
            "phone": _clean(phone),
            "title": _clean(title),
            "lead_source": _clean(lead_source),
            "source": _clean(source),
        }

    # Path 0: an external reference already names the contact. It comes before
    # the email upsert, which would mint a second row for a person whose address changed
    # since the last pull. Otherwise it is the email path's match: fill what is empty and
    # wire the organization lazily. An email another contact holds is a conflict naming
    # both, not a bare collision.
    if match_id is not None:
        existing = await _contact_row(conn, match_id)
        if existing["email"] is None and norm_email is not None:
            other = await _one(
                conn,
                select(contact.c.id).where(
                    contact.c.email_key == address_key(norm_email), _live(contact)
                ),
            )
            if other is not None:
                raise RefConflictError(
                    f"the reference names {match_id}, and the email {norm_email} belongs to "
                    f"{other['id']}",
                    next_step="If they are one person, join them with merge_contacts.",
                )
        fill = candidate_without_org()
        if existing["organization_id"] is None:
            org_to_wire = await resolve_org()
            if org_to_wire is not None:
                fill["organization_id"] = org_to_wire
        row = await _fill_empty(conn, contact, existing, fill)
        await write_audit(
            conn, ctx, entity="contact", action="find_or_create", outcome="ok", entity_id=row["id"]
        )
        final = await _contact_row(conn, row["id"])
        org_promoted = (
            await _promote_contact_org(conn, ctx, final["organization_id"])
            or _org_promoted_by_name[0]
        )
        return (
            Contact.from_row(final, retired=await _retired_for(conn, "contact")),
            "matched",
            org_promoted,
        )
    assert name is not None  # only a reference match goes without one

    # Path 1: dedupe by normalized email (the unique index). Idempotent insert.
    if norm_email is not None:
        now = _now()
        # S4: insert WITHOUT the org-by-name edge so a lost ON CONFLICT race (the
        # row already exists with an org) never orphans a freshly-created org. We
        # wire/fill the org AFTER we know created-vs-matched, resolving the name
        # only then — so an org is created only when something will reference it.
        insert_values = {
            "id": _new_contact_id(),
            **ctx.identity_columns(),
            "name": name,
            "email": norm_email,
            "phone": _clean(phone),
            "title": _clean(title),
            "organization_id": organization_id,  # a direct id is already validated
            "lead_source": _clean(lead_source),
            "source": _clean(source),
            "created_at": now,
            "updated_at": now,
        }
        clean_status = _clean(status)
        if clean_status is not None:
            insert_values["status"] = clean_status
        row, created = await _upsert_returning(
            conn,
            table=contact,
            insert_values=insert_values,
            conflict_index_elements=[
                contact.c.tenant_id,
                contact.c.workspace_id,
                contact.c.email_key,
            ],
            conflict_where=contact.c.email.isnot(None),
            reselect_predicate=and_(
                contact.c.tenant_id == ctx.tenant_id,
                contact.c.workspace_id == ctx.workspace_id,
                contact.c.email_key == address_key(norm_email),
            ),
        )
        action = "created" if created else "matched"
        # On a MATCH: fill empty non-org fields (never overwrites), and wire the
        # works_at edge if the existing org is empty — from a directly-given
        # (validated) id OR a lazily-resolved name. resolve_org() runs only when an
        # org will be referenced, so a by-name org is never orphaned (S4).
        if not created:
            fill = candidate_without_org()
            if row["organization_id"] is None:
                org_to_wire = await resolve_org()
                if org_to_wire is not None:
                    fill["organization_id"] = org_to_wire
            row = await _fill_empty(conn, contact, row, fill)
        elif row["organization_id"] is None:
            # Created without an org (by-name or none given) — resolve and wire.
            org_to_wire = await resolve_org()
            if org_to_wire is not None:
                row = await _fill_empty(conn, contact, row, {"organization_id": org_to_wire})
        await write_audit(
            conn,
            ctx,
            entity="contact",
            action="find_or_create",
            outcome="ok",
            entity_id=row["id"],
        )
        # Re-select through _contact_select so the returned wire model carries the
        # derived effective_status (the upsert/fill rows don't compute it).
        final = await _contact_row(conn, row["id"])
        org_promoted = (
            await _promote_contact_org(conn, ctx, final["organization_id"])
            or _org_promoted_by_name[0]
        )
        return (
            Contact.from_row(final, retired=await _retired_for(conn, "contact")),
            action,
            org_promoted,
        )

    # Path 2: no email → dedupe by name + organization_id within scope.
    # Resolve the org first only when a direct id was given; with only a NAME we
    # defer so a matched contact does not orphan a freshly-created org.
    name_pred = and_(contact.c.name_key == match_key(name), _live(contact))
    if organization_id is None and _clean(organization_name) is None and org_domain is None:
        # No org edge at all → dedupe against contacts that also have no org.
        existing = await _one(
            conn,
            select(contact).where(and_(name_pred, contact.c.organization_id.is_(None))),
        )
    else:
        # An org is determined — a direct id, or one resolved from the name
        # (resolve_org() may create it; it is referenced either way, so never
        # orphaned). Match in priority order:
        #   (a) an exact name + same-org contact (the precise existing row); else
        #   (b) the earlier NAME-ONLY stub (organization_id IS NULL) — ADOPT it
        #       and fill its empty org with the target. This is the live dedupe
        #       bug: a stub created by an earlier name-only call was invisible to
        #       an exact-org match, so a second row was created.
        target_org = await resolve_org()
        existing = await _one(
            conn,
            select(contact).where(and_(name_pred, contact.c.organization_id == target_org)),
        )
        if existing is None:
            existing = await _one(
                conn,
                select(contact).where(and_(name_pred, contact.c.organization_id.is_(None))),
            )
    if existing is not None:
        fill_candidate = {
            **candidate_without_org(),
            "organization_id": await resolve_org(),
        }
        row = await _fill_empty(conn, contact, existing, fill_candidate)
        await write_audit(
            conn,
            ctx,
            entity="contact",
            action="find_or_create",
            outcome="ok",
            entity_id=row["id"],
        )
        final = await _contact_row(conn, row["id"])
        org_promoted = (
            await _promote_contact_org(conn, ctx, final["organization_id"])
            or _org_promoted_by_name[0]
        )
        return (
            Contact.from_row(final, retired=await _retired_for(conn, "contact")),
            "matched",
            org_promoted,
        )

    # Genuine create (no email key to race on).
    now = _now()
    contact_id = _new_contact_id()
    values = {
        "id": contact_id,
        **ctx.identity_columns(),
        "name": name,
        "email": None,
        "phone": _clean(phone),
        "title": _clean(title),
        "organization_id": await resolve_org(),
        "lead_source": _clean(lead_source),
        "source": _clean(source),
        "created_at": now,
        "updated_at": now,
    }
    clean_status = _clean(status)
    if clean_status is not None:
        values["status"] = clean_status
    await conn.execute(contact.insert().values(**_with_keys(contact, values)))
    row = await _contact_row(conn, contact_id)
    await write_audit(
        conn,
        ctx,
        entity="contact",
        action="find_or_create",
        outcome="ok",
        entity_id=contact_id,
    )
    org_promoted = (
        await _promote_contact_org(conn, ctx, row["organization_id"]) or _org_promoted_by_name[0]
    )
    return (
        Contact.from_row(row, retired=await _retired_for(conn, "contact")),
        "created",
        org_promoted,
    )


async def update_contact(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    contact_id: str,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    organization_id: str | None = None,
    lead_source: str | None = None,
    status: str | None = None,
    source: str | None = None,
    email_status: str | None = None,
    email_checked_at: datetime | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
    add_external_refs: Sequence[str] | None = None,
    remove_external_refs: Sequence[str] | None = None,
) -> Contact:
    """Partial update. Omitted (``None``) fields are unchanged; bumps ``updated_at``.

        ``add_external_refs`` attaches references to this contact (one another record holds
        is a conflict, never moved) and ``remove_external_refs`` detaches the ones it holds
    .

        Only the non-``None`` values passed are applied; an omitted field is left as
        it is (the public tool surface has no way to clear a core field). If a new
        ``organization_id`` is given it is validated to be in-scope (404 otherwise)
        before wiring the edge.

        ``email_status`` grades the address, so it is stamped with
        ``email_checked_at`` (now, unless given), and a write that changes ``email``
        without grading the new address clears both: a status that outlived the
        address it described would mark an unchecked address deliverable.

        ``fields`` writes custom fields key by key: each key given
        replaces that key's value, a ``None`` value removes it, and every other key is
        untouched.
    """
    _check_len("name", name, _CAP_NAME)
    _check_len("email", email, _CAP_EMAIL)
    _check_len("phone", phone, _CAP_PHONE)
    _check_len("title", title, _CAP_TITLE)
    _check_len("lead_source", lead_source, _CAP_SHORT)
    _check_len("status", status, _CAP_SHORT)
    _check_stage(status)
    _check_len("source", source, _CAP_SHORT)
    graded = _email_grade(email_status, email_checked_at)
    current = await _contact_row(conn, contact_id)  # 404 if out of scope
    if organization_id is not None:
        await _org_row(conn, organization_id)  # validate the edge target in-scope
    candidate = {
        "name": name.strip() if isinstance(name, str) else name,
        "email": normalize_email(email) if (email is not None) else None,
        "phone": _clean(phone),
        "title": _clean(title),
        "organization_id": organization_id,
        "lead_source": _clean(lead_source),
        "status": _clean(status),
        "source": _clean(source),
    }
    updates = _partial_updates(candidate)
    new_email = updates.get("email")
    if graded is not None:
        if new_email is None and current["email"] is None:
            raise ValueError("email_status grades an email address, and this contact has none")
        updates["email_status"], updates["email_checked_at"] = graded
    elif new_email is not None and new_email != normalize_email(current["email"]):
        updates["email_status"] = None
        updates["email_checked_at"] = None
    patch, removed = await _field_patch(conn, entity="contact", submitted=fields, by=fields_by)
    if patch or removed:
        updates["fields"] = _fields_written(conn, contact, patch, removed)
    if updates:
        updates["updated_at"] = _now()
        try:
            await conn.execute(
                contact.update()
                .where(contact.c.id == contact_id)
                .values(**_with_keys(contact, updates))
            )
        except IntegrityError as exc:
            # A new ``email`` collided with another contact in this (account, workspace)
            # under the partial unique index. Translate to a domain conflict (409)
            # with a generic, field-named message — never the index name.
            raise ConflictError("email") from exc
    await _detach_refs(
        conn, ctx, entity="contact", record_id=contact_id, refs=clean_refs(remove_external_refs)
    )
    await _attach_refs(
        conn, ctx, entity="contact", record_id=contact_id, refs=clean_refs(add_external_refs)
    )
    row = await _contact_row(conn, contact_id)
    await write_audit(
        conn,
        ctx,
        entity="contact",
        action="update",
        outcome="ok",
        entity_id=contact_id,
        detail=_fields_detail(patch, removed),
    )
    return Contact.from_row(row, retired=await _retired_for(conn, "contact"))


# The archived-exclusion scopes. An archived record (archive_* or the source of a merge)
# drops out of every default surface: the listings, the counts, the org roster and its
# contact_count, the rollups. Route each through these so a new surface cannot drift
# back to showing archived rows.
_NOT_ARCHIVED = contact.c.archived_at.is_(None)
_ORG_NOT_ARCHIVED = organization.c.archived_at.is_(None)

# Read-time floor projection: the effective lifecycle stage derived
# from stored facts — a dossier ⇒ ``enriched``, an outbound touch ⇒ ``contacted``,
# an inbound touch ⇒ ``engaged``. Because the touch floors depend on RELATED rows,
# the derivation lives in SQL (not the Python wire boundary): ``_EFFECTIVE_STATUS``
# is both the ``effective_status`` column ``_contact_select`` returns AND the
# expression ``list_contacts`` filters on — the same object, so the field and the
# filter cannot diverge. ``lifecycle.effective_status`` is the Python oracle.
_TOUCH_KINDS = ("email", "call", "meeting")
#: An interaction's direction, when it has one. A touch's direction is what advances the
#: contact's stage (outbound → contacted, inbound → engaged).
DIRECTIONS = ("inbound", "outbound")  # note/research are internal, not a touch


def _has_touch(direction: str) -> Any:
    """EXISTS: does this contact have a touch interaction in ``direction``?

    Correlated to the outer ``contact`` row; runs under the same RLS session, so it
    only sees the caller's interactions. Served by ``ix_interaction_touch``, which the
    contact's ``tenant_id`` and ``workspace_id`` reach.
    """
    return (
        select(interaction.c.id)
        .where(
            and_(
                interaction.c.tenant_id == contact.c.tenant_id,
                interaction.c.workspace_id == contact.c.workspace_id,
                interaction.c.contact_id == contact.c.id,
                interaction.c.direction == direction,
                interaction.c.kind.in_(_TOUCH_KINDS),
            )
        )
        .correlate(contact)
        .exists()
    )


_EFFECTIVE_STATUS = effective_status_sql(
    contact.c.status,
    contact.c.dossier.isnot(None),
    _has_touch("outbound"),
    _has_touch("inbound"),
)


def _contact_select() -> Select:
    """A contact ``SELECT`` carrying the derived ``effective_status`` column.

    Every contact-returning read goes through this so the wire model's
    ``effective_status`` is populated uniformly; write paths re-select through it
    (via :func:`_contact_row`) after mutating.
    """
    return select(*contact.c, _EFFECTIVE_STATUS.label("effective_status"))


# Fields a merge fills on the target from the source (empty target fields only).
# Never identity, never name, never timestamps, never status (the merge archives
# the SOURCE through archived_at); dossier carries its own timestamp alongside it.
_MERGE_FILL_FIELDS = (
    "email",
    "phone",
    "title",
    "organization_id",
    "dossier",
    "lead_source",
    "source",
)


async def merge_contacts(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    target_id: str,
    source_id: str,
) -> tuple[Contact, dict[str, Any]]:
    """Merge ``source`` into ``target`` to reconcile a duplicate; archive the source.

    Reconciles two contact rows that turned out to be the same person (e.g. a
    name-only stub and a later enriched row). Both must be in scope (404 each
    otherwise). The merge:

      * **Reassigns ALL of source's interactions to target** — a bare
        ``UPDATE interaction SET contact_id=target WHERE contact_id=source``.
        Each interaction's denormalized ``organization_id`` is left as logged: it
        is historical attribution (the org the contact was at WHEN it happened),
        not a live edge that should follow the merge.
      * **Repoints the signals about the source** (``signal.contact_id``) and the
        deals naming it as their contact (``opportunity.primary_contact_id``).
      * **Fills target's EMPTY fields from source only** (email + its status,
        phone, title, organization_id, dossier + dossier_updated_at, lead_source,
        source, and each custom field the target does not hold) — it NEVER
        overwrites a field the target already has, the same fill-empty rule the
        dedupe merge uses.
      * **Archives the source** (sets ``archived_at``) rather than hard-deleting
        it — v1 preserves history; the archived row drops out of the default
        ``list_contacts`` / ``search`` views.

    Email is the contact's unique dedupe key (``uq_contact_email``). The source's
    email is ALWAYS CLEARED on merge — whether or not it filled the target — so the
    archived row never shadows future dedupe under the partial unique index. If the
    target was empty it adopts the address; either way the target becomes the single
    owner and a later ``find_or_create_contact`` on that address can never
    ON-CONFLICT-match the hidden archived row.

    Returns ``(target, summary)`` where ``summary`` carries the ``interactions_moved``,
    ``signals_moved``, ``opportunities_moved`` and ``refs_moved`` counts and
    ``fields_filled`` (the list of target fields the source filled).
    """
    if target_id == source_id:
        raise ValueError("target_id and source_id must differ")

    target_row = await _contact_row(conn, target_id)  # 404 if out of scope
    source_row = await _contact_row(conn, source_id)  # 404 if out of scope

    # Refuse confusing merges into/out of an already-archived row. An archived
    # target is a parked duplicate (the wrong thing to keep); an archived source
    # has already been merged away. ValueError → invalid_request at the tool layer.
    if target_row["archived_at"] is not None:
        raise ValueError("cannot merge into an archived contact")
    if source_row["archived_at"] is not None:
        raise ValueError("source contact is already archived")

    # FIX: the interaction-reassign below moves rows under uq_interaction_idem,
    # which is unique on (tenant_id, workspace_id, contact_id, idempotency_key) —
    # PER CONTACT by design. If the source and target each hold an interaction with
    # the SAME idempotency_key, reassigning source→target would collide on that
    # index → IntegrityError. So FIRST retire (null) the source keys that already
    # exist on the target: the token is spent, but the interaction still moves and
    # its history is preserved. A NULL key is exempt from the partial unique index.
    target_keys = select(interaction.c.idempotency_key).where(
        and_(
            interaction.c.contact_id == target_id,
            interaction.c.idempotency_key.isnot(None),
        )
    )
    await conn.execute(
        interaction.update()
        .where(
            and_(
                interaction.c.contact_id == source_id,
                interaction.c.idempotency_key.in_(target_keys),
            )
        )
        .values(idempotency_key=None)
    )

    # Reassign every source interaction onto the target. The denormalized
    # organization_id stays as logged (historical attribution, not a live edge).
    moved = await conn.execute(
        interaction.update()
        .where(interaction.c.contact_id == source_id)
        .values(contact_id=target_id)
    )
    interactions_moved = moved.rowcount or 0
    # The signals about the person, and the deals naming them as their contact, name the
    # person kept now: left on the archived source, the keeper's reads would miss them and a
    # deal would keep naming a hidden contact. No unique index holds either column.
    signals_moved = await _repoint(conn, signal.c.contact_id, source_id, target_id)
    opportunities_moved = await _repoint(
        conn, opportunity.c.primary_contact_id, source_id, target_id
    )
    # The source's external references name the person kept now; left on the archived
    # source, every later pull would match the hidden row.
    refs_moved = await _move_refs(conn, entity="contact", source_id=source_id, target_id=target_id)

    # Fill the target's EMPTY fields from the source (never overwrite). The
    # dossier carries its own timestamp, so move dossier_updated_at alongside it.
    candidate: dict[str, str | None] = {f: source_row.get(f) for f in _MERGE_FILL_FIELDS}
    updates = _merge_empty_only(target_row, candidate)
    if "dossier" in updates:
        updates["dossier_updated_at"] = source_row.get("dossier_updated_at")
    if "email" in updates:
        # The status grades the address, so it travels with it.
        updates["email_status"] = source_row.get("email_status")
        updates["email_checked_at"] = source_row.get("email_checked_at")
    fields_filled = [f for f in _MERGE_FILL_FIELDS if f in updates]
    inherited = _inherited_fields(target_row, source_row)
    if inherited:
        updates["fields"] = _fields_filled(conn, contact, inherited)
        fields_filled += [f"fields.{key}" for key in inherited]

    # Archive the source FIRST (v1 preserves history — no hard delete). ALWAYS
    # clear the source's email, whether or not it was adopted by the target: an
    # archived row must never hold a live address, or a later find_or_create on
    # that address would ON-CONFLICT-match the hidden archived row under
    # ``uq_contact_email``. Clearing it also avoids violating the index when the
    # target adopts the same address in this transaction.
    source_updates: dict[str, Any] = {
        "archived_at": _now(),
        "email": None,
        "email_status": None,
        "email_checked_at": None,
        "updated_at": _now(),
    }
    await conn.execute(
        contact.update()
        .where(contact.c.id == source_id)
        .values(**_with_keys(contact, source_updates))
    )

    if updates:
        updates["updated_at"] = _now()
        await conn.execute(
            contact.update().where(contact.c.id == target_id).values(**_with_keys(contact, updates))
        )

    fresh = await _contact_row(conn, target_id)
    await write_audit(
        conn,
        ctx,
        entity="contact",
        action="merge",
        outcome="ok",
        entity_id=target_id,
        # Durable record of what merged where: ids + a count + field NAMES only
        # (no PII/values), so the archived source_id is not lost from the log.
        detail={
            "source_id": source_id,
            "target_id": target_id,
            "interactions_moved": interactions_moved,
            "signals_moved": signals_moved,
            "opportunities_moved": opportunities_moved,
            "refs_moved": refs_moved,
            "fields_filled": fields_filled,
        },
    )
    return Contact.from_row(fresh, retired=await _retired_for(conn, "contact")), {
        "interactions_moved": interactions_moved,
        "signals_moved": signals_moved,
        "opportunities_moved": opportunities_moved,
        "refs_moved": refs_moved,
        "fields_filled": fields_filled,
    }


_ORG_MERGE_FILL_FIELDS = (
    "domain",
    "website",
    "industry",
    "region",
    "employee_range",
    "revenue_range",
    "headquarters",
    "dossier",
    "source",
)


async def merge_organizations(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    target_id: str,
    source_id: str,
) -> tuple[Organization, dict[str, Any]]:
    """Merge ``source`` into ``target`` to reconcile a duplicate; archive the source.

    The organization counterpart of :func:`merge_contacts`, and the tool the
    name-dedupe index cannot replace: that index stops NEW same-name duplicates,
    but two rows that already exist — or two that hold DIFFERENT domains and turn
    out to be one company — can only be reconciled by moving one onto the other.

    The merge:

      * **Repoints every reference**: ``contact.organization_id``,
        ``interaction.organization_id``, ``opportunity.organization_id``, and
        both endpoints of ``organization_edge``. Interaction attribution is
        historical, but the org it names is going away, so it follows.
      * **Collapses edges on their MAPPED endpoints**, not their raw ones. After
        the repoint an edge between source and target is a self-edge, and a
        source edge can duplicate one the target already holds under
        ``uq_organization_edge``. Both are marked (``deleted_at``) BEFORE the
        repoint and keep their raw endpoints, score and source; the repoint moves
        live edges only, so it cannot raise. No edge row is removed.
      * **Moves the evidence**: ``signal.organization_id``,
        ``proof_point.organization_id``, and the party of each live
        ``signal_evidence_for`` row. An evidence row is not repointed: the source's
        is removed (``removed_at``) and the target's added or revived, so a reader
        of changes filtered to either party sees the move.
      * **Folds a signal the target already holds.** A source signal with the
        target's source URL and type would duplicate it under ``uq_signal_source``.
        One of the pair is marked (``deleted_at``) before the repoint, the
        target's unless only the source's is still active, and its live evidence
        rows are added to the one kept, so no purpose a signal served is lost.
      * **Fills the target's EMPTY fields from the source** — never overwriting,
        the same fill-empty rule the dedupe merge uses. ``dossier_updated_at``
        moves with ``dossier`` so the pair stays coherent.
      * **Archives the source** (sets ``archived_at``) rather than deleting it;
        an archived org drops out of search and of ``list_organizations`` unless
        ``include_archived``.

    An archived source must not shadow either dedupe key. Its ``domain`` is
    cleared for the reason ``merge_contacts`` clears ``email``: a hidden row
    holding a live key would ON-CONFLICT-match a later ``find_or_create`` under
    ``uq_org_domain``. Clearing it moves the row INTO ``uq_org_name_when_domainless``,
    where it would then collide with the target whenever both share a name — the
    ordinary case for a merge — so the source is also renamed.

    The rename carries the SOURCE id, not just the target's. Keying it on the
    target alone makes the archived name a function of (source name, target id),
    so folding two identically-named duplicates into one keeper produces two
    byte-identical domain-less names and the second merge dies on the very index
    this rename exists to avoid. That is not an exotic input: the name index is
    partial, so same-name rows carrying different domains are legal by design,
    and merging two of them into a third is precisely the "different domains,
    same company" case this tool exists for. The source id makes the name unique
    by construction, and also settles the ``_CAP_NAME`` truncation variant, where
    two long names sharing a prefix would otherwise collide the same way.
    ``name`` is NOT NULL, so it can be made unique but not removed.

    Returns ``(target, summary)`` with ``contacts_moved`` / ``interactions_moved``
    / ``opportunities_moved`` / ``signals_moved`` / ``signals_dropped`` /
    ``proof_points_moved`` / ``evidence_moved`` / ``edges_moved`` /
    ``edges_dropped`` counts and the list of target fields the source filled.
    """
    if target_id == source_id:
        raise ValueError("target_id and source_id must differ")

    target_row = await _org_row(conn, target_id)  # 404 if out of scope
    source_row = await _org_row(conn, source_id)  # 404 if out of scope

    if target_row["archived_at"] is not None:
        raise ValueError("cannot merge into an archived organization")
    if source_row["archived_at"] is not None:
        raise ValueError("source organization is already archived")

    # --- Edges: mark what cannot survive the repoint, then repoint. --------
    # A source edge whose other endpoint IS the target becomes a self-edge; a
    # source edge the target already holds duplicates it. Mark both first.
    k = organization_edge.alias("k")
    same_scope = and_(
        k.c.tenant_id == organization_edge.c.tenant_id,
        k.c.workspace_id == organization_edge.c.workspace_id,
        k.c.relation == organization_edge.c.relation,
        k.c.deleted_at.is_(None),
    )
    edges_dropped = await _mark_deleted(
        conn,
        organization_edge,
        or_(
            # Between the two being merged: a self-edge after the repoint.
            and_(
                organization_edge.c.from_organization_id == source_id,
                organization_edge.c.to_organization_id == target_id,
            ),
            and_(
                organization_edge.c.to_organization_id == source_id,
                organization_edge.c.from_organization_id == target_id,
            ),
            # Source edge the target already holds: a duplicate after the repoint.
            and_(
                organization_edge.c.from_organization_id == source_id,
                exists().where(
                    and_(
                        same_scope,
                        k.c.from_organization_id == target_id,
                        k.c.to_organization_id == organization_edge.c.to_organization_id,
                    )
                ),
            ),
            and_(
                organization_edge.c.to_organization_id == source_id,
                exists().where(
                    and_(
                        same_scope,
                        k.c.to_organization_id == target_id,
                        k.c.from_organization_id == organization_edge.c.from_organization_id,
                    )
                ),
            ),
        ),
        _now(),
    )

    edges_moved = 0
    for col in (
        organization_edge.c.from_organization_id,
        organization_edge.c.to_organization_id,
    ):
        res = await conn.execute(
            organization_edge.update()
            .where(col == source_id, _live(organization_edge))
            .values(**{col.name: target_id})
        )
        edges_moved += res.rowcount or 0

    # --- Plain FK repoints. No unique index involves organization_id. --------
    moved: dict[str, int] = {}
    for label, table in (
        ("contacts_moved", contact),
        ("interactions_moved", interaction),
        ("opportunities_moved", opportunity),
    ):
        res = await conn.execute(
            table.update()
            .where(table.c.organization_id == source_id)
            .values(organization_id=target_id)
        )
        moved[label] = res.rowcount or 0

    # --- Evidence: whom signals serve, then duplicate signals, then the rows. --
    # The parties move first, so a duplicate folded below hands the keeper edges that
    # already name the target.
    evidence_moved = await _move_evidence_party(conn, ctx, source_id=source_id, target_id=target_id)
    signals_dropped = await _fold_duplicate_signals(
        conn, ctx, source_id=source_id, target_id=target_id
    )
    # Live signals only: a folded duplicate keeps the organization it was recorded on.
    moved["signals_moved"] = await _repoint(
        conn, signal.c.organization_id, source_id, target_id, live_only=True
    )
    moved["proof_points_moved"] = await _repoint(
        conn, proof_point.c.organization_id, source_id, target_id
    )

    # --- Fill the target's empty fields from the source. ---------------------
    candidate: dict[str, Any] = {f: source_row.get(f) for f in _ORG_MERGE_FILL_FIELDS}
    updates = _merge_empty_only(target_row, candidate)
    if "dossier" in updates:
        updates["dossier_updated_at"] = source_row.get("dossier_updated_at")
    fields_filled = [f for f in _ORG_MERGE_FILL_FIELDS if f in updates]
    inherited = _inherited_fields(target_row, source_row)
    if inherited:
        updates["fields"] = _fields_filled(conn, organization, inherited)
        fields_filled += [f"fields.{key}" for key in inherited]

    # --- Archive the source FIRST, clearing both dedupe keys (see docstring).
    marker = f" ({source_id} merged into {target_id})"
    archived_name = source_row["name"][: _CAP_NAME - len(marker)] + marker
    await conn.execute(
        organization.update()
        .where(organization.c.id == source_id)
        .values(
            **_with_keys(
                organization,
                {
                    "archived_at": _now(),
                    "domain": None,
                    "name": archived_name,
                    "updated_at": _now(),
                },
            )
        )
    )

    if updates:
        updates["updated_at"] = _now()
        await conn.execute(
            organization.update()
            .where(organization.c.id == target_id)
            .values(**_with_keys(organization, updates))
        )

    # The source's external references name the company kept now.
    refs_moved = await _move_refs(
        conn, entity="organization", source_id=source_id, target_id=target_id
    )
    fresh = await _org_row(conn, target_id)
    summary = {
        **moved,
        "signals_dropped": signals_dropped,
        "evidence_moved": evidence_moved,
        "refs_moved": refs_moved,
        "edges_moved": edges_moved,
        "edges_dropped": edges_dropped,
        "fields_filled": fields_filled,
    }
    await write_audit(
        conn,
        ctx,
        entity="organization",
        action="merge",
        outcome="ok",
        entity_id=target_id,
        # ids + counts + field NAMES only (no values), so the archived source_id
        # is recoverable from the log.
        # `source_domain` is the one VALUE recorded here, deliberately. Archiving
        # clears it, and `fields_filled` names fields without their contents, so
        # without this the domain a merge discarded (because the target already
        # held one) would be unrecoverable from any record.
        detail={
            "source_id": source_id,
            "target_id": target_id,
            "source_domain": source_row.get("domain"),
            **summary,
        },
    )
    return Organization.from_row(fresh, retired=await _retired_for(conn, "organization")), summary


async def _repoint(
    conn: AsyncConnection, column: Any, source_id: str, target_id: str, *, live_only: bool = False
) -> int:
    """Point every row of ``column``'s table that names ``source_id`` at ``target_id``; the
    count. For a column no unique index holds, so the repoint cannot collide."""
    table = column.table
    statement = table.update().where(column == source_id)
    if live_only:
        statement = statement.where(_live(table))
    return (await conn.execute(statement.values({column.name: target_id}))).rowcount or 0


async def _move_evidence_party(
    conn: AsyncConnection, ctx: Principal, *, source_id: str, target_id: str
) -> int:
    """Make each live signal that is evidence for ``source_id`` evidence for ``target_id``;
    the count of edges moved.

    The source's edge is removed (kept, with ``removed_at``) and the target's added, or
    revived when the signal once served it, so a reader of changes filtered to either party
    sees the move, and the signal's stamp moves. An edge already removed stays on the source,
    the party it was removed from. A deleted signal's edges are left: no read returns it.
    """
    edge = signal_evidence_for.c
    rows = await _all(
        conn,
        select(edge.signal_id, edge.added_by).where(
            edge.for_organization_id == source_id,
            edge.removed_at.is_(None),
            _live_target(edge.signal_id, signal),
        ),
    )
    for row in rows:
        stamp = db_now(conn.dialect.name)
        await conn.execute(
            signal_evidence_for.update()
            .where(
                edge.signal_id == row["signal_id"],
                edge.for_organization_id == source_id,
                edge.removed_at.is_(None),
            )
            .values(removed_at=stamp, updated_at=stamp)
        )
        await _put_edge(conn, ctx, row["signal_id"], target_id, added_by=row["added_by"])
        await _touch_signal(conn, row["signal_id"])
    return len(rows)


async def _fold_duplicate_signals(
    conn: AsyncConnection, ctx: Principal, *, source_id: str, target_id: str
) -> int:
    """Fold each live source signal the target already holds (same source URL and type,
    the ``uq_signal_source`` key) into one; the count marked.

    The target's signal is kept unless it is withdrawn and the source's is not: an active
    fact is never hidden behind a withdrawn copy of it. The other is marked (``deleted_at``)
    and keeps its organization and values, and its live evidence edges are added to the one
    kept, so every purpose the fact served survives the merge.
    """
    s = signal.alias("s")
    t = signal.alias("t")
    pairs = await _all(
        conn,
        select(
            s.c.id.label("source_signal"),
            s.c.status.label("source_status"),
            t.c.id.label("target_signal"),
            t.c.status.label("target_status"),
        ).where(
            s.c.organization_id == source_id,
            s.c.deleted_at.is_(None),
            t.c.tenant_id == s.c.tenant_id,
            t.c.workspace_id == s.c.workspace_id,
            t.c.organization_id == target_id,
            t.c.source_url == s.c.source_url,
            t.c.signal_type == s.c.signal_type,
            t.c.deleted_at.is_(None),
        ),
    )
    edge = signal_evidence_for.c
    for pair in pairs:
        keep, drop = pair["target_signal"], pair["source_signal"]
        if pair["target_status"] == WITHDRAWN and pair["source_status"] != WITHDRAWN:
            keep, drop = drop, keep
        parties = await _all(
            conn,
            select(edge.for_organization_id, edge.added_by)
            .where(edge.signal_id == drop, edge.removed_at.is_(None))
            .order_by(edge.for_organization_id.asc().nulls_first()),
        )
        changed = [
            await _put_edge(
                conn, ctx, keep, party["for_organization_id"], added_by=party["added_by"]
            )
            for party in parties
        ]
        if any(changed):
            await _touch_signal(conn, keep)
        await _mark_deleted(conn, signal, signal.c.id == drop, _now())
    return len(pairs)


# --------------------------------------------------------------------------- #
# External references                                                          #
# --------------------------------------------------------------------------- #

_REF_OWNER = {"organization": "organization_id", "contact": "contact_id"}
_MERGE_HINT = {"organization": "merge_organizations", "contact": "merge_contacts"}


async def _ref_owners(conn: AsyncConnection, refs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """``{ref: (entity, record_id)}`` for each of ``refs`` this workspace holds."""
    if not refs:
        return {}
    rows = await conn.execute(
        select(external_ref.c.uri, external_ref.c.organization_id, external_ref.c.contact_id).where(
            external_ref.c.uri.in_(list(refs)), _live(external_ref)
        )
    )
    return {
        uri: ("organization", org_id) if org_id is not None else ("contact", contact_id)
        for uri, org_id, contact_id in rows.tuples().all()
    }


async def _record_by_refs(conn: AsyncConnection, entity: str, refs: Sequence[str]) -> str | None:
    """The one record of ``entity`` that ``refs`` name, or ``None`` when none is held.

    Refs naming a record of the other kind, or two different records, are a conflict:
    the row describes two things this workspace keeps apart.
    """
    owners = await _ref_owners(conn, refs)
    other = [(r, o) for r, o in owners.items() if o[0] != entity]
    if other:
        ref, (kind, rid) = other[0]
        raise RefConflictError(
            f"{ref} names the {kind} {rid}, not this {entity}",
            next_step="Give each reference on the record it names.",
        )
    ids = sorted({rid for _kind, rid in owners.values()})
    if len(ids) > 1:
        raise RefConflictError(
            f"these references name two {entity} records, {ids[0]} and {ids[1]}",
            next_step=f"If they are one {entity}, join them with {_MERGE_HINT[entity]}.",
        )
    return ids[0] if ids else None


async def _refuse_name_only_match(
    conn: AsyncConnection, *, entity: str, record_id: str, name: str, refs: Sequence[str]
) -> None:
    """Refuse to attach references to a record matched by its name alone, when it already
    holds references outside ``refs``.

    A name is a weak key: across a large list, two companies share one. A record matched
    by name that already carries some other source's identity is most likely another
    company, and attaching the row's references would bind every later pull to it. A
    reference from ``refs`` itself is not another source's: a concurrent call with the same
    row attached it first.
    """
    owner = getattr(external_ref.c, _REF_OWNER[entity])
    holds = (
        await conn.execute(
            select(func.count())
            .select_from(external_ref)
            .where(owner == record_id, ~external_ref.c.uri.in_(refs), _live(external_ref))
        )
    ).scalar_one()
    if holds:
        raise RefConflictError(
            f"matched {record_id} ({name}) by name only, and it already holds other "
            "references; it may be another with the same name",
            next_step=(
                f"Give the row's domain{' or email' if entity == 'contact' else ''} so it "
                f"matches on identity, or attach the reference with update_{entity} if it is "
                "the same one."
            ),
        )


async def _attach_refs(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str,
    record_id: str,
    refs: Sequence[str],
    created: bool = False,
) -> int:
    """Attach each of ``refs`` the record does not hold yet; return how many were new.

    Race-safe the same way the upserts are: insert with ON CONFLICT DO NOTHING, and a lost
    insert re-reads the owner. A reference another record holds is never moved; it is a
    :class:`RefConflictError`, ``retryable`` when this call created the record (a
    concurrent call attached the reference to its own copy first, and a second pass finds
    that one).
    """
    owner_col = _REF_OWNER[entity]
    attached = 0
    for ref in refs:
        inserted = (
            await conn.execute(
                insert_for(conn.dialect.name, external_ref)
                .values(
                    id=_new_external_ref_id(),
                    **ctx.identity_columns(),
                    uri=ref,
                    **{owner_col: record_id},
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        external_ref.c.tenant_id,
                        external_ref.c.workspace_id,
                        external_ref.c.uri,
                    ],
                    index_where=_live(external_ref),
                )
                .returning(external_ref.c.id)
            )
        ).first()
        if inserted is not None:
            attached += 1
            continue
        kind, holder = (await _ref_owners(conn, [ref]))[ref]
        if (kind, holder) != (entity, record_id):
            raise RefConflictError(
                f"{ref} already names the {kind} {holder}",
                next_step=(
                    f"If they are one {kind}, join them with {_MERGE_HINT[kind]}; if the "
                    f"reference is on the wrong record, remove it there with update_{kind}."
                ),
                retryable=created,
            )
    if attached:
        await write_audit(
            conn,
            ctx,
            entity=entity,
            action="attach_refs",
            outcome="ok",
            entity_id=record_id,
            detail={"attached": attached},
        )
    return attached


async def _detach_refs(
    conn: AsyncConnection, ctx: Principal, *, entity: str, record_id: str, refs: Sequence[str]
) -> int:
    """Detach those of ``refs`` this record holds; a reference it does not hold is left alone.

    Detaching marks the row (``deleted_at``) and removes nothing: the row keeps the URI and
    the record it named, every read leaves it out, and ``uq_external_ref_uri`` frees the URI
    at once. Attaching it again inserts a new row.
    """
    if not refs:
        return 0
    owner = getattr(external_ref.c, _REF_OWNER[entity])
    removed = await _mark_deleted(
        conn, external_ref, and_(external_ref.c.uri.in_(list(refs)), owner == record_id), _now()
    )
    if removed:
        await write_audit(
            conn,
            ctx,
            entity=entity,
            action="detach_refs",
            outcome="ok",
            entity_id=record_id,
            detail={"removed": removed},
        )
    return removed


async def external_refs_of(
    conn: AsyncConnection, *, entity: str, record_id: str
) -> tuple[list[str], int]:
    """A record's references, oldest first, the first :data:`MAX_REFS_SHOWN`, and the count."""
    owner = getattr(external_ref.c, _REF_OWNER[entity])
    rows = await conn.execute(
        select(external_ref.c.uri)
        .where(owner == record_id, _live(external_ref))
        .order_by(external_ref.c.created_at, external_ref.c.id)
        .limit(MAX_REFS_SHOWN)
    )
    total = (
        await conn.execute(
            select(func.count())
            .select_from(external_ref)
            .where(owner == record_id, _live(external_ref))
        )
    ).scalar_one()
    return list(rows.scalars().all()), int(total)


async def find_by_external_refs(
    conn: AsyncConnection, ctx: Principal, *, refs: Sequence[Any]
) -> dict[str, Any]:
    """Which of ``refs`` this workspace holds, and on what; the rest under ``missing``.

    One index probe for the batch. Each found reference carries its record's kind, id,
    name and status; the tool adds the record's address.
    """
    clean = clean_refs(refs, limit=MAX_REFS_PER_LOOKUP)
    owners = await _ref_owners(conn, clean)
    org_ids = [rid for kind, rid in owners.values() if kind == "organization"]
    contact_ids = [rid for kind, rid in owners.values() if kind == "contact"]
    named: dict[str, tuple[str, str | None]] = {}
    if org_ids:
        rows = await conn.execute(
            select(organization.c.id, organization.c.name, organization.c.status).where(
                organization.c.id.in_(org_ids)
            )
        )
        named.update({rid: (name, st) for rid, name, st in rows.tuples().all()})
    if contact_ids:
        rows = await conn.execute(
            select(contact.c.id, contact.c.name, contact.c.status).where(
                contact.c.id.in_(contact_ids)
            )
        )
        named.update({rid: (name, st) for rid, name, st in rows.tuples().all()})
    found = [
        {
            "ref": ref,
            "entity": kind,
            "id": rid,
            "name": named.get(rid, (None, None))[0],
            "status": named.get(rid, (None, None))[1],
        }
        for ref in clean
        if ref in owners
        for kind, rid in [owners[ref]]
    ]
    return {"found": found, "missing": [ref for ref in clean if ref not in owners]}


async def _move_refs(conn: AsyncConnection, *, entity: str, source_id: str, target_id: str) -> int:
    """Repoint every reference of a merged-away record at the record kept.

    The source is archived, not deleted; a reference left on it would match a hidden row on
    every later re-pull. Uniqueness is on the live URI alone, so a repoint cannot collide. A
    detached reference stays on the record it was detached from.
    """
    owner = getattr(external_ref.c, _REF_OWNER[entity])
    return (
        await conn.execute(
            external_ref.update()
            .where(owner == source_id, _live(external_ref))
            .values({owner.name: target_id})
        )
    ).rowcount


# --------------------------------------------------------------------------- #
# Deletion                                                                     #
# --------------------------------------------------------------------------- #


class DeleteRefusedError(ConflictError):
    """A delete refused because records the user keeps still depend on the one named.

    ``str(exc)`` is what blocks it (``contacts`` / ``opportunities``); ``message`` says how
    many, and ``next_step`` how to clear them. Nothing was deleted.
    """

    def __init__(self, field: str, *, message: str, next_step: str) -> None:
        super().__init__(field)
        self.message = message
        self.next_step = next_step


def _live(table: Any) -> ColumnElement[bool]:
    """``table``'s rows no delete has marked."""
    return table.c.deleted_at.is_(None)


def _live_target(link: Any, table: Any) -> ColumnElement[bool]:
    """Whether ``link`` names a live row of ``table``: a link to a deleted row reads as absent.

    The target is read through its own alias, so the test correlates to ``link``'s row alone
    even where ``table`` is joined into the same query."""
    target = table.alias()
    return exists().where(target.c.id == link, target.c.deleted_at.is_(None))


def _live_link(link: Any, table: Any) -> Any:
    """``link``, or NULL where it names a deleted row of ``table``."""
    return case((_live_target(link, table), link), else_=null())


def _live_party(edge: Any) -> ColumnElement[bool]:
    """Whether an evidence edge's party is live: the workspace, or a live organization."""
    return or_(
        edge.for_organization_id.is_(None), _live_target(edge.for_organization_id, organization)
    )


def _check_confirm(record_id: str, confirm: str) -> None:
    """Refuse a delete whose ``confirm`` is not the id it deletes, exactly."""
    if confirm != record_id:
        raise ValueError("confirm must repeat the id being deleted, exactly; nothing was deleted")


async def _merged_into(
    conn: AsyncConnection, ctx: Principal, *, entity: str, table: Any, record_id: str
) -> list[str]:
    """The live archived duplicates merged into ``record_id``, transitively.

    A merge archives its source and records ``source_id`` on the audit row of the record
    kept; the archived source is the same person or company. A source that is no longer
    archived was revived as a record of its own and is left.
    """
    found = {record_id}
    frontier = [record_id]
    while frontier:
        rows = await _all(
            conn,
            select(people_audit.c.detail).where(
                people_audit.c.workspace_id == ctx.workspace_id,
                people_audit.c.entity == entity,
                people_audit.c.action == "merge",
                people_audit.c.outcome == "ok",
                people_audit.c.entity_id.in_(frontier),
            ),
        )
        sources = (
            {row["detail"].get("source_id") for row in rows if isinstance(row["detail"], dict)}
            - found
            - {None}
        )
        if not sources:
            break
        archived = await conn.execute(
            select(table.c.id).where(
                table.c.id.in_(sorted(sources)),
                table.c.workspace_id == ctx.workspace_id,
                table.c.archived_at.isnot(None),
                _live(table),
            )
        )
        frontier = sorted(r[0] for r in archived)
        found.update(frontier)
    return sorted(found - {record_id})


async def _mark_deleted(conn: AsyncConnection, table: Any, predicate: Any, stamp: datetime) -> int:
    """Set ``deleted_at`` on the live rows of ``table`` that ``predicate`` names, and nothing
    else; the count."""
    statement = table.update().where(predicate, _live(table)).values(deleted_at=stamp)
    return (await conn.execute(statement)).rowcount or 0


async def delete_contact(
    conn: AsyncConnection, ctx: Principal, *, contact_id: str, confirm: str
) -> dict[str, Any]:
    """Delete one person. Returns ``{"deleted": {table: count}}``.

    No row is removed and no value changes. ``deleted_at`` is set on the contact, the live
    archived duplicates merged into it, and what exists only for them: their interactions,
    the signals about them, and their external references. Every read leaves those rows out,
    and treats a link to one from a record that stays as absent. One audit row ``delete``
    records the counts. ``confirm`` must be ``contact_id``.

    The contact must be live and in the active workspace: a write goes there only, so a
    record another workspace lets this caller read is not found here.
    """
    _check_confirm(contact_id, confirm)
    found = await _one(
        conn,
        select(contact.c.id).where(
            contact.c.id == contact_id,
            contact.c.workspace_id == ctx.workspace_id,
            _live(contact),
        ),
    )
    if found is None:
        raise ContactNotFoundError(contact_id)

    ids = [
        contact_id,
        *await _merged_into(conn, ctx, entity="contact", table=contact, record_id=contact_id),
    ]
    stamp = _now()
    deleted = {
        "contact": await _mark_deleted(conn, contact, contact.c.id.in_(ids), stamp),
        "interaction": await _mark_deleted(
            conn, interaction, interaction.c.contact_id.in_(ids), stamp
        ),
        "signal": await _mark_deleted(conn, signal, signal.c.contact_id.in_(ids), stamp),
        "external_ref": await _mark_deleted(
            conn, external_ref, external_ref.c.contact_id.in_(ids), stamp
        ),
    }
    await write_audit(
        conn,
        ctx,
        entity="contact",
        action="delete",
        outcome="ok",
        entity_id=contact_id,
        detail={"deleted": deleted},
    )
    return {"deleted": deleted}


async def delete_organization(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str,
    confirm: str,
    delete_opportunities: bool = False,
) -> dict[str, Any]:
    """Delete one company. Returns ``{"deleted": {table: count}}``.

    Refused while any live contact still belongs to it (a person is deleted with
    :func:`delete_contact`, or moved), and while any live opportunity names it unless
    ``delete_opportunities`` (a deal is the user's own record, so deleting one is asked for
    by name). Otherwise no row is removed and no value changes: ``deleted_at`` is set on
    the organization, the live archived duplicates merged into it, their signals, proof
    points and external references, and their opportunities when asked. Every read leaves
    those rows out, and treats a link to one from a record that stays (an interaction, a
    similar-company edge, another signal's evidence) as absent. ``confirm`` must be
    ``organization_id``.
    """
    _check_confirm(organization_id, confirm)
    found = await _one(
        conn,
        select(organization.c.id).where(
            organization.c.id == organization_id,
            organization.c.workspace_id == ctx.workspace_id,
            _live(organization),
        ),
    )
    if found is None:
        raise OrganizationNotFoundError(organization_id)

    ids = [
        organization_id,
        *await _merged_into(
            conn, ctx, entity="organization", table=organization, record_id=organization_id
        ),
    ]
    members = await _count(conn, contact, and_(contact.c.organization_id.in_(ids), _live(contact)))
    if members:
        raise DeleteRefusedError(
            "contacts",
            message=(
                f"{members} contact(s) still belong to organization {organization_id!r}, "
                "archived duplicates included; nothing was deleted."
            ),
            next_step=(
                f"List them with list_contacts(organization_id={organization_id!r}) and "
                f"list_contacts(organization_id={organization_id!r}, include_archived=true). "
                "Delete each person with delete_contact, or move them with "
                "update_contact(organization_id=...), then delete the organization again."
            ),
        )
    deals = await _count(
        conn, opportunity, and_(opportunity.c.organization_id.in_(ids), _live(opportunity))
    )
    if deals and not delete_opportunities:
        raise DeleteRefusedError(
            "opportunities",
            message=(
                f"{deals} opportunity(ies) name organization {organization_id!r}; "
                "nothing was deleted."
            ),
            next_step=(
                f"Review them with list_opportunities(organization_id={organization_id!r}). "
                "To delete them with the organization, confirm with the user and call "
                "delete_organization again with delete_opportunities=true."
            ),
        )

    stamp = _now()
    deleted = {
        "organization": await _mark_deleted(conn, organization, organization.c.id.in_(ids), stamp),
        "signal": await _mark_deleted(conn, signal, signal.c.organization_id.in_(ids), stamp),
        "proof_point": await _mark_deleted(
            conn, proof_point, proof_point.c.organization_id.in_(ids), stamp
        ),
        "external_ref": await _mark_deleted(
            conn, external_ref, external_ref.c.organization_id.in_(ids), stamp
        ),
        "opportunity": await _mark_deleted(
            conn, opportunity, opportunity.c.organization_id.in_(ids), stamp
        ),
    }
    await write_audit(
        conn,
        ctx,
        entity="organization",
        action="delete",
        outcome="ok",
        entity_id=organization_id,
        detail={"deleted": deleted},
    )
    return {"deleted": deleted}


# --------------------------------------------------------------------------- #
# Imports                                                                      #
# --------------------------------------------------------------------------- #

#: The most rows one import call takes. A longer list is refused whole, so a caller
#: splits it rather than losing its tail.
IMPORT_MAX_ROWS = 500

_ORG_IMPORT_KEYS = ("name", "domain", "website", "industry", "region", "fields", "external_refs")
_CONTACT_IMPORT_KEYS = (
    "name",
    "email",
    "phone",
    "title",
    "organization_id",
    "organization_name",
    "organization_domain",
    "lead_source",
    "status",
    "email_status",
    "fields",
    "external_refs",
)
#: The core values a match can keep in place of a row's own, by entity. A differing
#: name is left out: the same company is written many ways, and that is not news.
_ORG_KEPT_CORE = ("domain", "website", "industry", "region")
_CONTACT_KEPT_CORE = ("email", "phone", "title", "lead_source", "status", "email_status")
#: How a core value compares with the one a record holds: in the form the record stores.
#: A match by domain or email holds the same one; a match by reference may not.
_KEPT_NORMAL = {"domain": normalize_domain, "email": normalize_email}
#: A skip reason is the row's error message, cut so a batch of failing rows stays readable.
_CAP_SKIP_REASON = 200
#: The most reason groups a result names; the rest share one, so the result stays small
#: whatever the rows hold.
_MAX_REASON_GROUPS = 20
#: What a row's errors may be. Anything else is a fault in this code, and fails the call.
_ROW_ERRORS = (ConflictError, ValueError, OrganizationNotFoundError, DataError)


def _import_row(row: object, keys: Sequence[str]) -> dict[str, Any]:
    """One import row's arguments: text values by key, ``fields`` as an object.

    A row is refused, never guessed at: an unknown key, a value that is not text, or
    ``fields`` that is not an object is a ``ValueError`` naming it. An empty string is
    read as absent, as a blank spreadsheet cell means, and a NUL character, which
    Postgres cannot store in text, is dropped.
    """
    if not isinstance(row, Mapping):
        raise ValueError("a row is an object of column values")
    unknown = sorted(k for k in row if k not in keys)
    if unknown:
        raise ValueError(f"unknown column {unknown[0]!r}; a row takes {', '.join(keys)}")
    fields = row.get("fields")
    if fields is not None and not isinstance(fields, Mapping):
        raise ValueError("fields is an object of custom field values by key")
    args: dict[str, Any] = {
        str(key): _import_text(str(key), value)
        for key, value in row.items()
        if key not in ("fields", "external_refs")
    }
    refs = row.get("external_refs")
    if refs is not None:
        args["external_refs"] = clean_refs(refs)
    if fields:
        args["fields"] = {
            k: v.replace("\x00", "") if isinstance(v, str) else v for k, v in fields.items()
        }
    return args


def _import_text(key: str, value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return _clean(value.replace("\x00", "")) if value is not None else None


def _skip_reason(exc: Exception) -> str:
    """What a skipped row reports: its error's own message, which names the column."""
    if isinstance(exc, OrganizationNotFoundError):
        return "organization_id is not an organization in this workspace"
    if isinstance(exc, DataError):
        return "a value the database cannot store"
    if isinstance(exc, RefConflictError):
        return exc.message[:_CAP_SKIP_REASON]
    reason = str(exc) or type(exc).__name__
    return reason[:_CAP_SKIP_REASON]


def _check_import_size(rows: Sequence[Any]) -> None:
    if len(rows) > IMPORT_MAX_ROWS:
        raise ValueError(
            f"An import takes at most {IMPORT_MAX_ROWS} rows per call and this one has "
            f"{len(rows)}; split it and call once per part."
        )


def _groups(by_reason: Mapping[str, list[int]]) -> list[dict[str, Any]]:
    """``[{reason, rows}]``, the first :data:`_MAX_REASON_GROUPS` reasons by name and the
    rest folded into one."""
    groups = [{"reason": r, "rows": rows} for r, rows in by_reason.items()]
    if len(groups) <= _MAX_REASON_GROUPS:
        return groups
    head, tail = groups[: _MAX_REASON_GROUPS - 1], groups[_MAX_REASON_GROUPS - 1 :]
    rest = sorted(i for g in tail for i in g["rows"])
    return [*head, {"reason": f"{len(tail)} other reasons", "rows": rest}]


async def _kept_keys(
    conn: AsyncConnection,
    *,
    entity: str,
    args: Mapping[str, Any],
    core: Sequence[str],
    held: Any,
    fields_by: str | None,
) -> list[str]:
    """The keys a matched record kept in place of the row's own value.

    A match fills only what is empty, so a row value that differs from the one the
    record holds is not written. Naming it is what keeps an import from losing data
    quietly: two rows for the same company, or a list naming a company another way.
    A field is compared in its stored form, so ``"27"`` and ``27`` are one value.
    """

    def given(key: str) -> Any:
        value = args.get(key)
        return _KEPT_NORMAL[key](value) if key in _KEPT_NORMAL and value is not None else value

    kept = [k for k in core if given(k) is not None and getattr(held, k) not in (None, given(k))]
    patch = await _field_fill_patch(conn, entity=entity, submitted=args.get("fields"), by=fields_by)
    have = held.fields or {}
    kept += [k for k, env in patch.items() if k in have and have[k] != env["v"]]
    return kept


async def _keeps_another_organization(
    conn: AsyncConnection, args: Mapping[str, Any], held: Contact
) -> bool:
    """Whether a matched contact keeps an organization other than the one its row names.

    A contact that holds an organization is never rewired by a match, so a row naming
    another one (the contact who moved) is not written. The row's organization is
    looked up the way :func:`find_or_create_organization` matches one, by domain else
    by name, and never created: a row naming one that does not exist differs too.
    """
    if held.organization_id is None:
        return False
    if args.get("organization_id") is not None:
        return args["organization_id"] != held.organization_id
    domain = normalize_domain(args.get("organization_domain"))
    name = args.get("organization_name")
    if domain is not None:
        match = organization.c.domain_key == address_key(domain)
    elif name is not None:
        match = organization.c.name_key == match_key(name)
    else:
        return False
    named = await _one(
        conn,
        select(organization.c.id)
        .where(match, _live(organization))
        .order_by(organization.c.created_at, organization.c.id),
    )
    return named is None or named["id"] != held.organization_id


async def import_organizations(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    rows: Sequence[Any],
    source: str | None = "import",
    fields_by: str | None = None,
) -> dict[str, Any]:
    """Find-or-create each row as an organization; the batch form of the tool.

    Each row goes through :func:`find_or_create_organization`, so an import dedupes,
    fills and promotes exactly as one call per row would, and writes the same audit
    rows. A re-run converges: a matched organization keeps every value it holds, and
    ``kept`` names each row whose value it kept in place of the row's own.

    Each row runs in its own savepoint. A row that fails (an unknown column, a value
    over its cap, an undefined field) is rolled back and reported under its reason
    by its index; the rest commit together. A row with no ``name`` is named by its
    domain, and a row with neither is skipped.
    """
    _check_import_size(rows)
    counts = {"created": 0, "matched": 0, "promoted": 0}
    skipped: dict[str, list[int]] = {}
    kept: dict[str, list[int]] = {}
    for index, raw in enumerate(rows):
        try:
            args = _import_row(raw, _ORG_IMPORT_KEYS)
            args["name"] = args.get("name") or normalize_domain(args.get("domain"))
            if not args["name"]:
                held_id = await _record_by_refs(
                    conn, "organization", args.get("external_refs") or []
                )
                if held_id is None:
                    raise ValueError("a row needs a name or a domain, or a reference held here")
                args["name"] = (await _org_row(conn, held_id))["name"]
            async with conn.begin_nested():
                org, action, promoted = await find_or_create_organization(
                    conn, ctx, **args, source=source, fields_by=fields_by
                )
                held = (
                    await _kept_keys(
                        conn,
                        entity="organization",
                        args=args,
                        core=_ORG_KEPT_CORE,
                        held=org,
                        fields_by=fields_by,
                    )
                    if action == "matched"
                    else []
                )
        except _ROW_ERRORS as exc:
            skipped.setdefault(_skip_reason(exc), []).append(index)
            continue
        counts[action] += 1
        counts["promoted"] += int(promoted)
        for key in held:
            kept.setdefault(f"matched an organization that keeps its own {key}", []).append(index)
    return await _import_done(conn, ctx, "organization", len(rows), counts, kept, skipped)


async def import_contacts(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    rows: Sequence[Any],
    source: str | None = "import",
    fields_by: str | None = None,
) -> dict[str, Any]:
    """Find-or-create each row as a contact; the batch form of the tool.

    Each row goes through :func:`find_or_create_contact`, with the same dedupe, fill
    and promotion, and the same audit rows. ``organization_domain`` names the
    contact's organization by its domain, which a list holds more reliably than a
    name; the organization is found or created only when the contact is wired to it.
    ``status`` is one of the contact lifecycle's statuses.

    Each row runs in its own savepoint, so a row that fails after its contact was
    written (an ``email_status`` with no address) leaves nothing behind.
    """
    _check_import_size(rows)
    counts = {"created": 0, "matched": 0, "promoted": 0}
    skipped: dict[str, list[int]] = {}
    kept: dict[str, list[int]] = {}
    statuses = tuple(LADDER)
    for index, raw in enumerate(rows):
        try:
            args = _import_row(raw, _CONTACT_IMPORT_KEYS)
            status = args.get("status")
            if status is not None and status.lower() not in statuses:
                raise ValueError(f"status is one of {', '.join(statuses)}")
            if status is not None:
                args["status"] = status.lower()
            if args.get("email_status") is not None:
                args["email_status"] = args["email_status"].lower()
            async with conn.begin_nested():
                found, action, promoted = await find_or_create_contact(
                    conn, ctx, **args, source=source, fields_by=fields_by
                )
                held = (
                    await _kept_keys(
                        conn,
                        entity="contact",
                        args=args,
                        core=_CONTACT_KEPT_CORE,
                        held=found,
                        fields_by=fields_by,
                    )
                    if action == "matched"
                    else []
                )
                if action == "matched" and await _keeps_another_organization(conn, args, found):
                    held.append("organization")
        except _ROW_ERRORS as exc:
            skipped.setdefault(_skip_reason(exc), []).append(index)
            continue
        counts[action] += 1
        counts["promoted"] += int(promoted)
        for key in held:
            kept.setdefault(f"matched a contact that keeps its own {key}", []).append(index)
    return await _import_done(conn, ctx, "contact", len(rows), counts, kept, skipped)


async def _import_done(
    conn: AsyncConnection,
    ctx: Principal,
    entity: str,
    received: int,
    counts: Mapping[str, int],
    kept: Mapping[str, list[int]],
    skipped: Mapping[str, list[int]],
) -> dict[str, Any]:
    """The import's result, and its one summary audit row beside the per-row ones."""
    n_skipped = sum(map(len, skipped.values()))
    await write_audit(
        conn,
        ctx,
        entity=entity,
        action="import",
        outcome="ok",
        detail={**counts, "received": received, "skipped": n_skipped},
    )
    return {"received": received, **counts, "kept": _groups(kept), "skipped": _groups(skipped)}


# --------------------------------------------------------------------------- #
# Interactions                                                                 #
# --------------------------------------------------------------------------- #


async def log_interaction(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    contact_id: str,
    kind: str,
    summary: str,
    body: str | None = None,
    direction: str | None = None,
    occurred_at: datetime | None = None,
    follow_up_at: datetime | None = None,
    opportunity_id: str | None = None,
    idempotency_key: str | None = None,
) -> Interaction:
    """Create an interaction; auto-copy ``organization_id`` from the contact.

    The org link is the contact's **immutable id**, denormalized at log time —
    never a name. Renaming the org later does NOT change this interaction's
    linkage (it carries the id). The contact must be in-scope (404 otherwise),
    validated in the same RLS transaction. ``occurred_at`` defaults to now.

    Optional ``idempotency_key`` makes a retry safe: when provided, the insert
    uses the same race-safe ON CONFLICT DO NOTHING + reselect pattern as the
    find_or_create upserts, keyed on the partial unique index ``uq_interaction_idem``
    over ``(tenant_id, workspace_id, contact_id, idempotency_key)``. A second call
    with the SAME key on the SAME contact returns the EXISTING interaction instead
    of appending a duplicate; the same key on a DIFFERENT contact is a new row.
    When omitted (the default), every call inserts a new row — the original
    append behavior, unchanged.
    """
    _check_len("kind", kind, _CAP_SHORT)
    _check_len("direction", direction, _CAP_SHORT)
    _check_len("summary", summary, _CAP_SUMMARY)
    _check_len("body", body, _CAP_BODY)
    _check_len("idempotency_key", idempotency_key, _CAP_SHORT)
    clean_direction = _clean(direction)
    if clean_direction is not None:
        clean_direction = clean_direction.lower()
        if clean_direction not in DIRECTIONS:
            # An unknown direction would store silently and never advance the contact's stage.
            raise ValueError(
                f"direction must be {' or '.join(DIRECTIONS)}, or omitted; got {direction!r}"
            )
    contact_row = await _contact_row(conn, contact_id)  # validate in-scope; 404 otherwise
    if opportunity_id is not None:
        # The opportunity this touch advances — validate in-scope (404 otherwise) in
        # the same RLS transaction, so a cross-workspace id can't be attached.
        await _opportunity_row(conn, opportunity_id)
    interaction_id = _new_interaction_id()
    now = _now()
    insert_values = {
        "id": interaction_id,
        **ctx.identity_columns(),
        "contact_id": contact_id,
        # Org link FROZEN at log time (CONFIRMED product decision): the
        # interaction stays attributed to the org the contact was at WHEN it
        # happened. If the contact later changes employers, this row's
        # organization_id does NOT follow — it is historical truth, not a live
        # edge. Immutable id, never a denormalized name.
        "organization_id": contact_row["organization_id"],
        "opportunity_id": opportunity_id,
        "kind": kind.strip(),
        "direction": clean_direction,
        "summary": summary,
        "body": _clean(body),
        "occurred_at": _ensure_utc(occurred_at) or now,
        "follow_up_at": _ensure_utc(follow_up_at),
        "created_at": now,
        "idempotency_key": idempotency_key,
    }

    if idempotency_key is not None:
        # Retry-safe path: a second call with the same key converges to the row
        # the first call inserted — no duplicate — via the partial unique index.
        upserted, _created = await _upsert_returning(
            conn,
            table=interaction,
            insert_values=insert_values,
            conflict_index_elements=[
                interaction.c.tenant_id,
                interaction.c.workspace_id,
                interaction.c.contact_id,
                interaction.c.idempotency_key,
            ],
            conflict_where=interaction.c.idempotency_key.isnot(None),
            reselect_predicate=and_(
                interaction.c.tenant_id == ctx.tenant_id,
                interaction.c.workspace_id == ctx.workspace_id,
                interaction.c.contact_id == contact_id,
                interaction.c.idempotency_key == idempotency_key,
            ),
        )
        interaction_id = upserted["id"]
    else:
        # No key → plain append (every call inserts a new interaction).
        await conn.execute(interaction.insert().values(**insert_values))
    # Read back through the live-link projection: a retry can return a row whose
    # organization or opportunity was deleted since, and that link reads as absent.
    fetched = await _one(
        conn, select(*_INTERACTION_COLUMNS).where(interaction.c.id == interaction_id)
    )
    if fetched is None:  # pragma: no cover - just written in this txn
        raise RuntimeError("interaction write succeeded but the row is not visible")
    row = fetched

    await write_audit(
        conn, ctx, entity="interaction", action="log", outcome="ok", entity_id=row["id"]
    )
    return Interaction.from_row(row)


# --------------------------------------------------------------------------- #
# Dossiers                                                                     #
# --------------------------------------------------------------------------- #


async def set_contact_dossier(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    contact_id: str,
    dossier: str,
    append: bool = False,
) -> tuple[Contact, datetime | None]:
    """Write a contact's dossier; stamp ``dossier_updated_at``. Returns ``(Contact, previous_ts)``.

    An empty string **clears** the dossier (back to ``None``); ``append`` adds the
    text after the current dossier instead (:func:`_write_dossier`). Returns the
    *previous* ``dossier_updated_at`` so the tool can report staleness movement.
    """
    row = await _contact_row(conn, contact_id)  # 404 if out of scope
    previous = row["dossier_updated_at"]
    await _write_dossier(conn, contact, contact_id, dossier, append=append)
    fresh = await _contact_row(conn, contact_id)
    await write_audit(
        conn, ctx, entity="contact", action="set_dossier", outcome="ok", entity_id=contact_id
    )
    return Contact.from_row(fresh, retired=await _retired_for(conn, "contact")), previous


async def set_organization_dossier(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    org_id: str,
    dossier: str,
    append: bool = False,
) -> tuple[Organization, datetime | None]:
    """Write an org's dossier; stamp ``dossier_updated_at``. Returns ``(Organization, previous_ts)``.

    An empty string clears the dossier; ``append`` adds the text after the current
    one instead. Returns the previous timestamp.
    """
    row = await _org_row(conn, org_id)  # 404 if out of scope
    previous = row["dossier_updated_at"]
    await _write_dossier(conn, organization, org_id, dossier, append=append)
    fresh = await _org_row(conn, org_id)
    await write_audit(
        conn, ctx, entity="organization", action="set_dossier", outcome="ok", entity_id=org_id
    )
    return Organization.from_row(fresh, retired=await _retired_for(conn, "organization")), previous


async def _write_dossier(
    conn: AsyncConnection, table: Any, row_id: str, dossier: str, *, append: bool
) -> None:
    """Replace a row's dossier, or with ``append`` add to it, in one statement.

    An append joins the text to the current dossier with a blank line between them
    (``concat_ws`` skips an empty dossier), inside the UPDATE itself, so two
    concurrent appends both land: no read-merge-write for a second writer to race.
    The cap is checked on the joined text in the same statement; an append that
    would pass it changes nothing and raises ``ValueError``. Appending nothing is
    refused, since it cannot mean "clear". A row gone since the caller's scoped
    read writes nothing and raises nothing here: the caller's re-read raises its
    not-found error.
    """
    _check_len("dossier", dossier, _CAP_DOSSIER)
    now = _now()
    if not append:
        value, stamp = _dossier_value(dossier)
        await conn.execute(
            table.update()
            .where(table.c.id == row_id)
            .values(dossier=value, dossier_updated_at=(now if stamp else None), updated_at=now)
        )
        return
    text, _stamp = _dossier_value(dossier)
    if text is None:
        raise ValueError("append adds text to the dossier; pass the text to add")
    joined = dialect.joined_text(conn.dialect.name, "\n\n", table.c.dossier, text)
    written = await conn.execute(
        table.update()
        .where(table.c.id == row_id, dialect.byte_length(conn.dialect.name, joined) <= _CAP_DOSSIER)
        .values(dossier=joined, dossier_updated_at=now, updated_at=now)
    )
    if written.rowcount == 0:
        if await _one(conn, select(table.c.id).where(table.c.id == row_id)) is None:
            return
        raise ValueError(
            f"appending would take the dossier past its maximum length of {_CAP_DOSSIER} "
            "bytes; rewrite it shorter without append"
        )


def _dossier_value(dossier: str) -> tuple[str | None, bool]:
    """Map a dossier write to ``(stored_value, stamp_timestamp)``.

    An empty/whitespace string clears the dossier (``None``) AND clears the
    timestamp (no staleness signal for an absent dossier). A non-empty write
    stores the text and stamps the timestamp.
    """
    cleaned = dossier.strip() if dossier is not None else ""
    if not cleaned:
        return None, False
    return cleaned, True


# --------------------------------------------------------------------------- #
# Reads                                                                        #
# --------------------------------------------------------------------------- #


async def get_contact(
    conn: AsyncConnection, ctx: Principal, *, contact_id: str
) -> tuple[Contact, dict[str, int]]:
    """Fetch a contact + related counts. 404 (not 403) if out of scope."""
    row = await _contact_row(conn, contact_id)
    interaction_count = await _count(
        conn, interaction, and_(*_scope(interaction, ctx), interaction.c.contact_id == contact_id)
    )
    _log_access(ctx, entity="contact", action="get", entity_id=contact_id)
    retired = (await _retired_marks(conn)).get("contact")
    return Contact.from_row(row, details=True, retired=retired), {"interactions": interaction_count}


async def get_organization(
    conn: AsyncConnection, ctx: Principal, *, org_id: str
) -> tuple[Organization, dict[str, int]]:
    """Fetch an organization + related counts (contacts, interactions). 404 if out of scope."""
    row = await _org_row(conn, org_id)
    # Count only the live roster — archived (merged-away) duplicates drop out, the
    # same scope organization_timeline's contact list uses.
    contact_count = await _count(
        conn,
        contact,
        and_(*_scope(contact, ctx), contact.c.organization_id == org_id, _NOT_ARCHIVED),
    )
    interaction_count = await _count(
        conn, interaction, and_(*_scope(interaction, ctx), interaction.c.organization_id == org_id)
    )
    _log_access(ctx, entity="organization", action="get", entity_id=org_id)
    retired = (await _retired_marks(conn)).get("organization")
    return Organization.from_row(row, details=True, retired=retired), {
        "contacts": contact_count,
        "interactions": interaction_count,
    }


@dataclass(frozen=True)
class Page:
    """One page of a listing.

    ``rows`` are wire models, or ``{"id", path: value}`` dicts when the caller
    named its ``columns``. ``total`` counts every row the filters match and is
    taken on the first page only: a walk asks once, not once per page.
    """

    rows: list[Any]
    next_cursor: str | None
    total: int | None


async def list_contacts(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    include_archived: bool = False,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Contact], str | None]:
    """List contacts, newest first: ``(contacts, next_cursor)``. See :func:`page_contacts`."""
    page = await page_contacts(
        conn,
        ctx,
        organization_id=organization_id,
        status=status,
        lead_source=lead_source,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )
    return page.rows, page.next_cursor


async def page_contacts(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    terms: str | None = None,
    include_archived: bool = False,
    filter: Any = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: Sequence[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page:
    """One page of contacts under a filter, in a chosen order.

    ``terms`` is free text matched by :func:`_terms_predicate` against the name and email.

    ``filter`` is the grammar :mod:`mcp_people.query` compiles, over the contact's
    core and custom fields, its organization's (``organization.<path>``), and the
    signal and interaction rollups. ``sort`` is a field path, ``-`` for descending;
    without one the order is newest first. ``columns`` returns only the paths
    named, so a caller walking a large workspace is not handed every dossier.
    RLS scopes the whole query to the caller's account and workspace.

    ``status`` matches the **effective** stage, as does the ``status``
    filter path: ``sourced`` (the enrichment worklist) excludes a dossier'd contact
    whose floor reads ``enriched``. Archived contacts (archived by :func:`archive_record`
    or merged away by :func:`merge_contacts`) are excluded unless ``include_archived``.
    """
    limit = _clamp_limit(limit)
    resolver = await _contact_resolver(conn) if (filter or sort or columns) else None
    predicate, _, _, joins = query.where(filter, resolver) if resolver else (None, {}, {}, set())
    conditions: list[ColumnElement[bool]] = []
    if organization_id is not None:
        conditions.append(contact.c.organization_id == organization_id)
    if status is not None:
        conditions.append(_EFFECTIVE_STATUS == status)
    if not include_archived:
        conditions.append(_NOT_ARCHIVED)
    if lead_source is not None:
        conditions.append(contact.c.lead_source == lead_source)
    matched = _terms_predicate(terms, (contact.c.name, contact.c.email))
    if matched is not None:
        conditions.append(matched)
    if predicate is not None:
        conditions.append(predicate)
    return await _page(
        conn,
        ctx,
        table=contact,
        base=_contact_select(),
        default_listing=_CONTACTS,
        name="contacts",
        entity="contact",
        model=Contact,
        resolver=resolver,
        conditions=conditions,
        joins=set(joins),
        sort=sort,
        columns=columns,
        limit=limit,
        cursor=cursor,
        log=lambda: _log_access(ctx, entity="contact", action="list"),
    )


async def list_organizations(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    industry: str | None = None,
    region: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Organization], str | None]:
    """List organizations, newest first: ``(organizations, next_cursor)``. See
    :func:`page_organizations`."""
    page = await page_organizations(
        conn,
        ctx,
        industry=industry,
        region=region,
        status=status,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )
    return page.rows, page.next_cursor


async def page_organizations(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    industry: str | None = None,
    region: str | None = None,
    status: str | None = None,
    terms: str | None = None,
    include_archived: bool = False,
    filter: Any = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: Sequence[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page:
    """One page of organizations under a filter, in a chosen order.

    The same grammar as :func:`page_contacts`, over the organization's core and
    custom fields and its contact and signal rollups. ``terms`` matches the name and
    domain (:func:`_terms_predicate`).

    **Allowlist default:** unless the request asks for another status
    (:func:`_unpromoted_asked`), this returns ``status="active"`` only — a WHERE predicate applied BEFORE the
    limit/keyset, so hidden ``discovered`` lookalikes (or any future quarantine
    state) never short-page the result. Defaulting to ``active`` is
    self-protecting as new statuses appear.
    """
    limit = _clamp_limit(limit)
    resolver = await _organization_resolver(conn) if (filter or sort or columns) else None
    predicate, asked, excluded, joins = (
        query.where(filter, resolver) if resolver else (None, {}, {}, set())
    )
    conditions: list[ColumnElement[bool]] = []
    if status is not None:
        conditions.append(organization.c.status == status)
    elif not _unpromoted_asked(asked, excluded):
        conditions.append(organization.c.status == "active")
    if not include_archived:
        conditions.append(_ORG_NOT_ARCHIVED)
    if industry is not None:
        conditions.append(organization.c.industry == industry)
    if region is not None:
        conditions.append(organization.c.region == region)
    matched = _terms_predicate(terms, (organization.c.name, organization.c.domain))
    if matched is not None:
        conditions.append(matched)
    if predicate is not None:
        conditions.append(predicate)
    return await _page(
        conn,
        ctx,
        table=organization,
        base=select(organization),
        default_listing=_ORGANIZATIONS,
        name="organizations",
        entity="organization",
        model=Organization,
        resolver=resolver,
        conditions=conditions,
        joins=set(joins),
        sort=sort,
        columns=columns,
        limit=limit,
        cursor=cursor,
        log=lambda: _log_access(ctx, entity="organization", action="list"),
    )


def _source(table: Any, joins: set[str]) -> Any:
    """``table``, joined to the related tables and aggregates a request's paths read.

    Every join is a LEFT join onto at most one row, so it never drops or repeats a
    listed row: a contact has one organization, and each aggregate has one group
    per key.
    """
    source = table
    if "organization" in joins:
        source = source.outerjoin(
            organization, and_(table.c.organization_id == organization.c.id, _live(organization))
        )
    if "signals" in joins:
        account = table.c.organization_id if table is contact else table.c.id
        source = source.outerjoin(_SIGNAL_COUNTS, _SIGNAL_COUNTS.c.organization_id == account)
    if "interactions" in joins:
        source = source.outerjoin(_INTERACTION_STATS, _INTERACTION_STATS.c.contact_id == table.c.id)
    if "contacts" in joins:
        source = source.outerjoin(_CONTACT_COUNTS, _CONTACT_COUNTS.c.organization_id == table.c.id)
    if "stage" in joins:
        source = source.outerjoin(
            _STAGE,
            and_(
                _STAGE.c.tenant_id == table.c.tenant_id,
                _STAGE.c.workspace_id == table.c.workspace_id,
                _STAGE.c.key == table.c.stage,
            ),
        )
    if "contact" in joins:
        source = source.outerjoin(
            contact, and_(table.c.primary_contact_id == contact.c.id, _live(contact))
        )
    if "deal_interactions" in joins:
        source = source.outerjoin(
            _DEAL_INTERACTION_STATS, _DEAL_INTERACTION_STATS.c.opportunity_id == table.c.id
        )
    return source


def _scope(table: Any, ctx: Principal) -> list[ColumnElement[bool]]:
    """``table``'s rows in ``ctx``'s account and readable workspaces, as a predicate.

    These are the rows row-level security admits, stated in the query so the planner
    can use an index: the policy's workspace test sits under an ``OR`` with the system
    role, which no index answers, so a listing or count without this reads every row
    the account holds.
    """
    scope = [
        table.c.tenant_id == ctx.tenant_id,
        table.c.workspace_id.in_(sorted(ctx.allowed_workspaces)),
    ]
    # A deleted row is out of every read.
    if "deleted_at" in table.c:
        scope.append(_live(table))
    return scope


# The joins that add at most one row's columns to a listed row, cheap to read for a page
# of ids. The others join an aggregate over a whole related table.
_ROW_JOINS = frozenset({"organization", "contact", "stage"})


async def _page(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    table: Any,
    base: Select,
    default_listing: Listing,
    name: str,
    entity: str,
    model: Any,
    resolver: query.Resolver | None,
    conditions: list[ColumnElement[bool]],
    joins: set[str],
    sort: str | None,
    columns: Sequence[str] | None,
    limit: int,
    cursor: str | None,
    log: Any,
) -> Page:
    """Run one listing: find the page's rows, read them, and count the matches on the first page.

    Finding and reading are two queries. The first selects only each row's id and the
    columns its order reads, under the filter and the sort, so when those touch only
    indexed columns the database answers it from an index and never reads a wide row
    it will not return. The second reads the page's rows by id, with the columns asked
    for; a rollup shown there is computed for those rows alone.
    """
    conditions = [*_scope(table, ctx), *conditions]
    listing = default_listing
    # The filter's joins, and the sort's: what finding and counting the rows reads.
    finding = set(joins)
    extra: list[ColumnElement[Any]] = []
    if sort and resolver is not None:
        ref, descending = query.parse_sort(sort, resolver)
        listing, sort_key = query.sorted_listing(name, ref, descending, table.c.id)
        extra.append(sort_key)
        if ref.joins:
            finding.add(ref.joins)
    found = (
        select(table.c.id, table.c.created_at, *extra)
        .select_from(_source(table, finding))
        .where(*conditions)
    )
    found_rows = await _all(conn, listing.apply(found, cursor).limit(limit + 1))
    found_rows, next_cursor = listing.paginate(found_rows, limit)
    ids = [r["id"] for r in found_rows]
    total = None
    if not cursor:
        total = await _count(conn, _source(table, set(joins)), and_(*conditions))
    log()

    reading = finding & _ROW_JOINS
    selected: list[tuple[str, str, ColumnElement[Any]]] | None = None
    if columns and resolver is not None:
        selected, column_joins = query.projection(columns, resolver)
        reading |= column_joins
        base = select(table.c.id, *(expr for _p, _l, expr in selected))
    rows = await _all(conn, base.select_from(_source(table, reading)).where(table.c.id.in_(ids)))
    by_id = {r["id"]: r for r in rows}
    page = [by_id[i] for i in ids if i in by_id]
    if selected is None:
        # A full row drops the entity's retired fields; a projection cannot name one
        # (the resolver refuses a retired path).
        retired = (await _retired_marks(conn)).get(entity)
        return Page([model.from_row(r, retired=retired) for r in page], next_cursor, total)
    # A projected numeric comes back as a Decimal, which the wire would carry as a
    # string; a column holds the number the full record holds.
    return Page(
        [{"id": r["id"], **{path: _plain(r[label]) for path, label, _e in selected}} for r in page],
        next_cursor,
        total,
    )


async def list_interactions(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    contact_id: str | None = None,
    organization_id: str | None = None,
    opportunity_id: str | None = None,
    kind: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Interaction], str | None]:
    """One record's interactions, newest first, with keyset pagination.

    Exactly one of ``contact_id``, ``organization_id`` and ``opportunity_id`` names the
    record; it is read first, so an id out of scope is that record's not-found. An
    organization's interactions are found by the ``organization_id`` frozen on each at log
    time, so a renamed organization still has its history. ``kind`` keeps one kind, matched
    exactly. Returns ``(interactions, next_cursor)``.
    """
    named = [
        name
        for name, value in (
            ("contact_id", contact_id),
            ("organization_id", organization_id),
            ("opportunity_id", opportunity_id),
        )
        if value is not None
    ]
    if len(named) != 1:
        raise ValueError(
            "pass exactly one of contact_id, organization_id or opportunity_id"
            + (f"; this call passed {', '.join(named)}" if named else "")
        )
    limit = _clamp_limit(limit)
    if contact_id is not None:
        await _contact_row(conn, contact_id)
        owner = interaction.c.contact_id == contact_id
    elif organization_id is not None:
        await _org_row(conn, organization_id)
        owner = interaction.c.organization_id == organization_id
    else:
        assert opportunity_id is not None
        await _ensure_pipeline(conn, ctx)
        await _opportunity_row(conn, opportunity_id)
        owner = interaction.c.opportunity_id == opportunity_id
    stmt = select(*_INTERACTION_COLUMNS).where(*_scope(interaction, ctx), owner)
    if kind is not None and kind.strip():
        stmt = stmt.where(interaction.c.kind == kind.strip())
    stmt = _INTERACTIONS.apply(stmt, cursor)
    rows = await _all(conn, stmt.limit(limit + 1))
    page, next_cursor = _INTERACTIONS.paginate(rows, limit)
    _log_access(ctx, entity="interaction", action="list")
    return [Interaction.from_row(r) for r in page], next_cursor


# --------------------------------------------------------------------------- #
# Similar-companies graph                                                       #
# --------------------------------------------------------------------------- #


async def archive_record(
    conn: AsyncConnection, ctx: Principal, *, entity: str, record_id: str, archived: bool
) -> Contact | Organization:
    """Archive a contact or an organization, or restore one: set or clear ``archived_at``.

    Archiving hides the record from the default listings and counts and changes nothing
    else; restoring brings it back. Archiving an archived record keeps its first stamp, and
    restoring a live one changes nothing, so a repeat leaves the record as the first call
    did. Not-found when the id is out of scope.
    """
    table = contact if entity == "contact" else organization
    row = (
        await _contact_row(conn, record_id)
        if entity == "contact"
        else await _org_row(conn, record_id)
    )
    if (row["archived_at"] is None) == archived:
        now = _now()
        await conn.execute(
            table.update()
            .where(table.c.id == record_id)
            .values(archived_at=now if archived else None, updated_at=now)
        )
        await write_audit(
            conn,
            ctx,
            entity=entity,
            action="archive" if archived else "restore",
            outcome="ok",
            entity_id=record_id,
        )
    if entity == "contact":
        fresh = await _contact_row(conn, record_id)
        return Contact.from_row(fresh, retired=await _retired_for(conn, "contact"))
    fresh = await _org_row(conn, record_id)
    return Organization.from_row(fresh, retired=await _retired_for(conn, "organization"))


async def promote_organization(
    conn: AsyncConnection, ctx: Principal, *, org_id: str
) -> Organization:
    """Flip a ``discovered`` org to ``active`` so it enters the default CRM views.

    The one explicit transition that moves an auto-persisted lookalike into the
    real contact list. 404 (not 403) if the id is out of scope. Setting
    ``status="active"`` on an already-active org is a harmless no-op.
    """
    await _org_row(conn, org_id)  # 404 if out of scope
    await conn.execute(
        organization.update()
        .where(organization.c.id == org_id)
        .values(status="active", updated_at=_now())
    )
    fresh = await _org_row(conn, org_id)
    await write_audit(
        conn, ctx, entity="organization", action="promote", outcome="ok", entity_id=org_id
    )
    return Organization.from_row(fresh, retired=await _retired_for(conn, "organization"))


async def _upsert_edge(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    from_id: str,
    to_id: str,
    relation: str,
    score: float | None,
    source: str | None,
) -> OrganizationEdge:
    """Idempotently upsert ONE org↔org edge (no endpoint validation) + audit it.

    The edge-write core of :func:`link_similar_companies`. It assumes BOTH endpoints
    are already known to be in-scope — the caller is responsible for that check: the
    loop in ``link_similar_companies`` validated the seed once at the top and just
    upserted each target, so both endpoints are known-good. The insert is idempotent
    under the unique index ``uq_organization_edge`` on
    ``(tenant_id, workspace_id, from, to, relation)`` via ON CONFLICT DO NOTHING + reselect
    (:func:`_upsert_returning`), so a re-linked lookalike set converges. Writes the
    ``create_edge`` audit row.
    """
    relation = relation.strip()
    now = _now()
    row, _created = await _upsert_returning(
        conn,
        table=organization_edge,
        insert_values={
            "id": _new_edge_id(),
            **ctx.identity_columns(),
            "from_organization_id": from_id,
            "to_organization_id": to_id,
            "relation": relation,
            "score": score,
            "source": _clean(source),
            "created_at": now,
            "updated_at": now,
        },
        conflict_index_elements=[
            organization_edge.c.tenant_id,
            organization_edge.c.workspace_id,
            organization_edge.c.from_organization_id,
            organization_edge.c.to_organization_id,
            organization_edge.c.relation,
        ],
        # The index leaves marked edges out; _upsert_returning names that predicate.
        conflict_where=None,
        reselect_predicate=and_(
            organization_edge.c.tenant_id == ctx.tenant_id,
            organization_edge.c.workspace_id == ctx.workspace_id,
            organization_edge.c.from_organization_id == from_id,
            organization_edge.c.to_organization_id == to_id,
            organization_edge.c.relation == relation,
        ),
    )
    await write_audit(
        conn,
        ctx,
        entity="organization_edge",
        action="create_edge",
        outcome="ok",
        entity_id=row["id"],
    )
    return OrganizationEdge.from_row(row)


async def link_similar_companies(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    org_id: str,
    companies: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Persist a lookalike set into the graph as ``similar_to`` edges from a seed org.

    The host hands in a similar-companies result from whichever tool found it (**People
    never fetches lookalikes itself**). For each company:

      * A row without a resolvable ``domain`` can't be domain-deduped or acted on
        downstream (no domain → no email discovery), so it is **skipped and counted
        in ``skipped``**, never created as a dangling org. A row whose field trips a
        length cap is likewise **skipped and counted** — one unusable row never rolls
        back the whole batch (only genuine DB errors propagate).
      * A domain already processed EARLIER in this same batch is an idempotent
        duplicate (the unique index would collapse it to one edge), so it is skipped
        WITHOUT counting — neither ``linked`` nor ``skipped``.
      * Otherwise the Organization is upserted by the normalized-domain dedupe key.
        A **newly-inserted** lookalike carries ``source="similar_companies"``,
        ``status="discovered"``; an **existing** org is left completely untouched —
        its ``status`` / ``source`` (and everything else) are never restamped, so a
        real ``active`` customer that surfaces as a lookalike is never downgraded.
      * Either way a ``similar_to`` edge is created from the seed to that org (the
        edge carries its own ``source``/``score``).

    Idempotent on both the org (domain) and the edge
    (``from, to, relation``), so re-linking converges. Returns
    ``{linked, created, skipped}``: ``created`` = newly-inserted orgs, ``linked`` =
    edges written, ``skipped`` = rows dropped as unusable (no resolvable domain, or a
    field over the length cap).
    """
    await _org_row(conn, org_id)  # validate the seed in-scope; 404 otherwise
    linked = 0
    created = 0
    skipped = 0
    seen: set[str] = set()  # normalized domains already processed in THIS batch
    for company in companies:
        domain = normalize_domain(company.get("domain"))
        if domain is None:
            # No resolvable domain → can't dedupe or act on it downstream. Unusable.
            skipped += 1
            continue
        if domain in seen:
            # Already linked this domain earlier in the batch — the unique index would
            # collapse it to the same edge. An idempotent duplicate, not unusable: skip
            # WITHOUT counting (neither linked nor skipped).
            continue
        try:
            # Per-row field validation ONLY. A field over its cap makes this one row
            # unusable; drop it (skipped) rather than let its ValueError roll back the
            # whole batch. The domain is NOT added to `seen` here, so a malformed first
            # occurrence does not pre-consume the domain for a valid later one.
            name = _clean(company.get("name")) or domain  # fall back to the domain as a name
            industry = _clean(company.get("industry"))
            employee_range = _clean(company.get("employee_range"))
            revenue_range = _clean(company.get("revenue_range"))
            headquarters = _clean(company.get("headquarters"))
            _check_len("name", name, _CAP_NAME)
            _check_len("domain", domain, _CAP_DOMAIN)
            _check_len("industry", industry, _CAP_SHORT)
            _check_len("employee_range", employee_range, _CAP_SHORT)
            _check_len("revenue_range", revenue_range, _CAP_SHORT)
            _check_len("headquarters", headquarters, _CAP_SHORT)
        except ValueError:
            skipped += 1
            continue

        now = _now()
        # Provenance flags apply to newly-inserted orgs ONLY: ON CONFLICT DO NOTHING
        # so a domain match to an existing org leaves it entirely untouched (never
        # restamps status/source) — we just reuse its id as the edge target.
        row, was_created = await _upsert_returning(
            conn,
            table=organization,
            insert_values={
                "id": _new_org_id(),
                **ctx.identity_columns(),
                "name": name,
                "domain": domain,
                "industry": industry,
                "employee_range": employee_range,
                "revenue_range": revenue_range,
                "headquarters": headquarters,
                "source": "similar_companies",
                "status": "discovered",
                "created_at": now,
                "updated_at": now,
            },
            conflict_index_elements=[
                organization.c.tenant_id,
                organization.c.workspace_id,
                organization.c.domain_key,
            ],
            conflict_where=organization.c.domain.isnot(None),
            reselect_predicate=and_(
                organization.c.tenant_id == ctx.tenant_id,
                organization.c.workspace_id == ctx.workspace_id,
                organization.c.domain_key == address_key(domain),
            ),
        )
        if was_created:
            created += 1
        # Both endpoints are known in-scope here (the seed was validated at the top;
        # the target was just upserted), so go straight to the edge write.
        await _upsert_edge(
            conn,
            ctx,
            from_id=org_id,
            to_id=row["id"],
            relation="similar_to",
            score=company.get("similarity"),
            source="similar_companies",
        )
        linked += 1
        seen.add(domain)  # mark processed only on success (see the malformed-skip note)

    # One durable summary on the seed org (counts only — no PII), alongside the
    # per-edge create_edge rows _upsert_edge already writes.
    await write_audit(
        conn,
        ctx,
        entity="organization",
        action="link_similar_companies",
        outcome="ok",
        entity_id=org_id,
        detail={"linked": linked, "created": created, "skipped": skipped},
    )
    return {"linked": linked, "created": created, "skipped": skipped}


async def list_similar_companies(
    conn: AsyncConnection, ctx: Principal, *, org_id: str, limit: int = _DEFAULT_LIMIT
) -> list[tuple[Organization, float | None, datetime]]:
    """A seed org's ``similar_to`` edges joined to their target Organization rows.

    Returns a list of ``(Organization, score, discovered_at)`` — the target org, the
    edge's ``score`` (the lookalike's similarity), and the edge's ``created_at`` (the
    discovery time) — ordered by similarity (highest first, nulls last), then newest
    edge, capped at ``limit`` (clamped to ``_MAX_LIMIT``). Returns the top-N by
    similarity — the panel's need; there is no pagination cursor. 404 (not 403) if
    the seed is out of scope. This is the read the UI's Similar Companies panel renders.
    """
    limit = _clamp_limit(limit)
    await _org_row(conn, org_id)  # 404 if the seed is out of scope
    edge_created_at = organization_edge.c.created_at.label("edge_created_at")
    stmt = (
        select(organization, organization_edge.c.score, edge_created_at)
        .select_from(
            organization_edge.join(
                organization, organization_edge.c.to_organization_id == organization.c.id
            )
        )
        .where(
            and_(
                *_scope(organization_edge, ctx),
                _live(organization),
                _ORG_NOT_ARCHIVED,
                organization_edge.c.from_organization_id == org_id,
                organization_edge.c.relation == "similar_to",
            )
        )
        .order_by(
            organization_edge.c.score.is_(None),  # non-null similarities first (nulls last)
            organization_edge.c.score.desc(),
            organization_edge.c.created_at.desc(),
        )
        .limit(limit)
    )
    rows = await _all(conn, stmt)
    retired = (await _retired_marks(conn)).get("organization")
    _log_access(ctx, entity="organization", action="list_similar", entity_id=org_id)
    return [
        (Organization.from_row(r, retired=retired), r["score"], r["edge_created_at"]) for r in rows
    ]


# --------------------------------------------------------------------------- #
# Internal: org-edge resolution, merge, pagination, ranking                    #
# --------------------------------------------------------------------------- #


async def _fill_empty(
    conn: AsyncConnection,
    table: Any,
    row: RowMapping,
    candidate: Mapping[str, Any],
    *,
    db_clock: bool = False,
) -> RowMapping:
    """Fill only the EMPTY fields of ``row`` from ``candidate``; re-select and return.

    The dedupe-merge rule: a match never overwrites existing data; it only fills
    holes. If nothing to fill, the row is returned unchanged (no write, no
    ``updated_at`` bump). ``db_clock`` stamps ``updated_at`` with the database's
    ``now()`` instead of this process's clock, for a table whose ``updated_at`` orders a
    change read (``signal``).
    """
    updates = _merge_empty_only(row, candidate)
    if not updates:
        return row
    updates["updated_at"] = db_now(conn.dialect.name) if db_clock else _now()
    try:
        await conn.execute(
            table.update().where(table.c.id == row["id"]).values(**_with_keys(table, updates))
        )
    except IntegrityError as exc:
        # Defensive: a fill writes indexed dedupe columns too (email / domain), so a
        # concurrent insert of the same value could collide under the partial unique
        # index. Translate to a domain conflict naming the field — never the index.
        raise ConflictError(_conflicting_field(updates)) from exc
    fresh = await _one(conn, select(table).where(table.c.id == row["id"]))
    return fresh if fresh is not None else row


def _conflicting_field(updates: Mapping[str, Any]) -> str:
    """Pick the indexed dedupe field present in an UPDATE's SET clause.

    The unique-indexed columns a write touches are ``email`` (contact) and
    ``domain`` / ``name`` (organization — ``uq_org_domain`` and
    ``uq_org_name_when_domainless`` respectively). Name whichever is present so
    the conflict message is field-specific; default to the generic ``"record"``
    if none is (should not happen for an IntegrityError, but never leak a
    constraint name).

    ``domain`` is checked before ``name`` because an update carrying both can
    only collide on ``domain``: the name index covers domain-less rows only, so
    a row that just acquired a domain is outside it.
    """
    for field in ("email", "domain", "name"):
        if field in updates:
            return field
    return "record"


def _partial_updates(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Build the SET clause for a partial update: apply non-``None`` values only.

    An omitted field arrives as ``None`` and is left untouched. The public tool
    surface never passes a sentinel to clear a field, so omission is the only
    semantics callers can express.
    """
    return {k: v for k, v in candidate.items() if v is not None}


async def _count(conn: AsyncConnection, table: Any, predicate: Any) -> int:
    result = await conn.execute(select(func.count()).select_from(table).where(predicate))
    return int(result.scalar_one())


def _clamp_limit(limit: int) -> int:
    if limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


# Each paged read's total order, declared once: newest first, with the id breaking ties
# among rows written in one transaction. The name tags its cursors, so a cursor from one
# list tool is refused by another (pgkeyset, docs/PAGING-CONTRACT.md).
_CONTACTS = Listing("contacts", Sort.desc(contact.c.created_at), Sort.desc(contact.c.id))
_ORGANIZATIONS = Listing(
    "organizations", Sort.desc(organization.c.created_at), Sort.desc(organization.c.id)
)
_OPPORTUNITIES = Listing(
    "opportunities", Sort.desc(opportunity.c.created_at), Sort.desc(opportunity.c.id)
)
_SIGNALS = Listing("signals", Sort.desc(signal.c.created_at), Sort.desc(signal.c.id))
# The change read: oldest change first, so a reader's mark only moves forward. Its own
# name, so a listing cursor is refused as a change mark and the reverse.
_SIGNAL_CHANGES = Listing("signal_changes", Sort.asc(signal.c.updated_at), Sort.asc(signal.c.id))
_PROOF_POINTS = Listing(
    "proof_points", Sort.desc(proof_point.c.created_at), Sort.desc(proof_point.c.id)
)
# A record's history: the most recent touch first, by when it happened.
_INTERACTIONS = Listing(
    "interactions", Sort.desc(interaction.c.occurred_at), Sort.desc(interaction.c.id)
)


def _escape_like(value: str) -> str:
    """Escape ILIKE wildcards so a literal ``%``/``_`` in the query isn't a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _token_predicate(tokens: Sequence[str], columns: Sequence[Any]) -> Any:
    """Build the search WHERE clause: AND across tokens, OR across columns.

    Each token must be a case-insensitive substring of AT LEAST ONE of ``columns``;
    all tokens must match (AND). This is the tokenizing that lets ``"Matt Calloway"``
    find ``"Matthew Calloway"`` — the whole query is not a contiguous substring of
    the name, but every token is. Per-token wildcards are escaped, so a literal
    ``%``/``_`` in a token is matched literally, not as a wildcard.
    """
    return and_(
        *(
            or_(*(col.ilike(f"%{_escape_like(tok)}%", escape="\\") for col in columns))
            for tok in tokens
        )
    )


def _terms_predicate(terms: str | None, columns: Sequence[Any]) -> Any:
    """The match a listing's free-text ``query`` adds, or None when it names no token.

    The text is split on whitespace; a row matches when every token is a case-insensitive
    substring of at least one of ``columns`` (:func:`_token_predicate`). Tokens narrow, and
    word order does not matter, so "Matt Calloway" finds "Matthew Calloway". The listing
    keeps its own order and cursor; the match is one more condition on it.
    """
    _check_len("query", terms, _CAP_SHORT)
    tokens = (terms or "").split()
    return _token_predicate(tokens, columns) if tokens else None


def _new_external_ref_id() -> str:
    from mcp_people.ids import new_external_ref_id

    return new_external_ref_id()


def _new_org_id() -> str:
    from mcp_people.ids import new_organization_id

    return new_organization_id()


def _new_contact_id() -> str:
    from mcp_people.ids import new_contact_id

    return new_contact_id()


def _new_interaction_id() -> str:
    from mcp_people.ids import new_interaction_id

    return new_interaction_id()


def _new_edge_id() -> str:
    from mcp_people.ids import new_organization_edge_id

    return new_organization_edge_id()


def _new_opportunity_id() -> str:
    from mcp_people.ids import new_opportunity_id

    return new_opportunity_id()


def _new_signal_id() -> str:
    from mcp_people.ids import new_signal_id

    return new_signal_id()


def _new_proof_point_id() -> str:
    from mcp_people.ids import new_proof_point_id

    return new_proof_point_id()


def _new_field_def_id() -> str:
    from mcp_people.ids import new_field_def_id

    return new_field_def_id()


# --------------------------------------------------------------------------- #
# Custom fields                                                                #
# --------------------------------------------------------------------------- #
#
# A value lives on its entity row's ``fields`` JSONB as an envelope; a definition
# lives in ``field_def``. Every write merges in SQL (``||`` / ``-``), never by
# reading the object and writing it back, so two writers setting different keys
# on one row both land.

_CAP_FIELDS_BY = 128


def _email_grade(
    email_status: str | None, email_checked_at: datetime | None
) -> tuple[str, datetime] | None:
    """The ``(status, checked_at)`` pair to store, or ``None`` when no grade was given.

    The two travel together: a status is stamped now unless the caller knows when
    the check ran, and a timestamp with no status describes nothing.
    """
    if email_status is None:
        if email_checked_at is not None:
            raise ValueError("email_checked_at is set together with email_status")
        return None
    status = validate_choice("email_status", email_status.lower(), EMAIL_STATUSES)
    return status, _ensure_utc(email_checked_at) or _now()


async def _field_defs(conn: AsyncConnection, entity: str) -> dict[str, RowMapping]:
    rows = await _all(
        conn,
        select(field_def)
        .where(field_def.c.entity == entity)
        .order_by(field_def.c.created_at, field_def.c.id),
    )
    return {row["key"]: row for row in rows}


def _live_count(defs: Mapping[str, RowMapping]) -> int:
    """How many of one entity's definitions are live: the ones the cap counts."""
    return sum(1 for row in defs.values() if row["retired_at"] is None)


# Which entities' fields a path on each entity can name: its own bare, and the
# others under `<entity>.`.
_REACHES: dict[str, tuple[str, ...]] = {
    "contact": ("contact", "organization"),
    "organization": ("organization",),
    "opportunity": ("opportunity", "organization", "contact"),
}


@dataclass(frozen=True)
class Replacement:
    """Where a retired field's ``replaced_by`` chain ends.

    ``live`` is the live ``(entity, key)`` the chain reaches. When it reaches none,
    ``retired`` is the retired field it stops at, or ``None`` when the chain names
    no replacement at all.
    """

    live: tuple[str, str] | None
    retired: tuple[str, str] | None = None


async def _all_field_defs(conn: AsyncConnection) -> dict[tuple[str, str], RowMapping]:
    """Every definition in the workspace, retired included, by ``(entity, key)``."""
    rows = await _all(conn, select(field_def).order_by(field_def.c.created_at, field_def.c.id))
    return {(row["entity"], row["key"]): row for row in rows}


async def _retired_marks(conn: AsyncConnection) -> dict[str, dict[str, dict[str, Any]]]:
    """Each entity's retired fields, by key, as the mark a by-id read gives the value.

    ``{entity: {key: {retired_at, replaced_by?}}}``; a listing drops these keys from
    its rows. One read serves a whole listing or search, not one per row.
    """
    rows = await _all(conn, select(field_def).where(field_def.c.retired_at.is_not(None)))
    marks: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        defined = FieldDef.from_row(row)
        mark: dict[str, Any] = {"retired_at": defined.retired_at}
        if defined.replaced_by is not None:
            mark["replaced_by"] = defined.replaced_by
        marks.setdefault(defined.entity, {})[defined.key] = mark
    return marks


async def _retired_for(conn: AsyncConnection, entity: str) -> dict[str, dict[str, Any]]:
    """One entity's retired fields as :func:`_retired_marks` gives them: what a
    single record returned without ``field_details`` (a write's result, a timeline)
    leaves out of its ``fields``."""
    return (await _retired_marks(conn)).get(entity, {})


def _follow_replacement(
    defs: Mapping[tuple[str, str], RowMapping], entity: str, key: str
) -> Replacement:
    """Follow ``(entity, key)``'s ``replaced_by`` chain to the first live field.

    Each step is validated live when it is set, but a step can retire later, so
    the chain is followed rather than read one hop deep. A field already visited
    ends it, so a cycle cannot loop.
    """
    seen: set[tuple[str, str]] = set()
    at = (entity, key)
    while True:
        seen.add(at)
        row = defs.get(at)
        if row is None or row["replaced_by_key"] is None:
            return Replacement(live=None, retired=at if at != (entity, key) else None)
        nxt = (row["replaced_by_entity"] or row["entity"], row["replaced_by_key"])
        target = defs.get(nxt)
        if target is None or nxt in seen:
            return Replacement(live=None, retired=at)
        if target["retired_at"] is None:
            return Replacement(live=nxt)
        at = nxt


async def live_replacement(conn: AsyncConnection, entity: str, key: str) -> Replacement:
    """The live field a retired ``(entity, key)`` is replaced by, following the chain."""
    return _follow_replacement(await _all_field_defs(conn), entity, key)


def _field_path(entity: str, key: str, *, relative_to: str) -> str | None:
    """``(entity, key)`` as a path on ``relative_to``'s rows, or ``None`` when its
    paths cannot reach that entity."""
    if entity == relative_to:
        return key
    if entity in _REACHES[relative_to]:
        return f"{entity}.{key}"
    return None


def _replacement_hint(
    defs: Mapping[tuple[str, str], RowMapping],
    entity: str,
    key: str,
    *,
    relative_to: str,
    writing: bool = False,
) -> str:
    """One sentence naming what replaces a retired field, for a refusal's ``next_step``.

    The replacement is named as a path on ``relative_to``'s rows when they reach it,
    and as ``<entity> field <key>`` when they do not. A write sets only its own
    entity's fields, so for ``writing`` a replacement on another entity names the
    update that sets it there.
    """
    found = _follow_replacement(defs, entity, key)
    if found.live is not None:
        live_entity, live_key = found.live
        if writing and live_entity != relative_to:
            return (
                f"Its replacement is the {live_entity} field {live_key!r}: set it on the "
                f"{live_entity} with update_{live_entity}({live_entity}_id=..., "
                f"fields={{{live_key!r}: ...}})."
            )
        path = _field_path(live_entity, live_key, relative_to=relative_to)
        if path is not None:
            return f"Use {path!r} instead, the live field that replaces it."
        return (
            f"Its replacement is the {live_entity} field {live_key!r}; set or read it "
            f"on the {live_entity}."
        )
    if found.retired is not None:
        end_entity, end_key = found.retired
        return (
            f"Its replacement chain ends at the {end_entity} field {end_key!r}, which is "
            f"retired too. Read list_fields(entity={relative_to!r}) for the live fields."
        )
    return f"It names no replacement. Read list_fields(entity={relative_to!r}) for the live fields."


def _blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


def _clears(definition: RowMapping, value: Any) -> bool:
    """Whether a submitted value removes the field: ``null``, or a blank text value."""
    return value is None or (definition["type"] == "text" and _blank(value))


async def _field_patch(
    conn: AsyncConnection,
    *,
    entity: str,
    submitted: Mapping[str, Any] | None,
    by: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate a ``fields={…}`` write: ``(envelopes to set, keys to remove)``.

    Every key must already be defined for ``entity`` in this workspace. An
    undefined key is refused rather than created, because a misspelt key would
    otherwise become a field nobody filters on, holding the value the real field
    is missing. A retired key takes only ``null``, which clears the value a row
    still holds; any other value is refused with the field that replaces it. A
    blank string clears a text field as ``null`` does: there is no blank text value.
    """
    if not submitted:
        return {}, []
    if not isinstance(submitted, Mapping):
        raise ValueError("fields must be an object of key: value")
    _check_len("fields_by", by, _CAP_FIELDS_BY)
    # A shared lock on the definitions written, taken before they are read: an option being
    # removed (``update_field``) waits for this write, or this write reads the options left.
    await conn.execute(
        select(field_def.c.id)
        .where(field_def.c.entity == entity, field_def.c.key.in_(sorted(map(str, submitted))))
        .with_for_update(read=True)
    )
    defs = await _field_defs(conn, entity)
    retired = sorted(
        key
        for key, raw in submitted.items()
        if key in defs
        and defs[key]["retired_at"] is not None
        and not _clears(defs[key], parse_input(key, raw).value)
    )
    if retired:
        hint = _replacement_hint(
            await _all_field_defs(conn), entity, retired[0], relative_to=entity, writing=True
        )
        raise FieldError(f"{entity} field {retired[0]!r} is retired", next_step=hint)
    unknown = sorted(str(key) for key in submitted if key not in defs)
    if unknown:
        raise FieldError(
            f"no {entity} field named {', '.join(repr(k) for k in unknown)} is defined "
            "in this workspace",
            next_step=(
                f"Read list_fields(entity={entity!r}) for the defined keys, or define "
                f"a new field with define_field(entity={entity!r}, key=..., label=..., "
                "type=...) and retry. If you cannot call define_field, tell the user which "
                "field is missing, and never write the value into a dossier instead."
            ),
        )
    now, default_by = _now(), _clean(by)
    patch: dict[str, Any] = {}
    removed: list[str] = []
    for key, raw in submitted.items():
        given = parse_input(key, raw)
        definition = defs[key]
        if _clears(definition, given.value):
            removed.append(key)
            continue
        value = coerce_value(
            entity=entity,
            key=key,
            field_type=definition["type"],
            options=definition["options"],
            value=given.value,
            minimum=definition["minimum"],
            maximum=definition["maximum"],
        )
        patch[key] = _storable(
            key, envelope(value, by=given.by or default_by, at=now, why=given.why)
        )
    return patch, removed


def _storable(key: str, stored: Any) -> Any:
    """``stored``, refused when it holds a NUL character anywhere.

    Postgres cannot store one in ``jsonb`` and refuses the write; SQLite would store it.
    Refusing it here makes the write fail the same way on both, before the database.
    """

    def has_nul(value: Any) -> bool:
        if isinstance(value, str):
            return "\x00" in value
        if isinstance(value, Mapping):
            return any(has_nul(k) or has_nul(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(has_nul(v) for v in value)
        return False

    if has_nul(stored):
        raise ValueError(f"field {key!r} holds a NUL character, which cannot be stored")
    return stored


async def _field_fill_patch(
    conn: AsyncConnection,
    *,
    entity: str,
    submitted: Mapping[str, Any] | None,
    by: str | None,
) -> dict[str, Any]:
    """Validate ``fields`` for a find-or-create, which fills and never removes.

    A blank string is absent, as a blank cell in an imported list is, so it fills nothing.
    """
    if submitted and isinstance(submitted, Mapping):
        submitted = {
            key: raw for key, raw in submitted.items() if not _blank(parse_input(key, raw).value)
        }
    patch, removed = await _field_patch(conn, entity=entity, submitted=submitted, by=by)
    if removed:
        raise ValueError(
            f"find_or_create fills fields and cannot clear {removed[0]!r}; clear it with "
            f"update_{entity}"
        )
    return patch


def _fields_written(
    conn: AsyncConnection, table: Any, patch: Mapping[str, Any], removed: Sequence[str]
) -> Any:
    """``fields`` with ``patch``'s keys replaced and ``removed``'s keys dropped."""
    return dialect.fields_written(conn.dialect.name, table.c.fields, patch, removed)


def _fields_filled(conn: AsyncConnection, table: Any, patch: Mapping[str, Any]) -> Any:
    """``fields`` with ``patch``'s keys added where absent; a key already held wins."""
    return dialect.fields_filled(conn.dialect.name, table.c.fields, patch)


def _fields_detail(patch: Mapping[str, Any], removed: Sequence[str]) -> dict[str, Any] | None:
    """The audit detail for a custom-field write: key names only, never values."""
    keys = sorted([*patch, *removed])
    return {"fields": keys} if keys else None


def _inherited_fields(target_row: RowMapping, source_row: RowMapping) -> dict[str, Any]:
    """The custom fields a merge target takes from its source: the keys it lacks."""
    held = target_row["fields"] or {}
    return {key: env for key, env in (source_row["fields"] or {}).items() if key not in held}


# --- The paths a listing can name -------------------------
#
# A path is a core column, a rollup over a related table, or a custom field's
# key. A rollup is one aggregate over its table, joined to the listing: grouped
# once per query, not computed per row. A correlated scalar subquery reads the
# same but runs once for every row the listing considers, which at tens of
# thousands of rows is the difference between milliseconds and a minute. The
# aggregates run under the same RLS session as the listing, so they count only
# what the caller can see.

# A link from a live record to a deleted one reads as absent: a deal's
# primary contact, an interaction's organization and opportunity.
_PRIMARY_CONTACT = _live_link(opportunity.c.primary_contact_id, contact)
_INTERACTION_COLUMNS: tuple[Any, ...] = (
    *(c for c in interaction.c if c.name not in ("organization_id", "opportunity_id")),
    _live_link(interaction.c.organization_id, organization).label("organization_id"),
    _live_link(interaction.c.opportunity_id, opportunity).label("opportunity_id"),
)

_SIGNAL_COUNTS = (
    select(signal.c.organization_id, func.count().label("active"))
    .where(signal.c.status == ACTIVE, _live(signal))
    .group_by(signal.c.organization_id)
    .subquery("signal_counts")
)
_INTERACTION_STATS = (
    select(
        interaction.c.contact_id,
        func.count().label("count"),
        func.max(interaction.c.occurred_at).label("last_at"),
    )
    .where(_live(interaction))
    .group_by(interaction.c.contact_id)
    .subquery("interaction_stats")
)
_CONTACT_COUNTS = (
    select(contact.c.organization_id, func.count().label("count"))
    .where(and_(contact.c.organization_id.isnot(None), _NOT_ARCHIVED, _live(contact)))
    .group_by(contact.c.organization_id)
    .subquery("contact_counts")
)
_DEAL_INTERACTION_STATS = (
    select(
        interaction.c.opportunity_id,
        func.count().label("count"),
        func.max(interaction.c.occurred_at).label("last_at"),
    )
    .where(interaction.c.opportunity_id.isnot(None), _live(interaction))
    .group_by(interaction.c.opportunity_id)
    .subquery("deal_interaction_stats")
)

# A deal's stage row: its outcome, the default probability
# it takes, and from those its weighted value. Every opportunity read joins it.
_STAGE = pipeline_stage.alias("stage_def")
_STAGE_JOIN = opportunity.outerjoin(
    _STAGE,
    and_(
        _STAGE.c.tenant_id == opportunity.c.tenant_id,
        _STAGE.c.workspace_id == opportunity.c.workspace_id,
        _STAGE.c.key == opportunity.c.stage,
    ),
)
_OPPORTUNITY_OUTCOME = func.coalesce(_STAGE.c.outcome, literal(OPEN))
_EFFECTIVE_PROBABILITY = func.coalesce(opportunity.c.probability, _STAGE.c.probability)
_WEIGHTED_VALUE = as_real(opportunity.c.value) * _EFFECTIVE_PROBABILITY / 100


def _core_ref(
    path: str,
    kind: str,
    expr: Any,
    options: Sequence[str] = (),
    joins: str | None = None,
    key: Any = None,
    fold: Any = None,
) -> query.Ref:
    return query.Ref(
        path=path,
        kind=kind,
        value=expr,
        present=expr.isnot(None),
        scalar=expr,
        options=tuple(options),
        joins=joins,
        key=key,
        fold=fold,
    )


def _count_ref(path: str, counted: Any, joins: str, projected: Any) -> query.Ref:
    """A rollup count: zero for a row the aggregate has no group for. It compares
    as ``numeric`` so a filter value binds one way. ``projected`` is the same count
    for one listed row (see :func:`_per_row`)."""
    count = func.coalesce(counted, 0)
    return query.Ref(
        path=path,
        kind="number",
        value=count,
        present=literal(True),
        scalar=cast(count, Numeric),
        joins=joins,
        projected=projected,
    )


def _per_row(aggregate: Any, related: Any, listed: Any, *where: Any) -> Any:
    """``aggregate`` over ``related``'s rows for one ``listed`` row, as a correlated subquery.

    A listing that only shows a rollup reads it this way, so the database computes it
    for the page's rows alone. The related row's ``tenant_id`` and ``workspace_id`` are matched to
    the listed row's, which is the prefix of every index on a related table; without
    them the lookup cannot use one.
    """
    return (
        select(aggregate)
        .where(
            related.c.tenant_id == listed.c.tenant_id,
            related.c.workspace_id == listed.c.workspace_id,
            *((_live(related),) if "deleted_at" in related.c else ()),
            *where,
        )
        .correlate(listed)
        .scalar_subquery()
    )


_CONTACT_REFS: dict[str, query.Ref] = {
    ref.path: ref
    for ref in (
        _core_ref("name", "text", contact.c.name, key=contact.c.name_key, fold=match_key),
        _core_ref("email", "text", contact.c.email, key=contact.c.email_key, fold=address_key),
        _core_ref("email_status", "select", contact.c.email_status, sorted(EMAIL_STATUSES)),
        _core_ref("email_checked_at", "timestamp", contact.c.email_checked_at),
        _core_ref("phone", "text", contact.c.phone),
        _core_ref("title", "text", contact.c.title),
        _core_ref("organization_id", "text", contact.c.organization_id),
        # The stage a reader sees: both names are the effective status, which is what
        # the `status` argument filters on too.
        _core_ref("status", "text", _EFFECTIVE_STATUS),
        _core_ref("effective_status", "text", _EFFECTIVE_STATUS),
        _core_ref("archived_at", "timestamp", contact.c.archived_at),
        _core_ref("dossier_updated_at", "timestamp", contact.c.dossier_updated_at),
        _core_ref("lead_source", "text", contact.c.lead_source),
        _core_ref("source", "text", contact.c.source),
        _core_ref("created_at", "timestamp", contact.c.created_at),
        _core_ref("updated_at", "timestamp", contact.c.updated_at),
        # Signals hang off the account, so a contact's are its organization's.
        _count_ref(
            "signals.active",
            _SIGNAL_COUNTS.c.active,
            "signals",
            _per_row(
                func.count(),
                signal,
                contact,
                signal.c.organization_id == contact.c.organization_id,
                signal.c.status == ACTIVE,
            ),
        ),
        _count_ref(
            "interactions.count",
            _INTERACTION_STATS.c.count,
            "interactions",
            _per_row(func.count(), interaction, contact, interaction.c.contact_id == contact.c.id),
        ),
        replace(
            _core_ref(
                "interactions.last_at",
                "timestamp",
                _INTERACTION_STATS.c.last_at,
                joins="interactions",
            ),
            projected=_per_row(
                func.max(interaction.c.occurred_at),
                interaction,
                contact,
                interaction.c.contact_id == contact.c.id,
            ),
        ),
    )
}

_ORGANIZATION_REFS: dict[str, query.Ref] = {
    ref.path: ref
    for ref in (
        _core_ref("name", "text", organization.c.name, key=organization.c.name_key, fold=match_key),
        _core_ref(
            "domain", "text", organization.c.domain, key=organization.c.domain_key, fold=address_key
        ),
        _core_ref("website", "text", organization.c.website),
        _core_ref("industry", "text", organization.c.industry),
        _core_ref("region", "text", organization.c.region),
        _core_ref("employee_range", "text", organization.c.employee_range),
        _core_ref("revenue_range", "text", organization.c.revenue_range),
        _core_ref("headquarters", "text", organization.c.headquarters),
        _core_ref("status", "text", organization.c.status),
        _core_ref("archived_at", "timestamp", organization.c.archived_at),
        _core_ref("source", "text", organization.c.source),
        _core_ref("dossier_updated_at", "timestamp", organization.c.dossier_updated_at),
        _core_ref("created_at", "timestamp", organization.c.created_at),
        _core_ref("updated_at", "timestamp", organization.c.updated_at),
    )
}

# Rollups that only make sense with the organization as the listed row.
_ORGANIZATION_ROLLUPS: dict[str, query.Ref] = {
    ref.path: ref
    for ref in (
        _count_ref(
            "signals.active",
            _SIGNAL_COUNTS.c.active,
            "signals",
            _per_row(
                func.count(),
                signal,
                organization,
                signal.c.organization_id == organization.c.id,
                signal.c.status == ACTIVE,
            ),
        ),
        _count_ref(
            "contacts.count",
            _CONTACT_COUNTS.c.count,
            "contacts",
            _per_row(
                func.count(),
                contact,
                organization,
                contact.c.organization_id == organization.c.id,
                _NOT_ARCHIVED,
            ),
        ),
    )
}

# A deal's own paths. The stage-derived ones join the stage row; `close_date`
# compares as its ISO text, the way a custom date does, so one value binds both.
_OPPORTUNITY_REFS: dict[str, query.Ref] = {
    ref.path: ref
    for ref in (
        _core_ref("title", "text", opportunity.c.title),
        _core_ref("organization_id", "text", opportunity.c.organization_id),
        _core_ref("primary_contact_id", "text", _PRIMARY_CONTACT),
        _core_ref("stage", "select", opportunity.c.stage),
        _core_ref("outcome", "select", _OPPORTUNITY_OUTCOME, (OPEN, WON, LOST), joins="stage"),
        _core_ref("value", "number", opportunity.c.value),
        _core_ref("currency", "text", opportunity.c.currency),
        _core_ref("probability", "number", cast(_EFFECTIVE_PROBABILITY, Numeric), joins="stage"),
        _core_ref("weighted_value", "number", cast(_WEIGHTED_VALUE, Numeric), joins="stage"),
        _core_ref("close_date", "date", cast(opportunity.c.close_date, SAText)),
        _core_ref("closed_at", "timestamp", opportunity.c.closed_at),
        _core_ref("source", "text", opportunity.c.source),
        _core_ref("dossier_updated_at", "timestamp", opportunity.c.dossier_updated_at),
        _core_ref("created_at", "timestamp", opportunity.c.created_at),
        _core_ref("updated_at", "timestamp", opportunity.c.updated_at),
        _count_ref(
            "interactions.count",
            _DEAL_INTERACTION_STATS.c.count,
            "deal_interactions",
            _per_row(
                func.count(),
                interaction,
                opportunity,
                interaction.c.opportunity_id == opportunity.c.id,
            ),
        ),
        replace(
            _core_ref(
                "interactions.last_at",
                "timestamp",
                _DEAL_INTERACTION_STATS.c.last_at,
                joins="deal_interactions",
            ),
            projected=_per_row(
                func.max(interaction.c.occurred_at),
                interaction,
                opportunity,
                interaction.c.opportunity_id == opportunity.c.id,
            ),
        ),
    )
}


# What each rollup counts, for `describe_fields`. `test_query.py` holds these equal
# to the rollup refs above.
_ROLLUP_HELP: dict[str, dict[str, str]] = {
    "contact": {
        "signals.active": "Active signals on the contact's organization.",
        "interactions.count": "Interactions logged with the contact.",
        "interactions.last_at": "When the contact's latest interaction happened.",
    },
    "organization": {
        "signals.active": "Active signals on the organization.",
        "contacts.count": "Contacts at the organization, archived ones excluded.",
    },
    "opportunity": {
        "interactions.count": "Interactions logged against the deal.",
        "interactions.last_at": "When the deal's latest interaction happened.",
    },
}


def rollups(entity: str) -> list[dict[str, str]]:
    """The computed paths a listing of ``entity`` can filter and sort on."""
    refs = {
        "contact": _CONTACT_REFS,
        "organization": _ORGANIZATION_ROLLUPS,
        "opportunity": _OPPORTUNITY_REFS,
    }[entity]
    return [
        {"path": path, "type": refs[path].kind, "description": description}
        for path, description in _ROLLUP_HELP[entity].items()
    ]


def _custom_refs(
    conn: AsyncConnection, defs: Mapping[tuple[str, str], RowMapping]
) -> dict[str, dict[str, query.Ref]]:
    """Each entity's live custom fields as refs. A retired field has no ref, so a
    path naming it reaches the resolver's retired refusal instead."""
    tables = {"contact": contact, "organization": organization, "opportunity": opportunity}
    refs: dict[str, dict[str, query.Ref]] = {entity: {} for entity in ENTITIES}
    for row in defs.values():
        if row["retired_at"] is not None:
            continue
        refs[row["entity"]][row["key"]] = query.custom_ref(
            row["key"],
            column=tables[row["entity"]].c.fields,
            key=row["key"],
            field_type=row["type"],
            options=row["options"],
            dialect=conn.dialect.name,
        )
    return refs


def _retired_paths(defs: Mapping[tuple[str, str], RowMapping], entity: str) -> dict[str, str]:
    """Each retired field a listing of ``entity`` can name, by its path there, with
    the sentence naming its replacement on that listing's rows."""
    retired: dict[str, str] = {}
    for (owner, key), row in defs.items():
        path = _field_path(owner, key, relative_to=entity)
        if row["retired_at"] is not None and path is not None:
            retired[path] = _replacement_hint(defs, owner, key, relative_to=entity)
    return retired


async def _contact_resolver(conn: AsyncConnection) -> query.Resolver:
    """A contact's own paths, plus its organization's under ``organization.``."""
    defs = await _all_field_defs(conn)
    custom = _custom_refs(conn, defs)
    refs = {**_CONTACT_REFS, **custom["contact"]}
    for path, ref in {**_ORGANIZATION_REFS, **custom["organization"]}.items():
        refs[f"organization.{path}"] = replace(
            ref, path=f"organization.{path}", joins="organization"
        )
    return query.Resolver("contact", refs, retired=_retired_paths(defs, "contact"))


async def _organization_resolver(conn: AsyncConnection) -> query.Resolver:
    defs = await _all_field_defs(conn)
    custom = _custom_refs(conn, defs)
    return query.Resolver(
        "organization",
        {**_ORGANIZATION_REFS, **_ORGANIZATION_ROLLUPS, **custom["organization"]},
        retired=_retired_paths(defs, "organization"),
    )


async def _opportunity_resolver(conn: AsyncConnection) -> query.Resolver:
    """A deal's own paths, its account's under ``organization.`` and its primary
    contact's under ``contact.`` (the contact's own fields, not its rollups)."""
    defs = await _all_field_defs(conn)
    custom = _custom_refs(conn, defs)
    refs = {**_OPPORTUNITY_REFS, **custom["opportunity"]}
    for path, ref in {**_ORGANIZATION_REFS, **custom["organization"]}.items():
        refs[f"organization.{path}"] = replace(
            ref, path=f"organization.{path}", joins="organization"
        )
    contact_core = {path: ref for path, ref in _CONTACT_REFS.items() if ref.joins is None}
    for path, ref in {**contact_core, **custom["contact"]}.items():
        refs[f"contact.{path}"] = replace(ref, path=f"contact.{path}", joins="contact")
    return query.Resolver("opportunity", refs, retired=_retired_paths(defs, "opportunity"))


_MAX_COUNT_VALUES = 100


async def count_by(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str,
    field: str,
    filter: Any = None,  # noqa: A002 - the wire name
    limit: int = 25,
) -> dict[str, Any]:
    """How the rows matching ``filter`` divide over one field's values.

    Returns ``{values: [{value, count}], total, truncated}``, most common first; a
    ``None`` value is the rows that hold none. For a ``multi_select`` or ``list``
    field each item counts once per row that holds it, so the counts can sum past
    ``total``. The same default exclusions as the listings apply (archived
    contacts, unpromoted organizations).
    """
    entity = normalize_entity(entity)
    limit = max(1, min(limit, _MAX_COUNT_VALUES))
    if entity == "contact":
        table, resolver, hidden = contact, await _contact_resolver(conn), _NOT_ARCHIVED
    elif entity == "organization":
        table, resolver = organization, await _organization_resolver(conn)
        hidden = and_(organization.c.status == "active", _ORG_NOT_ARCHIVED)
    else:
        await _ensure_pipeline(conn, ctx)
        table, resolver = opportunity, await _opportunity_resolver(conn)
        hidden = _OPPORTUNITY_OUTCOME == OPEN
    ref = resolver.ref(field)
    if ref.kind == "timestamp":
        raise FieldError(
            f"field {ref.path!r} is a timestamp, which has no values worth counting",
            next_step="Count by another field, or filter this one with gte / lte / between.",
        )
    predicate, asked, excluded, joins = query.where(filter, resolver)
    if entity == "contact":
        shown = False
    elif entity == "organization":
        shown = _unpromoted_asked(asked, excluded)
    else:
        shown = _closed_asked(asked, excluded)
    conditions = _scope(table, ctx)
    if not shown:
        conditions.append(hidden)
    elif entity == "organization":
        conditions.append(_ORG_NOT_ARCHIVED)
    if predicate is not None:
        conditions.append(predicate)
    joins = set(joins) | ({ref.joins} if ref.joins else set())
    if entity == "opportunity":
        joins.add("stage")  # the default's outcome reads the stage row
    source = _source(table, joins)
    if ref.scalar is None:
        # An array field: one row per item it holds.
        assert ref.elements is not None
        items = ref.elements(source, conditions).subquery()
        grouped = select(items.c.value, func.count().label("count")).group_by(items.c.value)
        value_column: Any = items.c.value
    elif ref.kind == "text":
        # A text filter matches regardless of case, so the count groups the
        # same way and shows one spelling, or a value's count would not match
        # the list its filter opens. The spelling shown is the first in byte
        # order (a capitalised one before its lowercase twin), the same on any
        # database collation.
        key = ref.key if ref.key is not None else func.lower(ref.scalar)
        grouped = (
            select(
                func.min(ref.scalar.collate(dialect.binary_collation(conn.dialect.name))).label(
                    "value"
                ),
                func.count().label("count"),
            )
            .select_from(source)
            .where(*conditions)
            .group_by(key)
        )
        value_column = key
    else:
        grouped = (
            select(ref.scalar.label("value"), func.count().label("count"))
            .select_from(source)
            .where(*conditions)
            .group_by(ref.scalar)
        )
        value_column = ref.scalar
    rows = await _all(
        conn,
        grouped.order_by(func.count().desc(), value_column.asc().nulls_last()).limit(limit + 1),
    )
    total = await _count(conn, source, and_(*conditions) if conditions else literal(True))
    _log_access(ctx, entity=entity, action="count")
    return {
        "values": [{"value": _plain(r["value"]), "count": r["count"]} for r in rows[:limit]],
        "total": total,
        "truncated": len(rows) > limit,
    }


def _status_asked(asked: Mapping[str, frozenset[Any]]) -> set[str]:
    """The status values a filter names under one clause kind, lowercased as text compares.

    Both names of a contact's stage count, since they are one expression.
    """
    return {
        value.lower()
        for path in ("status", "effective_status")
        for value in asked.get(path, ())
        if isinstance(value, str)
    }


def _unpromoted_asked(
    asked: Mapping[str, frozenset[Any]], excluded: Mapping[str, frozenset[Any]]
) -> bool:
    """Whether an organization filter asks for the unpromoted rows.

    It does by naming a status other than ``active`` (``eq`` / ``in``), or by ruling
    ``active`` out (``ne`` / ``not_in``): the default keeps one value, so a filter
    that excludes it would otherwise match nothing at all.
    """
    return not _status_asked(asked) <= {"active"} or "active" in _status_asked(excluded)


def _closed_asked(
    asked: Mapping[str, frozenset[Any]], excluded: Mapping[str, frozenset[Any]]
) -> bool:
    """Whether a deal filter speaks to open-ness at all.

    A listing shows open deals unless the filter names ``outcome`` or ``stage``
    in any clause: a closed deal is not a tombstone, so a caller who mentions
    either has chosen which columns to read, won and lost included.
    """
    return bool({"outcome", "stage"} & (set(asked) | set(excluded)))


def _plain(value: Any) -> Any:
    """A grouped value as JSON holds it: a ``numeric`` back to an int or a float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


async def define_field(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str,
    key: str,
    label: str,
    field_type: str,
    options: Sequence[str] | None = None,
    description: str | None = None,
    defined_by: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[FieldDef, str, list[str]]:
    """Define a custom field, or return the one already defined under this key.

    Idempotent on ``(entity, key)`` via ``uq_field_def_key``. Returns
    ``(FieldDef, action, options_added)``.

    A match is additive only: it appends options the definition does not yet hold
    and fills an empty description. It never changes the label, never removes an
    option, and refuses a different ``type``, because rows already hold values
    validated against the definition as it stands.
    """
    entity = normalize_entity(entity)
    key = normalize_key(entity, key)
    field_type = normalize_type(field_type)
    label = clean_label(label)
    description = clean_description(description)
    wanted = clean_options(field_type, options)
    low, high = clean_bounds(field_type, minimum, maximum)
    _check_len("defined_by", defined_by, _CAP_SHORT)

    defs = await _field_defs(conn, entity)
    if key not in defs and _live_count(defs) >= MAX_FIELDS_PER_ENTITY:
        raise ValueError(
            f"this workspace already defines {MAX_FIELDS_PER_ENTITY} live {entity} fields, "
            "the maximum"
        )
    now = _now()
    row, created = await _upsert_returning(
        conn,
        table=field_def,
        insert_values={
            "id": _new_field_def_id(),
            **ctx.identity_columns(),
            "entity": entity,
            "key": key,
            "label": label,
            "type": field_type,
            "options": wanted,
            "description": description,
            "minimum": low,
            "maximum": high,
            "defined_by": _clean(defined_by),
            "created_at": now,
            "updated_at": now,
        },
        conflict_index_elements=[
            field_def.c.tenant_id,
            field_def.c.workspace_id,
            field_def.c.entity,
            field_def.c.key,
        ],
        conflict_where=None,
        reselect_predicate=and_(
            field_def.c.tenant_id == ctx.tenant_id,
            field_def.c.workspace_id == ctx.workspace_id,
            field_def.c.entity == entity,
            field_def.c.key == key,
        ),
    )
    options_added: list[str] = []
    if not created and row["retired_at"] is not None:
        # A retired key stays taken, so it cannot come back with another type over
        # the values it left on the rows, and defining it never restores it.
        hint = _replacement_hint(await _all_field_defs(conn), entity, key, relative_to=entity)
        raise FieldError(
            f"{entity} field {key!r} is retired",
            next_step=(
                f"{hint} Restore it with update_field(entity={entity!r}, "
                f"key={key!r}, retired=false)."
            ),
        )
    if not created:
        # Lock and read the definition again, so options merge onto the list as it stands
        # after any concurrent option change rather than overwrite it.
        locked = await _one(
            conn, select(field_def).where(field_def.c.id == row["id"]).with_for_update()
        )
        row = locked if locked is not None else row
        if row["type"] != field_type:
            raise ValueError(
                f"{entity} field {key!r} is already defined as {row['type']}; a field's type "
                "cannot change"
            )
        merged = merge_options(row["options"], wanted)
        options_added = merged[len(row["options"]) :]
        updates: dict[str, Any] = {}
        if options_added:
            updates["options"] = merged
        if description is not None and not row["description"]:
            updates["description"] = description
        if updates:
            await conn.execute(
                field_def.update()
                .where(field_def.c.id == row["id"])
                .values(**updates, updated_at=now)
            )
            fresh = await _one(conn, select(field_def).where(field_def.c.id == row["id"]))
            row = fresh if fresh is not None else row
    await write_audit(
        conn,
        ctx,
        entity="field_def",
        action="define",
        outcome="ok",
        entity_id=row["id"],
        detail={"entity": entity, "key": key, "options_added": len(options_added)},
    )
    return FieldDef.from_row(row), "created" if created else "matched", options_added


def _parse_replaced_by(entity: str, path: str) -> tuple[str, str]:
    """``replaced_by`` as ``(entity, key)``: a bare key on ``entity``, or a key
    under ``organization.`` / ``contact.``."""
    clean = path.strip() if isinstance(path, str) else ""
    prefix, dot, rest = clean.partition(".")
    target, key = (prefix, rest) if dot else (entity, clean)
    if target not in ("organization", "contact", entity):
        target = ""
    try:
        if target:
            return target, normalize_key(target, key)
    except ValueError:
        pass
    raise FieldError(
        f"replaced_by {path!r} names no custom field",
        next_step=(
            "Name the replacement by its key on the same entity, or as "
            "organization.<key> / contact.<key>."
        ),
    )


async def _bounds_for(
    conn: AsyncConnection,
    ctx: Principal,
    row: RowMapping,
    entity: str,
    key: str,
    minimum: float | None,
    maximum: float | None,
    clear: bool,
) -> tuple[float | None, float | None]:
    """The range ``row`` takes after this change; refused while a record holds a value outside it.

    ``row`` is the definition as locked by ``update_field``, so a value written to the field
    waits for this change and is then checked against the new range.
    """
    if row["type"] != "number":
        raise FieldError(
            f"{entity} field {key!r} is a {row['type']} field and takes no minimum or maximum",
            next_step="Only a number field has a range.",
        )
    low = None if clear else row["minimum"]
    high = None if clear else row["maximum"]
    low, high = clean_bounds(
        "number", minimum if minimum is not None else low, maximum if maximum is not None else high
    )
    if low is None and high is None:
        return low, high
    table = {"contact": contact, "organization": organization, "opportunity": opportunity}[entity]
    name = conn.dialect.name
    value = dialect.field_number(name, table.c.fields, key, Float())
    outside = []
    if low is not None:
        outside.append(value < low)
    if high is not None:
        outside.append(value > high)
    count = (
        await conn.execute(
            select(func.count())
            .select_from(table)
            .where(
                table.c.workspace_id == ctx.workspace_id,
                dialect.field_has_key(name, table.c.fields, key),
                or_(*outside),
            )
        )
    ).scalar_one()
    if count:
        raise FieldError(
            f"{count} {entity} record(s) hold a {key!r} value outside {low}..{high}",
            next_step=(
                f"Change those values first: list_{entity}s with a filter on {key!r} shows them."
            ),
        )
    return low, high


async def _options_without(
    conn: AsyncConnection,
    ctx: Principal,
    row: RowMapping,
    entity: str,
    key: str,
    remove: Sequence[str],
) -> list[str]:
    """``row``'s options less ``remove``, matched case-insensitively; refused while any record holds one.

    ``row`` is the definition as locked by ``update_field``, so a value written to the field
    waits for this change and is then checked against the options that remain, and a
    concurrent option change is seen rather than overwritten.
    """
    if row["type"] not in ("select", "multi_select"):
        raise FieldError(
            f"{entity} field {key!r} is a {row['type']} field and has no options",
            next_step="Only a select or multi_select field has options to remove.",
        )
    options = list(row["options"])
    by_fold = {o.casefold(): o for o in options}
    unknown = [o for o in remove if not isinstance(o, str) or o.strip().casefold() not in by_fold]
    if unknown:
        raise FieldError(
            f"{entity} field {key!r} has no option {unknown[0]!r}",
            next_step=f"Its options are {options}.",
        )
    dropped = {by_fold[o.strip().casefold()] for o in remove}
    remaining = [o for o in options if o not in dropped]
    if not remaining:
        raise FieldError(
            f"removing {sorted(dropped)} would leave {entity} field {key!r} with no options",
            next_step="Keep at least one option, or retire the field with update_field(retired=true).",
        )
    table = {"contact": contact, "organization": organization, "opportunity": opportunity}[entity]
    name = conn.dialect.name
    held = (
        dialect.field_text(name, table.c.fields, key).in_(sorted(dropped))
        if row["type"] == "select"
        else dialect.field_has_any(name, table.c.fields, key, sorted(dropped))
    )
    count = (
        await conn.execute(
            select(func.count())
            .select_from(table)
            .where(table.c.workspace_id == ctx.workspace_id, held)
        )
    ).scalar_one()
    if count:
        raise FieldError(
            f"{count} {entity} record(s) still hold an option being removed from {key!r}",
            next_step=(
                f"Change those values first: list_{entity}s or count_records(entity={entity!r}, "
                f"field={key!r}) shows which hold it."
            ),
        )
    return remaining


async def update_field(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str,
    key: str,
    retired: bool | None = None,
    replaced_by: str | None = None,
    default_column: bool | None = None,
    label: str | None = None,
    description: str | None = None,
    remove_options: Sequence[str] | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    clear_bounds: bool = False,
) -> tuple[FieldDef, str]:
    """Retire, restore or edit a custom field's definition.

    Returns ``(FieldDef, action)``, ``action`` being ``retired``, ``restored``, ``updated``
    (only ``default_column``, ``label``, ``description`` or options changed) or ``unchanged``.
    ``minimum`` / ``maximum`` set a number field's range and ``clear_bounds`` lifts it before
    they apply; a range some record falls outside is refused. ``description=""`` clears the description. ``remove_options`` drops options no record
    holds; an option a record holds, or removing every option, is refused. ``retired=True`` stamps ``retired_at`` once: retiring again keeps
    the original stamp and changes the replacement only when one is given.
    ``retired=False`` clears both. ``replaced_by`` must name a live field other
    than this one, and only a retired field has one.

    Retiring never touches the rows: their values stay, readable by id, and the
    key stays taken.
    """
    entity = normalize_entity(entity)
    key = normalize_key(entity, key)
    defs = await _all_field_defs(conn)
    row = defs.get((entity, key))
    if row is None:
        raise FieldError(
            f"no {entity} field named {key!r} is defined in this workspace",
            next_step=(
                f"Read list_fields(entity={entity!r}, include_retired=true) for the defined keys."
            ),
        )
    # Lock and read the definition again before any decision, so every comparison below
    # and the returned field see the definition as it stands after a concurrent change.
    locked = await _one(
        conn, select(field_def).where(field_def.c.id == row["id"]).with_for_update()
    )
    row = locked if locked is not None else row
    stays_retired = retired is True or (retired is None and row["retired_at"] is not None)
    if default_column is True and stays_retired:
        raise FieldError(
            f"{entity} field {key!r} is retired, so it cannot open as a column",
            next_step=(
                f"Restore it first with update_field(entity={entity!r}, key={key!r}, "
                "retired=false), or set default_column on the field that replaced it."
            ),
        )
    target: tuple[str, str] | None = None
    if replaced_by is not None:
        if not stays_retired:
            raise FieldError(
                "replaced_by applies only to a retired field",
                next_step=(
                    f"Retire it and name the replacement in one call: update_field("
                    f"entity={entity!r}, key={key!r}, retired=true, replaced_by=...)."
                ),
            )
        target = _parse_replaced_by(entity, replaced_by)
        if target == (entity, key):
            raise FieldError(
                f"{entity} field {key!r} cannot replace itself",
                next_step="Name the live field that takes its place, or leave replaced_by out.",
            )
        found = defs.get(target)
        if found is None or found["retired_at"] is not None:
            state = "is not defined" if found is None else "is retired"
            raise FieldError(
                f"replacement {target[0]} field {target[1]!r} {state}",
                next_step=(
                    f"A replacement must be a live field. Read list_fields("
                    f"entity={target[0]!r}) for the live keys."
                ),
            )

    now = _now()
    updates: dict[str, Any] = {}
    if retired is False:
        if row["retired_at"] is not None:
            own = {k: r for (e, k), r in defs.items() if e == entity}
            if _live_count(own) >= MAX_FIELDS_PER_ENTITY:
                raise ValueError(
                    f"this workspace already defines {MAX_FIELDS_PER_ENTITY} live {entity} "
                    "fields, the maximum; retire one before restoring this one"
                )
            updates = {"retired_at": None, "replaced_by_entity": None, "replaced_by_key": None}
    else:
        if retired is True and row["retired_at"] is None:
            updates["retired_at"] = now
        if target is not None and (row["replaced_by_entity"], row["replaced_by_key"]) != target:
            updates["replaced_by_entity"], updates["replaced_by_key"] = target
    if default_column is not None and bool(row["default_column"]) != default_column:
        updates["default_column"] = default_column
    if label is not None:
        cleaned = clean_label(label)
        if cleaned != row["label"]:
            updates["label"] = cleaned
    if description is not None:
        described = clean_description(description)
        if described != row["description"]:
            updates["description"] = described
    if clear_bounds or minimum is not None or maximum is not None:
        bounds = await _bounds_for(conn, ctx, row, entity, key, minimum, maximum, clear_bounds)
        if bounds != (row["minimum"], row["maximum"]):
            updates["minimum"], updates["maximum"] = bounds
    if remove_options:
        remaining = await _options_without(conn, ctx, row, entity, key, remove_options)
        if remaining != list(row["options"]):
            updates["options"] = remaining

    if updates:
        stamp = field_def.update().where(field_def.c.id == row["id"])
        if "retired_at" in updates and updates["retired_at"] is not None:
            # Two retires at once: the first stamp stands, the second writes nothing.
            stamp = stamp.where(field_def.c.retired_at.is_(None))
        await conn.execute(stamp.values(**updates, updated_at=now))
        fresh = await _one(conn, select(field_def).where(field_def.c.id == row["id"]))
        row = fresh if fresh is not None else row
        if set(updates) <= {
            "default_column",
            "label",
            "description",
            "options",
            "minimum",
            "maximum",
        }:
            action = "updated"
        else:
            action = "restored" if retired is False else "retired"
    else:
        action = "unchanged"
    await write_audit(
        conn,
        ctx,
        entity="field_def",
        action="update",
        outcome="ok",
        entity_id=row["id"],
        detail={
            "entity": entity,
            "key": key,
            "retired": row["retired_at"] is not None,
            "replaced_by": FieldDef.from_row(row).replaced_by,
            "default_column": bool(row["default_column"]),
            "options_removed": len(remove_options or []) if "options" in updates else 0,
            "minimum": row["minimum"],
            "maximum": row["maximum"],
            "changed": action != "unchanged",
        },
    )
    return FieldDef.from_row(row), action


async def describe_fields(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    entity: str | None = None,
    include_retired: bool = False,
) -> dict[str, list[FieldDef]]:
    """The custom fields defined in this workspace, by entity, in definition order.

    Live fields only, unless ``include_retired``: a retired field is not one to
    write, filter or show, so the default read is what every picker builds from.
    """
    entities = [normalize_entity(entity)] if entity is not None else list(ENTITIES)
    statement = select(field_def).where(field_def.c.entity.in_(entities))
    if not include_retired:
        statement = statement.where(field_def.c.retired_at.is_(None))
    rows = await _all(conn, statement.order_by(field_def.c.created_at, field_def.c.id))
    _log_access(ctx, entity="field_def", action="list")
    return {e: [FieldDef.from_row(r) for r in rows if r["entity"] == e] for e in entities}


# --------------------------------------------------------------------------- #
# Opportunities                                                                #
# --------------------------------------------------------------------------- #
#
# Opportunity is a first-class entity in the graph (peer to Contact/Interaction).
# Its `outcome` (open/won/lost) is a read-time projection of `stage`, never a
# stored column — the same discipline as contact.effective_status.
# `_OPPORTUNITY_OUTCOME` is BOTH the `outcome` column `_opportunity_select` returns
# AND the expression `list_opportunities`/`pipeline_summary` filter on, so the
# returned field and the filter can't diverge.

_CAP_CURRENCY = 16  # a currency code / short label — open string, not an enum

# --- The pipeline ------------------------------------------
#
# A deal's stage is a key into `pipeline_stage`, so the outcome, the default
# probability and the label come from one row per workspace. Every opportunity
# read joins that row. A stage no row holds reads `open` and carries no default
# probability.


def _new_pipeline_stage_id() -> str:
    from mcp_people.ids import new_pipeline_stage_id

    return new_pipeline_stage_id()


def _opportunity_select() -> Select:
    """An opportunity ``SELECT`` carrying the derived columns.

    Every opportunity-returning read (and write re-select via
    :func:`_opportunity_row`) goes through this so the wire model's ``outcome``,
    ``probability`` and ``weighted_value`` are populated uniformly.
    """
    return select(*_OPPORTUNITY_COLUMNS).select_from(_STAGE_JOIN)


# What an opportunity row carries on the wire: the table plus the stage-derived columns.
_OPPORTUNITY_COLUMNS: tuple[Any, ...] = (
    *(c for c in opportunity.c if c.name != "primary_contact_id"),
    _PRIMARY_CONTACT.label("primary_contact_id"),
    _OPPORTUNITY_OUTCOME.label("outcome"),
    _EFFECTIVE_PROBABILITY.label("effective_probability"),
    _WEIGHTED_VALUE.label("weighted_value"),
)


async def _opportunity_row(conn: AsyncConnection, opportunity_id: str) -> RowMapping:
    row = await _one(
        conn, _opportunity_select().where(opportunity.c.id == opportunity_id, _live(opportunity))
    )
    if row is None:
        raise OpportunityNotFoundError(opportunity_id)
    return row


async def _stage_rows(conn: AsyncConnection, ctx: Principal) -> list[RowMapping]:
    """The active workspace's stages in order. Scoped here, not by RLS alone: the
    read policy admits every workspace the caller may read, and a pipeline is one
    workspace's."""
    return list(
        await _all(
            conn,
            select(pipeline_stage)
            .where(pipeline_stage.c.workspace_id == ctx.workspace_id)
            .order_by(pipeline_stage.c.position, pipeline_stage.c.key),
        )
    )


def _live_rows(rows: Sequence[RowMapping]) -> list[RowMapping]:
    """The rows of a vocabulary (stages or signal types) no one has retired."""
    return [row for row in rows if row["retired_at"] is None]


def _replacement(rows: Sequence[RowMapping], key: str) -> str | None:
    """The live key a retired ``key`` is replaced by, following the ``replaced_by`` chain.

    Each step is validated live when it is set, but a step can retire later, so the chain
    is followed rather than read one hop deep. A key already visited ends it, so a cycle
    cannot loop. ``None`` when the chain reaches no live key.
    """
    by_key = {row["key"]: row for row in rows}
    seen: set[str] = set()
    at = key
    while True:
        seen.add(at)
        row = by_key.get(at)
        if row is None or row["replaced_by_key"] is None:
            return None
        nxt = row["replaced_by_key"]
        target = by_key.get(nxt)
        if target is None or nxt in seen:
            return None
        if target["retired_at"] is None:
            return nxt
        at = nxt


def _retired_next_step(rows: Sequence[RowMapping], key: str, *, kind: str, tool: str) -> str:
    """What to use instead of a retired ``key``, and how to bring it back."""
    replacement = _replacement(rows, key)
    if replacement is not None:
        use = f"Use {replacement!r}, which replaces it."
    else:
        live = [row["key"] for row in _live_rows(rows)]
        use = f"Use one of: {', '.join(live)}." if live else f"No live {kind} is defined."
    return f"{use} Restore it with {tool}(key={key!r}, retired=false)."


async def _ensure_pipeline(conn: AsyncConnection, ctx: Principal) -> list[RowMapping]:
    """The workspace's stages, seeding the default set when it has none.

    A workspace starts with the vocabulary its deals already use, so a deal
    created before anyone defined a pipeline has a column. Every pipeline-aware
    read and write calls this, because a migration cannot: it runs as the app
    role, which the FORCE'd policies bind to no workspace. The seed is idempotent
    under the unique key: two first reads converge on one set.
    """
    rows = await _stage_rows(conn, ctx)
    if rows:
        return rows
    now = _now()
    for position, (key, label, probability, outcome_) in enumerate(DEFAULT_STAGES):
        await conn.execute(
            insert_for(conn.dialect.name, pipeline_stage)
            .values(
                id=_new_pipeline_stage_id(),
                **ctx.identity_columns(),
                key=key,
                label=label,
                position=position,
                probability=probability,
                outcome=outcome_,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    pipeline_stage.c.tenant_id,
                    pipeline_stage.c.workspace_id,
                    pipeline_stage.c.key,
                ]
            )
        )
    return await _stage_rows(conn, ctx)


async def _stage_for(conn: AsyncConnection, ctx: Principal, stage: str | None) -> RowMapping:
    """The stage a deal may enter, or a refusal naming the ones it could.

    ``None`` is the pipeline's first live open stage. The stage returned is read under
    a shared lock held to commit: a retirement (``update_pipeline_stage``, which locks
    the row exclusively) either waits for this write and then counts its deal, or
    commits first and this read sees the stage retired. Only that one row is locked,
    since a retirement renumbers the other stages.
    """
    stages = await _ensure_pipeline(conn, ctx)
    while True:
        if stage is None:
            live = _live_rows(stages)
            found = next((row for row in live if row["outcome"] == OPEN), live[0])
            key = found["key"]
        else:
            key = stage.strip().lower()
            found = next((row for row in stages if row["key"] == key), None)
        if found is None or found["retired_at"] is not None:
            break
        locked = await _one(
            conn,
            select(pipeline_stage)
            .where(pipeline_stage.c.id == found["id"])
            .with_for_update(read=True),
        )
        if locked is not None and locked["retired_at"] is None:
            return locked
        # Retired since the read above: read the pipeline again, which now shows it.
        stages = await _stage_rows(conn, ctx)
    for row in stages:
        if row["key"] == key:
            if row["retired_at"] is not None:
                raise FieldError(
                    f"stage {key!r} is retired, so no deal enters it",
                    next_step=_retired_next_step(
                        stages, key, kind="stage", tool="update_pipeline_stage"
                    ),
                )
            return row
    raise FieldError(
        f"stage {stage!r} is not in this workspace's pipeline",
        next_step=(
            "Use one of: " + ", ".join(row["key"] for row in _live_rows(stages)) + ". "
            "get_pipeline lists them with their labels; define_pipeline_stage adds one."
        ),
    )


async def get_pipeline(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    include_retired: bool = False,
) -> dict[str, Any]:
    """The pipeline: its stages in order, each with its deals, and the totals.

    Each stage carries its definition and ``deals``, the live deals it holds, with
    ``value_sum`` and ``weighted_sum`` when any of them has a value. A deal whose stage
    the pipeline does not name still counts, listed after the stages under its key.
    A retired stage is listed only with ``include_retired``, after the live ones; the
    totals count its deals either way. ``organization_id`` counts one account's deals
    only, and is that account's not-found when out of scope. The totals are
    :func:`pipeline_summary`'s.
    """
    if organization_id is not None:
        await _org_row(conn, organization_id)
    stages = await _ensure_pipeline(conn, ctx)
    summary = await pipeline_summary(conn, ctx, organization_id=organization_id)
    by_stage: dict[str, dict[str, Any]] = summary.pop("by_stage")
    sums = ("value_sum", "weighted_sum")
    listed: list[dict[str, Any]] = []
    for row in stages:
        entry = by_stage.pop(row["key"])
        if row["retired_at"] is not None and not include_retired:
            continue
        listed.append(
            {
                **dump(PipelineStage.from_row(row)),
                "deals": entry["count"],
                **{k: entry[k] for k in sums if k in entry},
            }
        )
    for key, entry in by_stage.items():
        listed.append(
            {
                "key": key,
                "label": key,
                "outcome": entry["outcome"],
                "deals": entry["count"],
                **{k: entry[k] for k in sums if k in entry},
            }
        )
    return {"stages": listed, **summary}


async def define_pipeline_stage(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    key: str,
    label: str,
    position: int | None = None,
    probability: int | None = None,
    outcome: str | None = None,
) -> tuple[PipelineStage, str]:
    """Add a stage under ``key``. Returns ``(stage, action)``.

    The stage lands last unless a position is given, with probability 0 and outcome
    ``open`` unless said otherwise. A key the pipeline already holds is returned as it
    is (``matched``): defining never changes a stage, :func:`update_pipeline_stage` does.
    A retired key stays taken: defining it is refused, and restoring it is
    ``update_pipeline_stage(retired=False)``.
    """
    clean_key = normalize_stage_key(key)
    _check_position(position)
    stages = await _ensure_pipeline(conn, ctx)
    existing = next((row for row in stages if row["key"] == clean_key), None)
    if existing is not None and existing["retired_at"] is not None:
        raise FieldError(
            f"stage {clean_key!r} is retired, and its key stays taken",
            next_step=_retired_next_step(
                stages, clean_key, kind="stage", tool="update_pipeline_stage"
            ),
        )
    if existing is not None:
        return PipelineStage.from_row(existing), "matched"
    if len(_live_rows(stages)) >= MAX_STAGES:
        raise ValueError(f"a pipeline holds at most {MAX_STAGES} stages")
    now = _now()
    values = {
        "id": _new_pipeline_stage_id(),
        **ctx.identity_columns(),
        "key": clean_key,
        "label": clean_stage_label(label),
        "position": len(stages),
        "probability": normalize_probability(probability, default=0),
        "outcome": normalize_outcome(outcome),
        "created_at": now,
        "updated_at": now,
    }
    row, created = await _upsert_returning(
        conn,
        table=pipeline_stage,
        insert_values=values,
        conflict_index_elements=[
            pipeline_stage.c.tenant_id,
            pipeline_stage.c.workspace_id,
            pipeline_stage.c.key,
        ],
        conflict_where=None,
        reselect_predicate=and_(
            pipeline_stage.c.workspace_id == ctx.workspace_id,
            pipeline_stage.c.key == clean_key,
        ),
    )
    if created:
        # Last among the live stages unless a position is given: retired ones sort after.
        await _place_stage(conn, ctx, row["id"], position)
        row = await _one(conn, select(pipeline_stage).where(pipeline_stage.c.id == row["id"]))
        assert row is not None
    if created:
        await write_audit(
            conn, ctx, entity="pipeline_stage", action="define", outcome="ok", entity_id=row["id"]
        )
    return PipelineStage.from_row(row), "created" if created else "matched"


async def update_pipeline_stage(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    key: str,
    label: str | None = None,
    position: int | None = None,
    probability: int | None = None,
    outcome: str | None = None,
    retired: bool | None = None,
    replaced_by: str | None = None,
) -> tuple[PipelineStage, str]:
    """Retire, restore or edit the stage under ``key``: only the arguments given.

    Returns ``(stage, action)``, ``action`` being ``retired``, ``restored``, ``updated``
    (label, position, probability or outcome changed) or ``unchanged``.

    A deal in the stage keeps its key and takes the new default probability unless it
    has its own; a stage that starts or stops closing deals moves their ``closed_at``
    with it. The pipeline keeps a live open stage, since a new deal defaults to the
    first one: the last cannot close deals or retire.

    ``retired=True`` stamps ``retired_at`` once and moves the stage after the live
    ones; retiring again keeps the stamp and changes the replacement only when one is
    given. It is refused while an open stage holds live deals, since its board column
    would vanish under them; a won or lost stage retires with its deals. A retired
    stage keeps its row, so its deals still read its label and outcome, no deal enters
    it, and its key stays taken. ``retired=False`` clears both and puts it last among
    the live stages, or at ``position``. ``replaced_by`` names a live stage other than
    this one, and only a retired stage has one.
    """
    clean_key = normalize_stage_key(key)
    _check_position(position)
    stages = await _ensure_pipeline(conn, ctx)
    existing = next((row for row in stages if row["key"] == clean_key), None)
    if existing is None:
        raise FieldError(
            f"stage {key!r} is not in this workspace's pipeline",
            next_step=(
                "Use one of: " + ", ".join(row["key"] for row in stages) + "; add a new "
                "stage with define_pipeline_stage."
            ),
        )
    # Lock and read the stage again before any decision, so every check below sees it as
    # it stands after a concurrent change.
    locked = await _one(
        conn, select(pipeline_stage).where(pipeline_stage.c.id == existing["id"]).with_for_update()
    )
    existing = locked if locked is not None else existing
    was_retired = existing["retired_at"] is not None
    stays_retired = retired is True or (retired is None and was_retired)
    if position is not None and stays_retired:
        raise FieldError(
            f"stage {clean_key!r} is retired, so it has no place on the board",
            next_step=(
                f"Restore it at a position with update_pipeline_stage(key={clean_key!r}, "
                "retired=false, position=...)."
            ),
        )
    target: str | None = None
    if replaced_by is not None:
        if not stays_retired:
            raise FieldError(
                "replaced_by applies only to a retired stage",
                next_step=(
                    f"Retire it and name the replacement in one call: update_pipeline_stage("
                    f"key={clean_key!r}, retired=true, replaced_by=...)."
                ),
            )
        target = normalize_stage_key(replaced_by)
        if target == clean_key:
            raise FieldError(
                f"stage {clean_key!r} cannot replace itself",
                next_step="Name the live stage that takes its place, or leave replaced_by out.",
            )
        found = next((row for row in stages if row["key"] == target), None)
        if found is None or found["retired_at"] is not None:
            state = "is not in this workspace's pipeline" if found is None else "is retired"
            raise FieldError(
                f"replacement stage {target!r} {state}",
                next_step=(
                    "A replacement must be a live stage: one of "
                    + ", ".join(row["key"] for row in _live_rows(stages))
                    + "."
                ),
            )

    final_outcome = existing["outcome"] if outcome is None else normalize_outcome(outcome)
    others_open = [
        row for row in _live_rows(stages) if row["outcome"] == OPEN and row["key"] != clean_key
    ]
    if (
        not was_retired
        and existing["outcome"] == OPEN
        and (stays_retired or final_outcome != OPEN)
        and not others_open
    ):
        raise FieldError(
            f"stage {clean_key!r} is the pipeline's only open stage",
            next_step=(
                "Add another open stage with define_pipeline_stage first, since a new "
                "deal starts in an open stage."
            ),
        )
    if stays_retired and final_outcome == OPEN:
        held = (
            await conn.execute(
                select(func.count())
                .select_from(opportunity)
                .where(
                    opportunity.c.workspace_id == ctx.workspace_id,
                    opportunity.c.stage == clean_key,
                    _live(opportunity),
                )
            )
        ).scalar_one()
        if held:
            raise FieldError(
                f"stage {clean_key!r} holds {held} open deal{'s' if held != 1 else ''}, "
                "which a retired stage cannot",
                next_step=(
                    f"Move them out with move_opportunity (list_opportunities(stage="
                    f"{clean_key!r}) lists them), then retire it. A won or lost stage "
                    "retires with its deals."
                ),
            )
    if retired is False and was_retired and len(_live_rows(stages)) >= MAX_STAGES:
        raise ValueError(
            f"a pipeline holds at most {MAX_STAGES} live stages; retire one before "
            "restoring this one"
        )

    now = _now()
    updates: dict[str, Any] = {}
    if label is not None and clean_stage_label(label) != existing["label"]:
        updates["label"] = clean_stage_label(label)
    if probability is not None and normalize_probability(probability) != existing["probability"]:
        updates["probability"] = normalize_probability(probability)
    if final_outcome != existing["outcome"]:
        updates["outcome"] = final_outcome
    if retired is False:
        if was_retired:
            updates["retired_at"] = None
            updates["replaced_by_key"] = None
    else:
        if retired is True and not was_retired:
            updates["retired_at"] = now
        if target is not None and target != existing["replaced_by_key"]:
            updates["replaced_by_key"] = target
    retirement = {"retired_at", "replaced_by_key"} & set(updates)
    replace_it = "retired_at" in updates or (
        position is not None and position != existing["position"]
    )
    if updates:
        stamp = pipeline_stage.update().where(pipeline_stage.c.id == existing["id"])
        await conn.execute(stamp.values(**updates, updated_at=now))
        if "outcome" in updates:
            # A stage that starts or stops closing deals moves their closed_at with it.
            closes = updates["outcome"] != OPEN
            await conn.execute(
                opportunity.update()
                .where(
                    opportunity.c.workspace_id == ctx.workspace_id,
                    opportunity.c.stage == clean_key,
                )
                .values(closed_at=func.coalesce(opportunity.c.closed_at, now) if closes else None)
            )
    if replace_it:
        await _place_stage(conn, ctx, existing["id"], position)
        if not updates:
            await conn.execute(
                pipeline_stage.update()
                .where(pipeline_stage.c.id == existing["id"])
                .values(updated_at=now)
            )
    changed = bool(updates) or replace_it
    if retirement:
        action = "restored" if retired is False else "retired"
    else:
        action = "updated" if changed else "unchanged"
    row = await _one(conn, select(pipeline_stage).where(pipeline_stage.c.id == existing["id"]))
    assert row is not None
    if changed:
        await write_audit(
            conn,
            ctx,
            entity="pipeline_stage",
            action="update",
            outcome="ok",
            entity_id=existing["id"],
            detail={
                "key": clean_key,
                "retired": row["retired_at"] is not None,
                "replaced_by": row["replaced_by_key"],
            },
        )
    return PipelineStage.from_row(row), action


def _check_position(position: int | None) -> None:
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or position < 0
    ):
        raise ValueError("position must be a whole number, 0 first")


async def _place_stage(
    conn: AsyncConnection, ctx: Principal, stage_id: str, position: int | None
) -> None:
    """Put one stage at ``position`` and renumber the rest, so positions stay 0..n-1
    with no two stages sharing one: the board's column order is the list order.

    The live stages come first and the retired ones after them, so a live stage's
    position is its column on the board. A live stage goes to ``position`` among the
    live ones, or last among them when ``position`` is None; a retired one goes last.
    """
    rows = [row for row in await _stage_rows(conn, ctx) if row["id"] != stage_id]
    moved = await _one(conn, select(pipeline_stage).where(pipeline_stage.c.id == stage_id))
    assert moved is not None
    live = _live_rows(rows)
    retired_rows = [row for row in rows if row["retired_at"] is not None]
    if moved["retired_at"] is None:
        live.insert(len(live) if position is None else min(position, len(live)), moved)
    else:
        retired_rows.append(moved)
    rows = live + retired_rows
    for index, row in enumerate(rows):
        if row["position"] != index:
            await conn.execute(
                pipeline_stage.update()
                .where(pipeline_stage.c.id == row["id"])
                .values(position=index)
            )


def _to_amount(value: float | None) -> Decimal | None:
    """Coerce a wire float to a 2-dp Decimal for the Numeric column (via str, so a
    binary-float artifact never reaches the DB)."""
    return None if value is None else Decimal(str(value))


def _check_opportunity_numbers(value: float | None, probability: int | None) -> None:
    """Reject out-of-range numerics at the write seam (→ ``invalid_request``).

    Unlike the open-string ``source``, these are BOUNDED quantities, not an
    evolving vocabulary: ``probability`` is a 0-100 percentage and ``value`` is a
    non-negative deal size. Enforced app-side (no DB CHECK, consistent with the open
    Text columns) so a nonsense figure fails cleanly at the boundary — the documented
    contract instead of a silently-stored bad row. Validated at INPUT, not on the
    output wire model (an output constraint would turn a bad row into a read-500).
    """
    if probability is not None and not (0 <= probability <= 100):
        raise ValueError("probability must be between 0 and 100")
    if value is not None and value < 0:
        raise ValueError("value must not be negative")


async def find_or_create_opportunity(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    title: str,
    organization_id: str | None = None,
    primary_contact_id: str | None = None,
    stage: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    probability: int | None = None,
    close_date: date | None = None,
    source: str | None = None,
    idempotency_key: str | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
) -> tuple[Opportunity, str]:
    """Create an opportunity, or return the existing one for a repeated ``idempotency_key``.

    Unlike contacts/orgs, an opportunity has no natural dedupe key (many live deals
    can share a title on one account), so "find" is by the optional
    ``idempotency_key`` ONLY: a retried create — or the CRM data migration replaying
    a legacy deal id — with the same key converges to one row via the partial unique
    index ``uq_opportunity_idem`` (ON CONFLICT DO NOTHING + re-select). Without a key
    every call creates a new opportunity. Returns ``(Opportunity, action)`` with
    ``action`` ∈ {created, matched}.

    Edges: an explicit ``organization_id`` / ``primary_contact_id`` is validated
    in-scope (404 otherwise). When ``organization_id`` is omitted but a
    ``primary_contact_id`` is given, the account is FROZEN from that contact's
    organization at create — the same immutable-id edge as :func:`log_interaction`
    (a later change of the contact's employer does not move this opportunity's
    account). ``stage`` defaults to the pipeline's first open stage and must be one
    the pipeline holds; entering a closing stage stamps ``closed_at``. ``fields``
    fills custom fields; on a match the row's own values stay.
    """
    _check_len("title", title, _CAP_NAME)
    _check_len("stage", stage, _CAP_SHORT)
    _check_len("currency", currency, _CAP_CURRENCY)
    _check_len("source", source, _CAP_SHORT)
    _check_len("idempotency_key", idempotency_key, _CAP_SHORT)
    _check_opportunity_numbers(value, probability)
    title = title.strip()
    if not title:
        raise ValueError("title must not be empty")

    frozen_org_id = organization_id
    if organization_id is not None:
        await _org_row(conn, organization_id)  # validate the account in-scope
    if primary_contact_id is not None:
        contact_row = await _contact_row(conn, primary_contact_id)  # 404 if out of scope
        if frozen_org_id is None:
            frozen_org_id = contact_row["organization_id"]  # freeze the account from the contact

    stage_row = await _stage_for(conn, ctx, _clean(stage))
    patch = await _field_fill_patch(conn, entity="opportunity", submitted=fields, by=fields_by)

    now = _now()
    key = _clean(idempotency_key)
    insert_values: dict[str, Any] = {
        "id": _new_opportunity_id(),
        **ctx.identity_columns(),
        "organization_id": frozen_org_id,
        "primary_contact_id": primary_contact_id,
        "title": title,
        "stage": stage_row["key"],
        "value": _to_amount(value),
        "currency": _clean(currency),
        "probability": probability,
        "close_date": close_date,
        "closed_at": now if stage_row["outcome"] != OPEN else None,
        "source": _clean(source),
        "idempotency_key": key,
        "fields": patch,
        "created_at": now,
        "updated_at": now,
    }

    if key is not None:
        row, created = await _upsert_returning(
            conn,
            table=opportunity,
            insert_values=insert_values,
            conflict_index_elements=[
                opportunity.c.tenant_id,
                opportunity.c.workspace_id,
                opportunity.c.idempotency_key,
            ],
            conflict_where=opportunity.c.idempotency_key.isnot(None),
            reselect_predicate=and_(
                opportunity.c.tenant_id == ctx.tenant_id,
                opportunity.c.workspace_id == ctx.workspace_id,
                opportunity.c.idempotency_key == key,
            ),
        )
        action = "created" if created else "matched"
        row_id = row["id"]
        if not created and patch:
            await conn.execute(
                opportunity.update()
                .where(opportunity.c.id == row_id)
                .values(fields=_fields_filled(conn, opportunity, patch))
            )
    else:
        await conn.execute(opportunity.insert().values(**insert_values))
        action = "created"
        row_id = insert_values["id"]

    await write_audit(
        conn,
        ctx,
        entity="opportunity",
        action="find_or_create",
        outcome="ok",
        entity_id=row_id,
        detail=_fields_detail(patch, []),
    )
    final = await _opportunity_row(conn, row_id)  # re-select for the derived columns
    return Opportunity.from_row(final, retired=await _retired_for(conn, "opportunity")), action


async def update_opportunity(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    opportunity_id: str,
    title: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    probability: int | None = None,
    close_date: date | None = None,
    primary_contact_id: str | None = None,
    source: str | None = None,
    fields: Mapping[str, Any] | None = None,
    fields_by: str | None = None,
) -> Opportunity:
    """Partial update. Omitted (``None``) fields are unchanged; bumps ``updated_at``.

    Does NOT touch ``stage`` — that is the transition verb
    :func:`set_opportunity_stage` (which owns ``closed_at``). A new
    ``primary_contact_id`` is validated in-scope (404 otherwise). ``probability``
    becomes the deal's own number until the next stage move, which clears it
    unless :func:`set_opportunity_stage` is given a new one. ``fields`` overwrites custom fields key by key;
    a null value clears one.
    """
    _check_len("title", title, _CAP_NAME)
    _check_len("currency", currency, _CAP_CURRENCY)
    _check_len("source", source, _CAP_SHORT)
    _check_opportunity_numbers(value, probability)
    await _opportunity_row(conn, opportunity_id)  # 404 if out of scope
    if primary_contact_id is not None:
        await _contact_row(conn, primary_contact_id)  # validate the edge target in-scope
    clean_title: str | None = None
    if title is not None:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("title must not be empty")
    patch, removed = await _field_patch(conn, entity="opportunity", submitted=fields, by=fields_by)
    candidate = {
        "title": clean_title,
        "value": _to_amount(value),
        "currency": _clean(currency),
        "probability": probability,
        "close_date": close_date,
        "primary_contact_id": primary_contact_id,
        "source": _clean(source),
    }
    updates = _partial_updates(candidate)
    if patch or removed:
        updates["fields"] = _fields_written(conn, opportunity, patch, removed)
    if updates:
        updates["updated_at"] = _now()
        await conn.execute(
            opportunity.update().where(opportunity.c.id == opportunity_id).values(**updates)
        )
    row = await _opportunity_row(conn, opportunity_id)
    await write_audit(
        conn,
        ctx,
        entity="opportunity",
        action="update",
        outcome="ok",
        entity_id=opportunity_id,
        detail=_fields_detail(patch, removed),
    )
    return Opportunity.from_row(row, retired=await _retired_for(conn, "opportunity"))


async def set_opportunity_stage(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    opportunity_id: str,
    stage: str,
    probability: int | None = None,
) -> Opportunity:
    """Move the opportunity to ``stage`` — the transition verb that owns ``closed_at``.

    The stage must be one the workspace's pipeline holds. Entering a won or lost
    stage stamps ``closed_at``; an open stage clears it. The move takes the stage's
    default probability: the deal's own number is cleared unless ``probability``
    gives it a new one.
    """
    _check_len("stage", stage, _CAP_SHORT)
    _check_opportunity_numbers(None, probability)
    clean_stage = _clean(stage)
    if clean_stage is None:
        raise ValueError("stage must not be empty")
    await _opportunity_row(conn, opportunity_id)  # 404 if out of scope
    stage_row = await _stage_for(conn, ctx, clean_stage)
    now = _now()
    await conn.execute(
        opportunity.update()
        .where(opportunity.c.id == opportunity_id)
        .values(
            stage=stage_row["key"],
            probability=probability,
            closed_at=(now if stage_row["outcome"] != OPEN else None),
            updated_at=now,
        )
    )
    row = await _opportunity_row(conn, opportunity_id)
    await write_audit(
        conn, ctx, entity="opportunity", action="set_stage", outcome="ok", entity_id=opportunity_id
    )
    return Opportunity.from_row(row, retired=await _retired_for(conn, "opportunity"))


async def set_opportunity_dossier(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    opportunity_id: str,
    dossier: str,
    append: bool = False,
) -> tuple[Opportunity, datetime | None]:
    """Write an opportunity's dossier; stamp ``dossier_updated_at``. Returns ``(Opportunity, previous_ts)``.

    An empty string clears the dossier (back to ``None``); ``append`` adds the text
    after the current one instead. Returns the previous timestamp so the tool can
    report staleness movement (mirrors :func:`set_contact_dossier`).
    """
    row = await _opportunity_row(conn, opportunity_id)  # 404 if out of scope
    previous = row["dossier_updated_at"]
    await _write_dossier(conn, opportunity, opportunity_id, dossier, append=append)
    fresh = await _opportunity_row(conn, opportunity_id)
    await write_audit(
        conn,
        ctx,
        entity="opportunity",
        action="set_dossier",
        outcome="ok",
        entity_id=opportunity_id,
    )
    return Opportunity.from_row(fresh, retired=await _retired_for(conn, "opportunity")), previous


async def get_opportunity(
    conn: AsyncConnection, ctx: Principal, *, opportunity_id: str
) -> tuple[Opportunity, dict[str, int]]:
    """Fetch an opportunity + related counts. 404 (not 403) if out of scope."""
    await _ensure_pipeline(conn, ctx)
    row = await _opportunity_row(conn, opportunity_id)
    interaction_count = await _count(
        conn, interaction, interaction.c.opportunity_id == opportunity_id
    )
    _log_access(ctx, entity="opportunity", action="get", entity_id=opportunity_id)
    retired = (await _retired_marks(conn)).get("opportunity")
    return Opportunity.from_row(row, details=True, retired=retired), {
        "interactions": interaction_count
    }


async def list_opportunities(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    primary_contact_id: str | None = None,
    stage: str | None = None,
    outcome: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Opportunity], str | None]:
    """List opportunities, newest first: ``(opportunities, next_cursor)``. See :func:`page_opportunities`."""
    page = await page_opportunities(
        conn,
        ctx,
        organization_id=organization_id,
        primary_contact_id=primary_contact_id,
        stage=stage,
        outcome=outcome,
        limit=limit,
        cursor=cursor,
    )
    return page.rows, page.next_cursor


async def page_opportunities(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    primary_contact_id: str | None = None,
    stage: str | None = None,
    outcome: str | None = None,
    terms: str | None = None,
    filter: Any = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: Sequence[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page:
    """One page of deals under a filter, in a chosen order.

    ``terms`` matches the deal's title and its account's name (:func:`_terms_predicate`).

    The same grammar as :func:`page_contacts`, over the deal's core and custom
    fields, its account's under ``organization.``, its primary contact's under
    ``contact.``, and its interaction rollups. ``stage``, ``outcome``,
    ``probability`` and ``weighted_value`` read the stage row.

    **Open by default.** Unless the request speaks to open-ness (the ``stage`` or
    ``outcome`` argument, or a filter naming either path), only open deals are
    listed: won and lost deals are history, not the pipeline.
    """
    limit = _clamp_limit(limit)
    await _ensure_pipeline(conn, ctx)
    resolver = await _opportunity_resolver(conn) if (filter or sort or columns) else None
    predicate, asked, excluded, joins = (
        query.where(filter, resolver) if resolver else (None, {}, {}, set())
    )
    conditions: list[ColumnElement[bool]] = []
    if organization_id is not None:
        conditions.append(opportunity.c.organization_id == organization_id)
    if primary_contact_id is not None:
        conditions.append(_PRIMARY_CONTACT == primary_contact_id)
    if stage is not None:
        conditions.append(opportunity.c.stage == stage)
    if outcome is not None:
        conditions.append(_OPPORTUNITY_OUTCOME == outcome)
    elif stage is None and not _closed_asked(asked, excluded):
        conditions.append(_OPPORTUNITY_OUTCOME == OPEN)
    joins = set(joins)
    matched = _terms_predicate(terms, (opportunity.c.title, organization.c.name))
    if matched is not None:
        conditions.append(matched)
        joins.add("organization")
    if predicate is not None:
        conditions.append(predicate)
    return await _page(
        conn,
        ctx,
        table=opportunity,
        base=select(*_OPPORTUNITY_COLUMNS),
        default_listing=_OPPORTUNITIES,
        name="opportunities",
        entity="opportunity",
        model=Opportunity,
        resolver=resolver,
        conditions=conditions,
        joins={"stage", *joins},
        sort=sort,
        columns=columns,
        limit=limit,
        cursor=cursor,
        log=lambda: _log_access(ctx, entity="opportunity", action="list"),
    )


async def pipeline_summary(
    conn: AsyncConnection, ctx: Principal, *, organization_id: str | None = None
) -> dict[str, Any]:
    """Aggregate the pipeline: per-stage counts, value and weighted value, and open/won/lost totals.

    ``by_stage`` holds every stage of the pipeline in order, empty ones included,
    so a board draws its columns from it. Optionally scoped to one
    ``organization_id``. Sums cover only rows with a ``value``; ``weighted_sum``
    is ``value × probability / 100`` over them. A read — emits an access log
    line, no audit row.
    """
    stages = await _ensure_pipeline(conn, ctx)
    stmt = (
        select(
            opportunity.c.stage,
            _OPPORTUNITY_OUTCOME.label("outcome"),
            func.count().label("count"),
            func.sum(opportunity.c.value).label("value_sum"),
            func.sum(_WEIGHTED_VALUE).label("weighted_sum"),
        )
        .select_from(_STAGE_JOIN)
        .where(*_scope(opportunity, ctx))
        .group_by(opportunity.c.stage, _OPPORTUNITY_OUTCOME)
    )
    if organization_id is not None:
        stmt = stmt.where(opportunity.c.organization_id == organization_id)
    rows = {row["stage"]: row for row in await _all(conn, stmt)}

    by_stage: dict[str, dict[str, Any]] = {}
    open_count = won_count = lost_count = 0
    total_value = total_weighted = 0.0

    def tally(key: str, label: str, oc: str, row: RowMapping | None) -> None:
        nonlocal open_count, won_count, lost_count, total_value, total_weighted
        count = int(row["count"]) if row is not None else 0
        entry: dict[str, Any] = {"label": label, "outcome": oc, "count": count}
        if row is not None and row["value_sum"] is not None:
            entry["value_sum"] = float(row["value_sum"])
            entry["weighted_sum"] = float(row["weighted_sum"] or 0)
            total_value += entry["value_sum"]
            total_weighted += entry["weighted_sum"]
        by_stage[key] = entry
        if oc == WON:
            won_count += count
        elif oc == LOST:
            lost_count += count
        else:
            open_count += count

    for stage_row in stages:
        tally(
            stage_row["key"],
            stage_row["label"],
            stage_row["outcome"],
            rows.pop(stage_row["key"], None),
        )
    # A deal in a stage the pipeline no longer names still counts.
    for key, row in rows.items():
        tally(key, key, row["outcome"], row)

    _log_access(ctx, entity="opportunity", action="pipeline_summary")
    result: dict[str, Any] = {
        "by_stage": by_stage,
        "open_count": open_count,
        "won_count": won_count,
        "lost_count": lost_count,
    }
    if total_value:
        result["total_value"] = total_value
        result["total_weighted"] = total_weighted
    return result


# --------------------------------------------------------------------------- #
# Evidence: signals and proof points                                           #
# --------------------------------------------------------------------------- #
#
# Two tables, one column convention, different attestors. A `signal` is a fact
# about an account attested by a URL anyone can open; a `proof_point` is
# a claim about our own work attested by a person who says they stand behind it.
# Neither is ever deleted: a signal is withdrawn, a proof point is re-attested.
#
# The dossier is untouched by all of this. It stays the PROSE SYNTHESIS above the
# evidence rows — "here is what we think about this account" — and these records
# are what it must be answerable to. Nothing here migrates dossier content, and
# no read here parses it.


async def _signal_row(conn: AsyncConnection, signal_id: str, *, lock: bool = False) -> RowMapping:
    """The signal row, or :class:`SignalNotFoundError`. ``lock`` takes its row lock for the
    transaction, which serializes a change to its parties against its withdrawal."""
    stmt = select(signal).where(signal.c.id == signal_id, _live(signal))
    if lock:
        stmt = stmt.with_for_update()
    row = await _one(conn, stmt)
    if row is None:
        raise SignalNotFoundError(signal_id)
    return row


async def _proof_point_row(conn: AsyncConnection, proof_point_id: str) -> RowMapping:
    row = await _one(
        conn, select(proof_point).where(proof_point.c.id == proof_point_id, _live(proof_point))
    )
    if row is None:
        raise ProofPointNotFoundError(proof_point_id)
    return row


# --- The signal vocabulary ------------------------------
#
# A signal's type is a key into `signal_type`, one row per key per workspace. The
# label and description are the type's; a signal stores only the key.


def _new_signal_type_id() -> str:
    from mcp_people.ids import new_signal_type_id

    return new_signal_type_id()


async def _signal_type_rows(conn: AsyncConnection, ctx: Principal) -> list[RowMapping]:
    """The active workspace's signal types by key. Scoped here, not by RLS alone: the
    read policy admits every workspace the caller may read, and a vocabulary is one
    workspace's."""
    return list(
        await _all(
            conn,
            select(signal_type_table)
            .where(signal_type_table.c.workspace_id == ctx.workspace_id)
            .order_by(signal_type_table.c.key),
        )
    )


async def _ensure_signal_types(conn: AsyncConnection, ctx: Principal) -> list[RowMapping]:
    """The workspace's signal types, seeding one per type its signals already carry.

    A workspace whose signals predate the vocabulary gets a type for each key they
    hold, labelled with the key, so no signal is left holding a type its workspace
    does not define. A workspace with no signals stays empty: there is no default
    list. Every vocabulary-aware read and write calls this, because a migration
    cannot: it runs as the app role, which the FORCE'd policies bind to no
    workspace. The seed is idempotent under the unique key.

    The seed runs only while the workspace holds no row at all. A retired type keeps its
    row, so a vocabulary whose every type is retired is not empty and is never seeded
    again: retiring cannot bring a type back.
    """
    rows = await _signal_type_rows(conn, ctx)
    if rows:
        return rows
    held = (
        (
            await conn.execute(
                select(signal.c.signal_type)
                .where(signal.c.workspace_id == ctx.workspace_id, _live(signal))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not held:
        return []
    now = _now()
    for key in sorted(held):
        await conn.execute(
            insert_for(conn.dialect.name, signal_type_table)
            .values(
                id=_new_signal_type_id(),
                **ctx.identity_columns(),
                key=key,
                label=key,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    signal_type_table.c.tenant_id,
                    signal_type_table.c.workspace_id,
                    signal_type_table.c.key,
                ]
            )
        )
    return await _signal_type_rows(conn, ctx)


async def _signal_type_for(conn: AsyncConnection, ctx: Principal, key: str) -> str:
    """The vocabulary key a signal may carry, or a refusal naming the ones it could.

    The type returned is read under a shared lock held to commit, so a retirement
    (``update_signal_type``, which locks the row exclusively) either waits for this
    signal or commits first and this read sees the type retired.
    """
    types = await _ensure_signal_types(conn, ctx)
    clean = (key or "").strip().lower()
    found = next((row for row in types if row["key"] == clean), None)
    if found is not None and found["retired_at"] is None:
        locked = await _one(
            conn,
            select(signal_type_table)
            .where(signal_type_table.c.id == found["id"])
            .with_for_update(read=True),
        )
        if locked is not None and locked["retired_at"] is None:
            return clean
        types = await _signal_type_rows(conn, ctx)
    for row in types:
        if row["key"] == clean:
            if row["retired_at"] is not None:
                raise FieldError(
                    f"signal type {clean!r} is retired, so no signal is recorded under it",
                    next_step=_retired_next_step(
                        types, clean, kind="signal type", tool="update_signal_type"
                    ),
                )
            return clean
    live = _live_rows(types)
    if not live:
        raise FieldError(
            f"signal type {key!r} is not defined: this workspace defines no signal types yet",
            next_step=(
                "Add one with define_signal_type(key=..., label=...); list_signal_types "
                "lists them once defined."
            ),
        )
    raise FieldError(
        f"signal type {key!r} is not in this workspace's vocabulary",
        next_step=(
            "Use one of: " + ", ".join(row["key"] for row in live) + ". "
            "list_signal_types lists them with what each means; define_signal_type adds "
            "one."
        ),
    )


async def _active_signal_counts(conn: AsyncConnection, ctx: Principal) -> dict[str, int]:
    """How many active signals of the active workspace carry each type."""
    return {
        row["signal_type"]: int(row["count"])
        for row in await _all(
            conn,
            select(signal.c.signal_type, func.count().label("count"))
            .where(
                signal.c.workspace_id == ctx.workspace_id, signal.c.status == ACTIVE, _live(signal)
            )
            .group_by(signal.c.signal_type),
        )
    }


async def describe_signal_types(
    conn: AsyncConnection, ctx: Principal, *, include_retired: bool = False
) -> list[SignalType]:
    """The workspace's live signal types by key, each with how many active signals carry
    it; the retired ones too with ``include_retired``."""
    types = await _ensure_signal_types(conn, ctx)
    if not include_retired:
        types = _live_rows(types)
    counts = await _active_signal_counts(conn, ctx)
    _log_access(ctx, entity="signal_type", action="describe")
    return [SignalType.from_row(row, signal_count=counts.get(row["key"], 0)) for row in types]


async def define_signal_type(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    key: str,
    label: str,
    description: str | None = None,
) -> tuple[SignalType, str]:
    """Add a signal type under ``key``. Returns ``(type, action)``.

    A key the vocabulary already holds is returned as it is (``matched``): defining
    never changes a type, :func:`update_signal_type` does. A retired key stays taken:
    defining it is refused, and restoring it is ``update_signal_type(retired=False)``.
    """
    clean_key = normalize_signal_type_key(key)
    types = await _ensure_signal_types(conn, ctx)
    existing = next((row for row in types if row["key"] == clean_key), None)
    if existing is not None and existing["retired_at"] is not None:
        raise FieldError(
            f"signal type {clean_key!r} is retired, and its key stays taken",
            next_step=_retired_next_step(
                types, clean_key, kind="signal type", tool="update_signal_type"
            ),
        )
    if existing is None:
        if len(_live_rows(types)) >= MAX_SIGNAL_TYPES:
            raise ValueError(f"a workspace holds at most {MAX_SIGNAL_TYPES} signal types")
        now = _now()
        values = {
            "id": _new_signal_type_id(),
            **ctx.identity_columns(),
            "key": clean_key,
            "label": clean_signal_type_label(label),
            "description": clean_signal_type_description(description),
            "created_at": now,
            "updated_at": now,
        }
        row, created = await _upsert_returning(
            conn,
            table=signal_type_table,
            insert_values=values,
            conflict_index_elements=[
                signal_type_table.c.tenant_id,
                signal_type_table.c.workspace_id,
                signal_type_table.c.key,
            ],
            conflict_where=None,
            reselect_predicate=and_(
                signal_type_table.c.workspace_id == ctx.workspace_id,
                signal_type_table.c.key == clean_key,
            ),
        )
        action = "created" if created else "matched"
        if created:
            await write_audit(
                conn, ctx, entity="signal_type", action="define", outcome="ok", entity_id=row["id"]
            )
    else:
        row, action = existing, "matched"
    counts = await _active_signal_counts(conn, ctx)
    return SignalType.from_row(row, signal_count=counts.get(clean_key, 0)), action


async def update_signal_type(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    key: str,
    label: str | None = None,
    description: str | None = None,
    retired: bool | None = None,
    replaced_by: str | None = None,
) -> tuple[SignalType, str]:
    """Retire, restore or edit the signal type under ``key``: only the arguments given.

    Returns ``(type, action)``, ``action`` being ``retired``, ``restored``, ``updated``
    (label or description changed) or ``unchanged``. An empty ``description`` clears it.
    A signal of the type keeps its key, so no change here touches a signal.

    ``retired=True`` stamps ``retired_at`` once; retiring again keeps the stamp and
    changes the replacement only when one is given. A type retires while signals carry
    it: they keep it, ``list_signals`` still filters on it, and no new signal takes it.
    Its key stays taken. ``retired=False`` clears both. ``replaced_by`` names a live
    type other than this one, and only a retired type has one.
    """
    clean_key = normalize_signal_type_key(key)
    types = await _ensure_signal_types(conn, ctx)
    existing = next((row for row in types if row["key"] == clean_key), None)
    if existing is None:
        raise FieldError(
            f"signal type {key!r} is not in this workspace's vocabulary",
            next_step=(
                ("Use one of: " + ", ".join(row["key"] for row in types) + "; " if types else "")
                + "add a new type with define_signal_type."
            ),
        )
    locked = await _one(
        conn,
        select(signal_type_table).where(signal_type_table.c.id == existing["id"]).with_for_update(),
    )
    existing = locked if locked is not None else existing
    was_retired = existing["retired_at"] is not None
    stays_retired = retired is True or (retired is None and was_retired)
    target: str | None = None
    if replaced_by is not None:
        if not stays_retired:
            raise FieldError(
                "replaced_by applies only to a retired signal type",
                next_step=(
                    f"Retire it and name the replacement in one call: update_signal_type("
                    f"key={clean_key!r}, retired=true, replaced_by=...)."
                ),
            )
        target = normalize_signal_type_key(replaced_by)
        if target == clean_key:
            raise FieldError(
                f"signal type {clean_key!r} cannot replace itself",
                next_step="Name the live type that takes its place, or leave replaced_by out.",
            )
        found = next((row for row in types if row["key"] == target), None)
        if found is None or found["retired_at"] is not None:
            state = "is not in this workspace's vocabulary" if found is None else "is retired"
            live = [row["key"] for row in _live_rows(types)]
            raise FieldError(
                f"replacement signal type {target!r} {state}",
                next_step=(
                    "A replacement must be a live type"
                    + (": one of " + ", ".join(live) + "." if live else "; define one first.")
                ),
            )
    if retired is False and was_retired and len(_live_rows(types)) >= MAX_SIGNAL_TYPES:
        raise ValueError(
            f"a workspace holds at most {MAX_SIGNAL_TYPES} live signal types; retire one "
            "before restoring this one"
        )
    updates: dict[str, Any] = {}
    if label is not None and clean_signal_type_label(label) != existing["label"]:
        updates["label"] = clean_signal_type_label(label)
    if description is not None:
        cleaned = clean_signal_type_description(description)
        if cleaned != existing["description"]:
            updates["description"] = cleaned
    now = _now()
    if retired is False:
        if was_retired:
            updates["retired_at"] = None
            updates["replaced_by_key"] = None
    else:
        if retired is True and not was_retired:
            updates["retired_at"] = now
        if target is not None and target != existing["replaced_by_key"]:
            updates["replaced_by_key"] = target
    retirement = {"retired_at", "replaced_by_key"} & set(updates)
    if updates:
        await conn.execute(
            signal_type_table.update()
            .where(signal_type_table.c.id == existing["id"])
            .values(**updates, updated_at=now)
        )
    row = await _one(
        conn, select(signal_type_table).where(signal_type_table.c.id == existing["id"])
    )
    assert row is not None
    if updates:
        await write_audit(
            conn,
            ctx,
            entity="signal_type",
            action="update",
            outcome="ok",
            entity_id=existing["id"],
            detail={
                "key": clean_key,
                "retired": row["retired_at"] is not None,
                "replaced_by": row["replaced_by_key"],
            },
        )
    if retirement:
        action = "restored" if retired is False else "retired"
    else:
        action = "updated" if updates else "unchanged"
    counts = await _active_signal_counts(conn, ctx)
    return SignalType.from_row(row, signal_count=counts.get(clean_key, 0)), action


async def record_signal(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str,
    claim: str,
    evidence_quote: str,
    source_url: str,
    signal_type: str,
    contact_id: str | None = None,
    source_date: date | None = None,
    event_date: date | None = None,
    found_by: str | None = None,
    evidence_for: Sequence[str] | None = None,
) -> tuple[Signal, str, bool]:
    """Record one attested signal about an organization.

    Returns ``(Signal, action, stored)``. ``action`` is ``created`` or ``matched``;
    ``stored`` is whether the returned row holds the claim and quote THIS call
    submitted. It is ``False`` in exactly one case, and that case is why it is
    returned at all: the key matched a row recording a DIFFERENT fact, so the
    caller is handed a signal it did not write.

    Idempotent on ``(organization_id, source_url, signal_type)`` via the unique
    index ``uq_signal_source`` (ON CONFLICT DO NOTHING + re-select), so re-running
    research over an account converges instead of stacking near-duplicates. A
    match fills only the EMPTY annotation fields (``contact_id`` / ``source_date``
    / ``event_date`` / ``found_by``); it never rewrites the claim or quote, because a second read of
    the same page is not authority to restate the first one. ``event_date`` is filled only
    when the match records the same claim and quote: it dates the fact, not the page.

    **The key assumes one fact per (page, type), and nothing enforces that.** A
    release announcing two expansions, or a careers page listing two hires, has
    two facts the key cannot tell apart; the second call matches the first row and
    the second claim is not stored. Widening the key to include the claim would
    trade that for a worse failure — a re-run that paraphrases would stop
    converging, which is the property the whole idempotency contract rests on — so
    the collision is reported to the caller instead of designed away. ``stored``
    is that report, and the tool layer turns it into a next_step that says the
    submitted claim was discarded.

    **A match on a withdrawn signal returns it withdrawn.** Re-finding the source
    is not grounds to undo a judgment that the fact was wrong or retracted;
    resurrection would have to be a deliberate act, and there is no verb for it.

    A claim that drops a hedge its quote carries ("alleged", "reportedly") is refused
    (:func:`~mcp_people.evidence.dropped_hedge`): the hedge is part of the fact, and a
    claim is what a citation of the signal restates.

    ``event_date`` is when the event happened as the evidence states it, never derived
    from ``source_date``. It is refused when it is after today (UTC) or after the
    signal's ``source_date``: a source cannot attest that something happened after it
    was written. An announced plan is recorded with the date of the announcement or
    decision the source reports, or none, never the date the plan is for. The ordering
    is checked against the row as it stands after a match's fill, so a match cannot
    pair a stored date with a submitted one that contradicts it.

    ``source_url`` is shape-validated and normalized (:func:`normalize_source_url`);
    ``signal_type`` must be a key of the workspace's vocabulary
    (:func:`_signal_type_for`); anything else is rejected, not stored. The
    organization (and contact, when given) is validated in-scope in the same RLS
    transaction, so a cross-workspace id is a 404.

    ``evidence_for`` names whom the fact is evidence for, each
    party validated in scope like the organization. Its edges are added on a create, and
    on a match only when the match stored this call's fact and the signal is active: a
    purpose stated for a claim that was discarded would attach to a fact the caller did
    not record, and a withdrawn signal takes no edge change. Adding never removes an
    edge, so a fact found again for another purpose keeps both.
    """
    parties = parse_parties(evidence_for or [], required=False)
    _check_len("claim", claim, _CAP_CLAIM)
    _check_len("evidence_quote", evidence_quote, _CAP_QUOTE)
    _check_len("source_url", source_url, _CAP_URL)
    _check_len("found_by", found_by, _CAP_SHORT)
    clean_claim = _clean(claim)
    if clean_claim is None:
        raise ValueError("claim must not be empty")
    clean_quote = _clean(evidence_quote)
    if clean_quote is None:
        raise ValueError(
            "evidence_quote must not be empty — a claim without the words that "
            "support it is prose, not evidence"
        )
    hedge = dropped_hedge(claim=clean_claim, evidence_quote=clean_quote)
    if hedge is not None:
        raise ValueError(
            f"claim drops the hedge in its evidence_quote: the quote says {hedge!r} and the "
            "claim states it as fact. Keep the hedge and who says it in the claim (e.g. "
            "'A security blog alleges …', 'According to a security blog, …'), or quote only "
            "the words that support the claim"
        )
    url = normalize_source_url(source_url)
    if event_date is not None and event_date > _now().date():
        raise ValueError(
            f"event_date {event_date.isoformat()} is in the future: it is the date the event "
            "happened. A planned event takes the date of the announcement or decision the "
            "source reports, or no event_date"
        )
    _check_event_order(event_date, source_date)
    signal_type = await _signal_type_for(conn, ctx, signal_type)

    await _org_row(conn, organization_id)  # validate the account in-scope; 404 otherwise
    if contact_id is not None:
        contact_row = await _contact_row(conn, contact_id)  # 404 if out of scope
        if contact_row["organization_id"] not in (None, organization_id):
            raise ValueError("contact_id belongs to a different organization than organization_id")
    await _check_parties(conn, parties)

    stamp = db_now(conn.dialect.name)
    insert_values: dict[str, Any] = {
        "id": _new_signal_id(),
        **ctx.identity_columns(),
        "organization_id": organization_id,
        "contact_id": contact_id,
        "claim": clean_claim,
        "evidence_quote": clean_quote,
        "source_url": url,
        "source_date": source_date,
        "event_date": event_date,
        "signal_type": signal_type,
        "found_by": _clean(found_by),
        # The database's clock, which orders the change read.
        "created_at": stamp,
        "updated_at": stamp,
    }
    row, created = await _upsert_returning(
        conn,
        table=signal,
        insert_values=insert_values,
        conflict_index_elements=[
            signal.c.tenant_id,
            signal.c.workspace_id,
            signal.c.organization_id,
            signal.c.source_url,
            signal.c.signal_type,
        ],
        conflict_where=None,
        reselect_predicate=and_(
            signal.c.tenant_id == ctx.tenant_id,
            signal.c.workspace_id == ctx.workspace_id,
            signal.c.organization_id == organization_id,
            signal.c.source_url == url,
            signal.c.signal_type == signal_type,
        ),
    )
    # True on every create by construction, and on a match only when the row
    # already recorded the same fact. False means the key collided on a DIFFERENT
    # fact from the same page and type — see the docstring.
    stored = row["claim"] == clean_claim and row["evidence_quote"] == clean_quote
    if not created:
        # Fill the empty ANNOTATION fields only. The claim and quote of the
        # existing row stand: a second read of the same page adds context, never
        # authority to restate what was already recorded from it. The event date
        # belongs to the fact, not the page, so only the same fact may fill it.
        fill: dict[str, Any] = {
            "contact_id": contact_id,
            "source_date": source_date,
            "found_by": _clean(found_by),
        }
        if stored:
            fill["event_date"] = event_date
        row = await _fill_empty(conn, signal, row, fill, db_clock=True)
        # A fill can pair a stored date with a submitted one. Raising here rolls the
        # fill back with the rest of the transaction.
        _check_event_order(row["event_date"], row["source_date"])
    action = "created" if created else "matched"
    await write_audit(
        conn, ctx, entity="signal", action="record", outcome="ok", entity_id=row["id"]
    )
    if parties and stored:
        # Locked, so a concurrent withdrawal either lands first and is seen here, or
        # waits for these edges.
        row = await _signal_row(conn, row["id"], lock=True)
        if row["status"] == ACTIVE:
            await _add_edges(conn, ctx, row, parties, added_by=_clean(found_by))
            row = await _signal_row(conn, row["id"])
    return await _signal_out(conn, row), action, stored


def _check_event_order(event_date: date | None, source_date: date | None) -> None:
    """Refuse an ``event_date`` later than ``source_date``: a source cannot report that
    an event happened after the source was written."""
    if event_date is not None and source_date is not None and event_date > source_date:
        raise ValueError(
            f"event_date {event_date.isoformat()} is after source_date "
            f"{source_date.isoformat()}: a source cannot attest an event that had not "
            "happened when it was written. Record the date the source says the event "
            "happened, or no event_date"
        )


async def withdraw_signal(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    signal_id: str,
    reason: str | None = None,
) -> Signal:
    """Withdraw a signal — the only way one leaves the active set. 404 if out of scope.

    There is no delete. Withdrawing keeps the row, so the record of having
    believed the fact (and of anything that may already cite it) survives; it just
    stops appearing in the default listings.

    Idempotent: withdrawing an already-withdrawn signal returns it unchanged and
    does NOT restamp ``withdrawn_at`` or overwrite the original reason — the first
    withdrawal is the one that happened.
    """
    _check_len("reason", reason, _CAP_SUMMARY)
    row = await _signal_row(conn, signal_id)  # 404 if out of scope
    if row["status"] != WITHDRAWN:
        stamp = db_now(conn.dialect.name)
        await conn.execute(
            signal.update()
            .where(signal.c.id == signal_id)
            .values(
                status=WITHDRAWN,
                withdrawn_at=stamp,
                withdrawn_reason=_clean(reason),
                updated_at=stamp,
            )
        )
        row = await _signal_row(conn, signal_id)
    await write_audit(
        conn, ctx, entity="signal", action="withdraw", outcome="ok", entity_id=signal_id
    )
    return await _signal_out(conn, row)


async def get_signal(conn: AsyncConnection, ctx: Principal, *, signal_id: str) -> Signal:
    """Fetch one signal. 404 (not 403) if out of scope."""
    row = await _signal_row(conn, signal_id)
    _log_access(ctx, entity="signal", action="get", entity_id=signal_id)
    return await _signal_out(conn, row)


def _signal_filters(
    stmt: Select,
    ctx: Principal,
    *,
    organization_id: str | None,
    contact_id: str | None,
    signal_type: str | None,
    status: str | None,
    evidence_for: str | None,
    removed_edges_match: bool,
) -> Select:
    """The WHERE of both signal reads. ``status`` defaults to active; ``any`` reads both
    states. ``evidence_for`` is ``"workspace"``, an organization id, or ``"unstated"`` (no
    live party). ``removed_edges_match`` lets a party match an edge that was removed, so a
    reader of changes filtered to a party sees its removal."""
    stmt = stmt.where(*_scope(signal, ctx))
    if organization_id is not None:
        stmt = stmt.where(signal.c.organization_id == organization_id)
    if contact_id is not None:
        stmt = stmt.where(signal.c.contact_id == contact_id)
    if signal_type is not None:
        stmt = stmt.where(signal.c.signal_type == signal_type)
    wanted = validate_choice("status", status or ACTIVE, SIGNAL_STATUSES | {ANY_STATUS})
    if wanted != ANY_STATUS:
        stmt = stmt.where(signal.c.status == wanted)
    if evidence_for is not None:
        party = evidence_for.strip()
        edge = signal_evidence_for.c
        if party.lower() == UNSTATED:
            stmt = stmt.where(
                ~exists().where(
                    edge.signal_id == signal.c.id, edge.removed_at.is_(None), _live_party(edge)
                )
            )
        else:
            match = (
                edge.for_organization_id.is_(None)
                if party.lower() == WORKSPACE
                else edge.for_organization_id == party
            )
            live = [] if removed_edges_match else [edge.removed_at.is_(None)]
            stmt = stmt.where(
                exists().where(edge.signal_id == signal.c.id, match, _live_party(edge), *live)
            )
    return stmt


async def list_signals(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    contact_id: str | None = None,
    signal_type: str | None = None,
    status: str | None = None,
    evidence_for: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Signal], str | None]:
    """List signals, newest first, with opaque keyset pagination.

    **Withdrawn signals are excluded by default** — the same allowlist discipline
    as ``list_organizations``: the default is ``status="active"``, applied as a
    WHERE predicate before LIMIT so hidden rows never short-page a result, and
    reading withdrawn ones takes an explicit ``status="withdrawn"`` (or ``"any"``).
    This is the read made before citing or re-researching an account, and a
    withdrawn signal reaching it is exactly the failure withdrawal exists to prevent.

    ``evidence_for`` keeps the signals with a live edge to that party, or with none
    (``"unstated"``). Each signal carries its live parties.

    Returns ``(signals, next_cursor)``.
    """
    limit = _clamp_limit(limit)
    stmt = _signal_filters(
        select(signal),
        ctx,
        organization_id=organization_id,
        contact_id=contact_id,
        signal_type=signal_type,
        status=status,
        evidence_for=evidence_for,
        removed_edges_match=False,
    )
    stmt = _SIGNALS.apply(stmt, cursor)
    rows = await _all(conn, stmt.limit(limit + 1))
    page, next_cursor = _SIGNALS.paginate(rows, limit)
    _log_access(ctx, entity="signal", action="list")
    return await _signals_out(conn, page), next_cursor


# How long a change waits before the change read returns it. A transaction stamps
# `updated_at` with its START time (`now()`) and becomes visible only when it commits, so
# a mark taken past a row stamped earlier but committed later would skip it. Holding back
# every change younger than the longest signal-writing transaction closes that gap, and
# SIGNAL_WRITE_DEADLINE is what bounds that transaction: every tool that writes a signal
# or its parties runs its whole session (admission, lock waits, statements and commit)
# under it, and a call past the deadline is cancelled and rolls back, so no COMMIT is sent
# after it. A signal write touches a few rows, so only a stall reaches the deadline, and
# the stalled call returns a retryable internal error. The settle margin's excess over the deadline covers a COMMIT already in flight
# when the deadline fires. A reader polling to re-score loses nothing to a five-minute
# lag. Keep SIGNAL_WRITE_DEADLINE well under SIGNAL_CHANGE_SETTLE.
SIGNAL_CHANGE_SETTLE = timedelta(minutes=5)
SIGNAL_WRITE_DEADLINE = timedelta(minutes=2)


async def list_signal_changes(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    changed_since: str,
    organization_id: str | None = None,
    contact_id: str | None = None,
    signal_type: str | None = None,
    status: str | None = None,
    evidence_for: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[list[Signal], str, bool]:
    """Signals changed since a reader's mark, oldest change first.

    ``changed_since`` is the mark a previous call returned, or ``""`` to start from the
    beginning. Returns ``(signals, next_mark, more)``: ``next_mark`` is always a mark to
    keep (the last row returned, or ``changed_since`` itself when none changed), and
    ``more`` says another page is already waiting. Delivery is at least once.

    Three rules make the read complete. Every write stamps ``updated_at`` with the
    database's ``now()``, so one clock orders changes. A change is returned only once it
    is older than :data:`SIGNAL_CHANGE_SETTLE`, so a transaction that stamped earlier
    and committed later is not passed over. And a party filter matches removed edges too,
    so a reader filtered to a party sees that party being removed.

    Raises ``InvalidCursor`` (a ``ValueError``) for a mark that is not a change mark,
    such as a listing cursor.
    """
    limit = _clamp_limit(limit)
    stmt = _signal_filters(
        select(signal),
        ctx,
        organization_id=organization_id,
        contact_id=contact_id,
        signal_type=signal_type,
        status=status,
        evidence_for=evidence_for,
        removed_edges_match=True,
    ).where(signal.c.updated_at < dialect.db_ago(conn.dialect.name, SIGNAL_CHANGE_SETTLE))
    stmt = _SIGNAL_CHANGES.apply(stmt, changed_since)
    rows = await _all(conn, stmt.limit(limit + 1))
    page = list(rows[:limit])
    mark = _SIGNAL_CHANGES.encode(_SIGNAL_CHANGES.key(page[-1])) if page else changed_since
    _log_access(ctx, entity="signal", action="list_changes")
    return await _signals_out(conn, page), mark, len(rows) > limit


# --- Whom a signal is evidence for --------------------
#
# One `signal_evidence_for` row per (signal, party); a NULL organization is the
# workspace. Adding only adds, removing keeps the row with `removed_at`, and every change
# moves the signal's `updated_at` so a reader of changes sees it.


def _party_wire(for_organization_id: str | None) -> str:
    return WORKSPACE if for_organization_id is None else for_organization_id


def _party_key(column: Any) -> ColumnElement[Any]:
    """The party as the unique index folds it: NULL (the workspace) to ``''``."""
    return func.coalesce(column, literal_column("''"))


async def _check_parties(conn: AsyncConnection, parties: Sequence[str | None]) -> None:
    """Each named organization is in scope, or :class:`OrganizationNotFoundError`."""
    for party in parties:
        if party is not None:
            await _org_row(conn, party)


async def _live_parties(conn: AsyncConnection, signal_ids: Sequence[str]) -> dict[str, list[str]]:
    """Each signal's live parties, the workspace first, then organization ids in order."""
    if not signal_ids:
        return {}
    edge = signal_evidence_for.c
    rows = await _all(
        conn,
        select(edge.signal_id, edge.for_organization_id)
        .where(edge.signal_id.in_(signal_ids), edge.removed_at.is_(None), _live_party(edge))
        .order_by(edge.signal_id, edge.for_organization_id.asc().nulls_first()),
    )
    parties: dict[str, list[str]] = {}
    for row in rows:
        parties.setdefault(row["signal_id"], []).append(_party_wire(row["for_organization_id"]))
    return parties


async def _signal_out(conn: AsyncConnection, row: RowMapping) -> Signal:
    return (await _signals_out(conn, [row]))[0]


async def _signals_out(conn: AsyncConnection, rows: Sequence[RowMapping]) -> list[Signal]:
    parties = await _live_parties(conn, [r["id"] for r in rows])
    return [Signal.from_row(r, evidence_for=parties.get(r["id"], [])) for r in rows]


async def _touch_signal(conn: AsyncConnection, signal_id: str) -> None:
    """Move the signal's change stamp, so a reader of changes sees an edge change."""
    await conn.execute(
        signal.update().where(signal.c.id == signal_id).values(updated_at=db_now(conn.dialect.name))
    )


async def _put_edge(
    conn: AsyncConnection,
    ctx: Principal,
    signal_id: str,
    party: str | None,
    *,
    added_by: str | None,
) -> bool:
    """Add a live edge from the signal to ``party``, or revive a removed one; whether it
    changed. An edge already live is left as it is."""
    edge = signal_evidence_for.c
    stamp = db_now(conn.dialect.name)
    stmt = (
        insert_for(conn.dialect.name, signal_evidence_for)
        .values(
            **ctx.identity_columns(),
            signal_id=signal_id,
            for_organization_id=party,
            added_by=added_by,
            created_at=stamp,
            updated_at=stamp,
        )
        .on_conflict_do_update(
            index_elements=[
                edge.tenant_id,
                edge.workspace_id,
                edge.signal_id,
                _party_key(edge.for_organization_id),
            ],
            set_={"removed_at": None, "updated_at": stamp},
            where=edge.removed_at.is_not(None),
        )
        .returning(edge.signal_id)
    )
    return (await conn.execute(stmt)).first() is not None


async def _add_edges(
    conn: AsyncConnection,
    ctx: Principal,
    row: RowMapping,
    parties: Sequence[str | None],
    *,
    added_by: str | None,
) -> list[str]:
    """Add an edge per party to an active signal, reviving a removed one. Returns the
    parties that changed; the signal's stamp moves and the audit names them only if any
    did. An edge already live is left as it is."""
    changed = [
        _party_wire(party)
        for party in parties
        if await _put_edge(conn, ctx, row["id"], party, added_by=added_by)
    ]
    if changed:
        await _touch_signal(conn, row["id"])
        await write_audit(
            conn,
            ctx,
            entity="signal_evidence_for",
            action="add",
            outcome="ok",
            entity_id=row["id"],
            detail={"parties": changed},
        )
    return changed


async def _signal_for_edge_change(
    conn: AsyncConnection, signal_id: str, parties: Sequence[str | None]
) -> RowMapping:
    """The signal, locked against a concurrent withdrawal, once it and every party are in
    scope; :class:`SignalWithdrawnError` when it is withdrawn."""
    row = await _signal_row(conn, signal_id, lock=True)
    await _check_parties(conn, parties)
    if row["status"] == WITHDRAWN:
        raise SignalWithdrawnError(signal_id)
    return row


async def add_signal_for(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    signal_id: str,
    evidence_for: Sequence[str],
    added_by: str | None = None,
) -> tuple[Signal, list[str]]:
    """Add whom a recorded signal is evidence for. Idempotent.

    Each party is ``"workspace"`` or an in-scope organization id. An edge already live is
    returned as it is; one that was removed comes back. Returns ``(Signal, added)``,
    ``added`` naming the parties that changed. A withdrawn signal is refused
    (:class:`SignalWithdrawnError`), and the edges never change the signal's claim, quote,
    dates or status.
    """
    parties = parse_parties(evidence_for, required=True)
    _check_len("added_by", added_by, _CAP_SHORT)
    row = await _signal_for_edge_change(conn, signal_id, parties)
    added = await _add_edges(conn, ctx, row, parties, added_by=_clean(added_by))
    if not added:
        await write_audit(
            conn,
            ctx,
            entity="signal_evidence_for",
            action="add",
            outcome="ok",
            entity_id=signal_id,
            detail={"parties": []},
        )
    return await _signal_out(conn, await _signal_row(conn, signal_id)), added


async def remove_signal_for(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    signal_id: str,
    evidence_for: Sequence[str],
) -> tuple[Signal, list[str]]:
    """Remove whom a signal is evidence for: the fact was filed under the wrong purpose.

    Sets ``removed_at`` on each named live edge and keeps the row, so a reader of changes
    filtered to that party still receives the removal; an edge that is not live is left
    as it is. Returns ``(Signal, removed)``. A withdrawn signal is refused
    (:class:`SignalWithdrawnError`): withdrawing says the fact is wrong, and settles it.
    """
    parties = parse_parties(evidence_for, required=True)
    row = await _signal_for_edge_change(conn, signal_id, parties)
    edge = signal_evidence_for.c
    removed: list[str] = []
    for party in parties:
        stamp = db_now(conn.dialect.name)
        result = await conn.execute(
            signal_evidence_for.update()
            .where(
                edge.signal_id == row["id"],
                _party_key(edge.for_organization_id) == (party or ""),
                edge.removed_at.is_(None),
            )
            .values(removed_at=stamp, updated_at=stamp)
            .returning(edge.signal_id)
        )
        if result.first() is not None:
            removed.append(_party_wire(party))
    if removed:
        await _touch_signal(conn, signal_id)
    await write_audit(
        conn,
        ctx,
        entity="signal_evidence_for",
        action="remove",
        outcome="ok",
        entity_id=signal_id,
        detail={"parties": removed},
    )
    return await _signal_out(conn, await _signal_row(conn, signal_id)), removed


async def record_proof_point(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    claim: str,
    organization_id: str | None = None,
    engagement: str | None = None,
    metric: str | None = None,
    timeframe: str | None = None,
    verification: str | None = None,
    attested_by: str | None = None,
    scope_notes: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[ProofPoint, str]:
    """Record a citable claim about our own work. Returns ``(ProofPoint, action)``.

    ``verification`` defaults to ``unverified``: a claim starts un-attested and
    becomes evidence only when someone attests it. Recording it as ``verified``
    at create requires an ``attested_by`` — a verified claim with nobody behind it
    is the laundering hole this table exists to close — and stamps ``attested_at``.

    ``fabricated-placeholder`` is a legitimate value, not an error state: copy gets
    workshopped against an outcome we have not yet earned, and the set of these
    records is the punch list of case studies still to obtain. Marking it is what
    keeps it from being read as evidence later.

    The client may be an in-CRM ``organization_id`` or a free-text ``engagement``
    label. Neither is required: past work predates the CRM, and absence of a
    record is not absence of the fact. An optional ``idempotency_key`` makes a
    retried create converge to one row (partial unique ``uq_proof_point_idem``);
    without one every call creates a new proof point.
    """
    _check_len("claim", claim, _CAP_CLAIM)
    _check_len("engagement", engagement, _CAP_NAME)
    _check_len("metric", metric, _CAP_SHORT)
    _check_len("timeframe", timeframe, _CAP_SHORT)
    _check_len("attested_by", attested_by, _CAP_NAME)
    _check_len("scope_notes", scope_notes, _CAP_SCOPE_NOTES)
    _check_len("idempotency_key", idempotency_key, _CAP_SHORT)
    clean_claim = _clean(claim)
    if clean_claim is None:
        raise ValueError("claim must not be empty")
    clean_attester = _clean(attested_by)
    state = (
        validate_choice("verification", verification, VERIFICATION_STATES)
        if verification is not None
        else None
    )
    if state == VERIFIED and clean_attester is None:
        raise ValueError("attested_by is required to record a proof point as verified")
    if organization_id is not None:
        await _org_row(conn, organization_id)  # validate the client in-scope; 404 otherwise

    now = _now()
    key = _clean(idempotency_key)
    insert_values: dict[str, Any] = {
        "id": _new_proof_point_id(),
        **ctx.identity_columns(),
        "organization_id": organization_id,
        "engagement": _clean(engagement),
        "claim": clean_claim,
        "metric": _clean(metric),
        "timeframe": _clean(timeframe),
        "attested_by": clean_attester,
        # An attestation is stamped only when someone actually made one. A
        # `verified` create names its attester (checked above); an unverified one
        # naming a person is that person recording the claim, which is the same
        # act at a lower grade.
        "attested_at": now if clean_attester is not None else None,
        "scope_notes": _clean(scope_notes),
        "idempotency_key": key,
        "created_at": now,
        "updated_at": now,
    }
    if state is not None:
        insert_values["verification"] = state

    if key is not None:
        row, created = await _upsert_returning(
            conn,
            table=proof_point,
            insert_values=insert_values,
            conflict_index_elements=[
                proof_point.c.tenant_id,
                proof_point.c.workspace_id,
                proof_point.c.idempotency_key,
            ],
            conflict_where=proof_point.c.idempotency_key.isnot(None),
            reselect_predicate=and_(
                proof_point.c.tenant_id == ctx.tenant_id,
                proof_point.c.workspace_id == ctx.workspace_id,
                proof_point.c.idempotency_key == key,
            ),
        )
        action = "created" if created else "matched"
        row_id = row["id"]
    else:
        await conn.execute(proof_point.insert().values(**insert_values))
        row = await _proof_point_row(conn, insert_values["id"])
        action = "created"
        row_id = row["id"]

    await write_audit(
        conn, ctx, entity="proof_point", action="record", outcome="ok", entity_id=row_id
    )
    return ProofPoint.from_row(row), action


async def verify_proof_point(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    proof_point_id: str,
    verification: str,
    attested_by: str,
    scope_notes: str | None = None,
) -> ProofPoint:
    """Attest a proof point — the transition verb that owns ``attested_by`` / ``attested_at``.

    Every move through this verb names a person, in all three directions: marking
    a claim ``verified``, downgrading one back to ``unverified``, and declaring one
    a ``fabricated-placeholder`` are each a judgment somebody made, and the audit
    value is in knowing who. ``attested_at`` is stamped with each call, so it reads
    as "when the CURRENT verification was set", not "when it was first touched".

    ``scope_notes`` (naming permissions) is updated when given and left alone when
    omitted — the usual partial-update semantics. 404 if out of scope.
    """
    _check_len("attested_by", attested_by, _CAP_NAME)
    _check_len("scope_notes", scope_notes, _CAP_SCOPE_NOTES)
    state = validate_choice("verification", verification, VERIFICATION_STATES)
    clean_attester = _clean(attested_by)
    if clean_attester is None:
        raise ValueError("attested_by must not be empty — an attestation names who made it")
    await _proof_point_row(conn, proof_point_id)  # 404 if out of scope
    now = _now()
    values: dict[str, Any] = {
        "verification": state,
        "attested_by": clean_attester,
        "attested_at": now,
        "updated_at": now,
    }
    clean_notes = _clean(scope_notes)
    if clean_notes is not None:
        values["scope_notes"] = clean_notes
    await conn.execute(
        proof_point.update().where(proof_point.c.id == proof_point_id).values(**values)
    )
    row = await _proof_point_row(conn, proof_point_id)
    await write_audit(
        conn, ctx, entity="proof_point", action="verify", outcome="ok", entity_id=proof_point_id
    )
    return ProofPoint.from_row(row)


async def get_proof_point(
    conn: AsyncConnection, ctx: Principal, *, proof_point_id: str
) -> ProofPoint:
    """Fetch one proof point. 404 (not 403) if out of scope."""
    row = await _proof_point_row(conn, proof_point_id)
    _log_access(ctx, entity="proof_point", action="get", entity_id=proof_point_id)
    return ProofPoint.from_row(row)


async def list_proof_points(
    conn: AsyncConnection,
    ctx: Principal,
    *,
    organization_id: str | None = None,
    verification: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[ProofPoint], str | None]:
    """List proof points, newest first, with opaque keyset pagination.

    **No default filter**, deliberately — the mirror image of
    :func:`list_signals`. An unverified or placeholder proof point is not a hidden
    row to be protected from; it is the thing the reader most needs to see, and
    filtering to ``verification="fabricated-placeholder"`` is the punch-list read.

    Returns ``(proof_points, next_cursor)``.
    """
    limit = _clamp_limit(limit)
    stmt = select(proof_point).where(*_scope(proof_point, ctx))
    if organization_id is not None:
        stmt = stmt.where(proof_point.c.organization_id == organization_id)
    if verification is not None:
        stmt = stmt.where(proof_point.c.verification == verification)
    stmt = _PROOF_POINTS.apply(stmt, cursor)
    rows = await _all(conn, stmt.limit(limit + 1))
    page, next_cursor = _PROOF_POINTS.paginate(rows, limit)
    _log_access(ctx, entity="proof_point", action="list")
    return [ProofPoint.from_row(r) for r in page], next_cursor
