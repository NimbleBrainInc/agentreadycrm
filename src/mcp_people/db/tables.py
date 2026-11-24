"""SQLAlchemy Core table definitions for the People relationship graph.

Four design notes that are load-bearing:

  * **Open strings, never enums.** ``status`` / ``lead_source`` / ``kind`` /
    ``industry`` / ``source`` are plain ``Text`` with NO ``CHECK`` constraint or
    DB enum. The agents and tools that write them invent states and sources we
    can't enumerate today; a closed enum would force a migration every time a
    writer evolves — coupling them to this schema. A ``CHECK``-constrained
    ``status`` is a bespoke-creep finding; reject it.
  * **Real edges, no name-denormalization.** Postgres FKs are authoritative
    from creation: ``contact.organization_id`` (nullable), ``interaction.
    contact_id`` (required), ``interaction.organization_id`` (nullable, copied
    from the contact at log time). We denormalize immutable ids, never mutable
    display fields.
  * **Row-level security is not defined here.** These definitions are the shape.
    Every row is tagged ``(tenant_id, workspace_id)``; a deployment on Postgres with
    more than one principal enables and forces row-level security on that pair in its
    own migrations, and connects as a role without BYPASSRLS, so the policy is the
    actual boundary. A SQLite database serves one principal and needs no policy.
  * **One shape on both dialects.** Timestamps are :class:`UTCDateTime`, JSON is JSONB on
    Postgres, the audit id is an ``INTEGER`` key on SQLite, and each partial index carries
    its predicate for both dialects (:mod:`mcp_people.db.dialect`). The GIN indexes exist on
    Postgres only.

Ids are opaque, prefixed ulids (``org_``/``ct_``/``ix_``) minted in the store
layer — never DB sequences or server defaults. Idempotency rests on the partial
unique dedupe indexes below: contacts on ``email_key``, orgs on ``domain_key`` (and
``name_key`` when domain-less), scoped to ``(tenant_id, workspace_id)``. A key column
is written by the store from :mod:`mcp_people.keys` on every write of its source
column, never computed by the database (see ``ix_org_domain_key`` below).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    func,
    text,
)

from mcp_people.db.dialect import JSONType, RowId, UTCDateTime

metadata = MetaData()

organization = Table(
    "organization",
    metadata,
    Column("id", Text, primary_key=True),  # org_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("name", Text, nullable=False),
    Column("domain", Text, nullable=True),  # primary dedupe key (normalized, lowercased)
    # Match keys, written by the store: address_key(domain) and match_key(name)
    # (mcp_people.keys; see ``ix_org_domain_key`` below).
    Column("domain_key", Text, nullable=True),
    Column("name_key", Text, nullable=True),
    Column("website", Text, nullable=True),
    Column("industry", Text, nullable=True),  # open string, NOT an enum
    Column("region", Text, nullable=True),
    # Firmographics — populated from a similar-companies result via
    # link_similar_companies. Open strings, NOT enums.
    Column("employee_range", Text, nullable=True),  # headcount band, e.g. "51-200"
    Column("revenue_range", Text, nullable=True),  # revenue band
    Column("headquarters", Text, nullable=True),  # headquarters location (finer than region)
    # No tool reads or writes the dossier pair; the columns stay so a database that holds
    # dossiers keeps them.
    Column("dossier", Text, nullable=True),
    Column("dossier_updated_at", UTCDateTime(), nullable=True),
    Column("source", Text, nullable=True),  # provenance: research/manual/import/similar_companies
    # Provenance lifecycle: `discovered` (auto-persisted lookalike, hidden from the
    # default list_organizations allowlist) / `active` (real CRM org). Open string,
    # NOT an enum; default `active`, auto-discovered lookalikes are `discovered`
    # until promote_organization.
    Column("status", Text, nullable=False, server_default="active"),
    # Custom field values: {key: {"v": value, "by", "at", "why"}}.
    Column("fields", JSONType, nullable=False, server_default=text("'{}'")),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
    # Set when the organization is archived: hidden from the default views, reversibly
    # (archive_organization, and the source of a merge). NULL is not archived.
    Column("archived_at", UTCDateTime(), nullable=True),
)

contact = Table(
    "contact",
    metadata,
    Column("id", Text, primary_key=True),  # ct_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("name", Text, nullable=False),
    Column("email", Text, nullable=True),  # primary dedupe key (normalized lowercase)
    # Match keys, written by the store: address_key(email) and match_key(name)
    # (mcp_people.keys; see ``ix_org_domain_key`` below).
    Column("email_key", Text, nullable=True),
    Column("name_key", Text, nullable=True),
    # Whether `email` can receive mail: valid / invalid / risky / unknown, validated
    # app-side (`fields.EMAIL_STATUSES`). NULL until someone checks it. It grades the
    # address, so a write that changes `email` clears both columns.
    Column("email_status", Text, nullable=True),
    Column("email_checked_at", UTCDateTime(), nullable=True),
    Column("phone", Text, nullable=True),
    Column("title", Text, nullable=True),  # role at the org
    # works_at edge → organization. Nullable; FK is authoritative from creation.
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=True),
    # No tool reads or writes the dossier pair; the columns stay so a database that holds
    # dossiers keeps them.
    Column("dossier", Text, nullable=True),
    Column("dossier_updated_at", UTCDateTime(), nullable=True),
    Column("lead_source", Text, nullable=True),  # open string, NOT an enum
    # Lifecycle an outreach agent reads/writes. Open string, NOT an enum;
    # People stores it, does not drive transitions. Default `sourced` — the
    # first stage (identified, not yet worked); a lead sits here until a writer
    # advances it.
    Column("status", Text, nullable=False, server_default="sourced"),
    Column("source", Text, nullable=True),  # provenance
    # Custom field values, same shape as organization.fields.
    Column("fields", JSONType, nullable=False, server_default=text("'{}'")),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
    # Set when the contact is archived: hidden from the default views, reversibly
    # (archive_contact, and the source of a merge). NULL is not archived. Separate from
    # `status`, which holds only the stage.
    Column("archived_at", UTCDateTime(), nullable=True),
)

interaction = Table(
    "interaction",
    metadata,
    Column("id", Text, primary_key=True),  # ix_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # FK → contact, required. The interaction always hangs off a contact.
    Column("contact_id", Text, ForeignKey("contact.id"), nullable=False),
    # Denormalized immutable edge: copied from the contact at log time and FROZEN
    # there. Nullable because the contact may have no org. This does NOT follow a
    # contact who changes employers — the interaction stays attributed to the org
    # the contact was at when it happened; this is intentional historical
    # attribution (CONFIRMED). Never a denormalized display name.
    #
    # One exception, and only one: a merge (``merge_organizations``, and the
    # equivalent collapse in migration 0009) repoints this at the surviving org.
    # The row it names is being archived or deleted, so leaving it frozen would
    # freeze a pointer to something unreadable rather than preserve a fact.
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=True),
    # The opportunity this touch advances (nullable, mostly NULL). A peer FK like
    # organization_id above — Opportunity is a first-class member of the graph, not
    # a foreign module. Validated in-scope on write. (Forward-referenced by name;
    # the opportunity table is defined below.)
    Column("opportunity_id", Text, ForeignKey("opportunity.id"), nullable=True),
    Column("kind", Text, nullable=False),  # open string, validated with graceful fallthrough
    # inbound / outbound — open string, NOT an enum. Nullable: existing rows and
    # direction-agnostic kinds (note/research) carry none.
    Column("direction", Text, nullable=True),
    Column("summary", Text, nullable=False),
    Column("body", Text, nullable=True),  # markdown, longer detail
    Column("occurred_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("follow_up_at", UTCDateTime(), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Optional retry-dedupe token. Internal (never on the wire model): when a
    # batch step is retried with the same key, the partial unique index below
    # collapses the second insert to the existing row. NULL = a plain append.
    Column("idempotency_key", Text, nullable=True),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
)

# Append-only access log: one row per access. RLS-policied like
# the rest. No updated_at — rows are written once, never mutated.
people_audit = Table(
    "people_audit",
    metadata,
    Column("id", RowId, primary_key=True, autoincrement=True),
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("user_id", Text, nullable=True),
    Column("entity", String(32), nullable=False),  # organization | contact | interaction
    Column("entity_id", Text, nullable=True),
    Column("action", String(32), nullable=False),
    Column("outcome", String(16), nullable=False),  # ok | denied | error
    Column("correlation_id", Text, nullable=True),
    # Optional structured context for the change record (e.g. what a merge moved
    # where). Holds ONLY ids, counts, and field NAMES — never PII/values — so the
    # durable log stays attributable without becoming a second copy of the data.
    # The exception is a value the write DESTROYS (merge_organizations records the
    # source domain it clears): that is not a second copy, it is the only one.
    # See write_audit's docstring for the rule.
    Column("detail", JSONType, nullable=True),
    Column("at", UTCDateTime(), nullable=False, server_default=func.now()),
)

# Self-referential org↔org graph. ONE generic edge table, so a future
# `competitors` capability is a new ``relation`` value, not a new migration. Only
# ``similar_to`` is populated. Carries (tenant_id,
# workspace_id) like every table, for its own row-level policy on Postgres — RLS is
# per-row and does NOT follow the FKs. `relation` / `source` are open strings, NOT enums.
organization_edge = Table(
    "organization_edge",
    metadata,
    Column("id", Text, primary_key=True),  # oe_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # Both endpoints FK → organization; RLS does not follow the FKs, so the edge
    # carries its own (tenant_id, workspace_id) and is validated in-scope on write.
    Column("from_organization_id", Text, ForeignKey("organization.id"), nullable=False),  # seed
    Column("to_organization_id", Text, ForeignKey("organization.id"), nullable=False),  # related
    Column("relation", Text, nullable=False),  # similar_to / competitor_of — open string
    Column("score", Float, nullable=True),  # the discovering provider's score (a similarity)
    Column("source", Text, nullable=True),  # which discovery produced the edge
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a merge drops the edge: it would become a self-edge, or duplicate one the
    # record kept already holds. NULL is a live edge; every read leaves a marked one out,
    # and the row keeps its endpoints, score and source.
    Column("deleted_at", UTCDateTime(), nullable=True),
)

# Opportunity — a first-class pipeline entity in the relationship graph
# . A peer of Organization·Contact·Interaction: real FKs to
# organization/contact, and interactions reference it by FK. `stage`/`source`/
# `currency` are open Text (NO CHECK) so the pipeline vocabulary evolves without a
# migration. The won/lost/open `outcome` is NOT stored — it is the outcome of the
# workspace's `pipeline_stage` row for `stage`, read at query time, the same
# projection discipline as contact.effective_status. RLS is applied in the migration like every table.
opportunity = Table(
    "opportunity",
    metadata,
    Column("id", Text, primary_key=True),  # op_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # The account. Nullable; frozen from the primary contact's org at create when
    # omitted (same immutable-id edge as interaction.organization_id).
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=True),
    Column("primary_contact_id", Text, ForeignKey("contact.id"), nullable=True),
    Column("title", Text, nullable=False),
    # Single pipeline axis: a key into the workspace's `pipeline_stage` rows. Open
    # string, NOT an enum. Default `lead` — the first rung of the default pipeline.
    Column("stage", Text, nullable=False, server_default="lead"),
    Column("value", Numeric(18, 2), nullable=True),  # optional deal size
    Column("currency", Text, nullable=True),  # open string, e.g. USD
    Column("probability", Integer, nullable=True),  # 0-100, optional
    Column("close_date", Date, nullable=True),  # target/actual close date
    # Stamped when the opportunity enters a terminal stage (won/lost), cleared when
    # it leaves one. Owned by set_opportunity_stage — the transition verb.
    Column("closed_at", UTCDateTime(), nullable=True),
    # No tool reads or writes the dossier pair; the columns stay so a database that holds
    # dossiers keeps them.
    Column("dossier", Text, nullable=True),
    Column("dossier_updated_at", UTCDateTime(), nullable=True),
    Column("source", Text, nullable=True),  # provenance: manual / import / the writing tool
    # Custom field values by key, each `{v, by, at, why}`.
    Column("fields", JSONType, nullable=False, server_default=text("'{}'")),
    # Optional retry/import-dedupe token. Internal (never on the wire model): a
    # retried create — or the CRM data migration replaying a legacy deal id — with
    # the same key collapses to the existing row via uq_opportunity_idem below.
    Column("idempotency_key", Text, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
)

# FieldDef — one custom field of one entity in one workspace. The
# values live on the entity row's `fields` JSONB; this row says what a key means:
# its label, its type, the options a select may hold, and a description written
# for the agent that will fill it. `entity` / `type` are open Text validated
# app-side (`fields.ENTITIES` / `fields.FIELD_TYPES`), like the evidence grades.
field_def = Table(
    "field_def",
    metadata,
    Column("id", Text, primary_key=True),  # fd_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("entity", Text, nullable=False),  # contact / organization / opportunity
    Column("key", Text, nullable=False),  # the stable identifier filters and views name
    Column("label", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("options", JSONType, nullable=False, server_default=text("'[]'")),
    Column("description", Text, nullable=True),  # what it means, for the agent filling it
    Column("defined_by", Text, nullable=True),  # provenance: which agent, run or person
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when the field is retired; NULL is live. Its values stay on the rows.
    Column("retired_at", UTCDateTime(), nullable=True),
    # The field that replaces a retired one, possibly on another entity.
    Column("replaced_by_entity", Text, nullable=True),
    Column("replaced_by_key", Text, nullable=True),
    # An admin's choice that this field opens as a column in its lists.
    Column("default_column", Boolean, nullable=False, server_default=text("false")),
    # The inclusive range a number field accepts; NULL is unbounded on that side.
    Column("minimum", Float, nullable=True),
    Column("maximum", Float, nullable=True),
)

# One definition per key per entity per workspace: `define_field` is idempotent on
# it, so two agents defining the same field converge on one row.
uq_field_def_key = Index(
    "uq_field_def_key",
    field_def.c.tenant_id,
    field_def.c.workspace_id,
    field_def.c.entity,
    field_def.c.key,
    unique=True,
)

# The index a filter on a custom field's value rides: a select
# or list match compiles to `fields @> {...}`, which `jsonb_path_ops` answers and
# stores smaller than the default operator class. Postgres only: SQLite has no GIN, and
# its JSON match (`json_each`) reads the row.
ix_contact_fields = Index(
    "ix_contact_fields",
    contact.c.fields,
    postgresql_using="gin",
    postgresql_ops={"fields": "jsonb_path_ops"},
).ddl_if(dialect="postgresql")
ix_organization_fields = Index(
    "ix_organization_fields",
    organization.c.fields,
    postgresql_using="gin",
    postgresql_ops={"fields": "jsonb_path_ops"},
).ddl_if(dialect="postgresql")
ix_opportunity_fields = Index(
    "ix_opportunity_fields",
    opportunity.c.fields,
    postgresql_using="gin",
    postgresql_ops={"fields": "jsonb_path_ops"},
).ddl_if(dialect="postgresql")

# --- Pipeline stages -------------------------------------
#
# The stages a workspace moves its deals through, in order. A deal stores the
# `key`; the label, position, default probability and outcome are the stage's,
# so renaming or reweighting a stage touches one row.
pipeline_stage = Table(
    "pipeline_stage",
    metadata,
    Column("id", Text, primary_key=True),  # ps_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("key", Text, nullable=False),  # what a deal stores in `stage`
    Column("label", Text, nullable=False),
    Column("position", Integer, nullable=False),  # column order on the board
    Column("probability", Integer, nullable=False),  # 0-100, the default for a deal entering
    Column("outcome", Text, nullable=False, server_default="open"),  # open / won / lost
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when the stage is retired; NULL is live. Its deals keep the key, and their label
    # and outcome still read from this row; the key stays taken.
    Column("retired_at", UTCDateTime(), nullable=True),
    # The live stage that takes a retired one's place.
    Column("replaced_by_key", Text, nullable=True),
)

# One stage per key per workspace: `define_pipeline_stage` converges on it.
uq_pipeline_stage_key = Index(
    "uq_pipeline_stage_key",
    pipeline_stage.c.tenant_id,
    pipeline_stage.c.workspace_id,
    pipeline_stage.c.key,
    unique=True,
)

# --- Signal types -------------------------------------
#
# The kinds of fact a workspace records about an account. A signal stores the
# `key`; the label and description are the type's, so renaming a type touches
# one row.
signal_type = Table(
    "signal_type",
    metadata,
    Column("id", Text, primary_key=True),  # st_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("key", Text, nullable=False),  # what a signal stores in `signal_type`
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=True),  # what the type means, for the agent
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when the type is retired; NULL is live. Its signals keep the key and its label
    # still reads; the key stays taken.
    Column("retired_at", UTCDateTime(), nullable=True),
    # The live type that takes a retired one's place.
    Column("replaced_by_key", Text, nullable=True),
)

# Another system's name for an organization or a contact: an opaque URI the
# record is known by elsewhere (`vendor://companies/123`), stored as given and never
# parsed. A row, not a list on the record, because a URI names ONE record: the unique
# index is what makes a reference an identity an import can match on, and a list
# column has no such constraint. Exactly one owner per row.
external_ref = Table(
    "external_ref",
    metadata,
    Column("id", Text, primary_key=True),  # xr_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("uri", Text, nullable=False),
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=True),
    Column("contact_id", Text, ForeignKey("contact.id"), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
    CheckConstraint(
        "(organization_id IS NULL) <> (contact_id IS NULL)", name="ck_external_ref_one_owner"
    ),
)

uq_external_ref_uri = Index(
    "uq_external_ref_uri",
    external_ref.c.tenant_id,
    external_ref.c.workspace_id,
    external_ref.c.uri,
    unique=True,
    postgresql_where=external_ref.c.deleted_at.is_(None),
    sqlite_where=external_ref.c.deleted_at.is_(None),
)
ix_external_ref_org = Index("ix_external_ref_org", external_ref.c.organization_id)
ix_external_ref_contact = Index("ix_external_ref_contact", external_ref.c.contact_id)

# One type per key per workspace: `define_signal_type` converges on it.
uq_signal_type_key = Index(
    "uq_signal_type_key",
    signal_type.c.tenant_id,
    signal_type.c.workspace_id,
    signal_type.c.key,
    unique=True,
)

# --- Idempotency dedupe indexes ---------------------------------
#
# Partial unique indexes on the stored match keys, scoped to (tenant_id,
# workspace_id). These are the correctness backbone of the idempotent upsert: the
# retrying host loop calling find_or_create_* per lead can't double-insert. Partial
# (WHERE ... IS NOT NULL) so multiple rows may carry a NULL email/domain without
# colliding — only populated keys dedupe. They index the key columns, never a
# `lower(col)` expression: whether two rows are one record is decided by the
# application's key function (mcp_people.keys), so every database compares the
# same bytes.

uq_contact_email = Index(
    "uq_contact_email",
    contact.c.tenant_id,
    contact.c.workspace_id,
    contact.c.email_key,
    unique=True,
    postgresql_where=and_(contact.c.email.isnot(None), contact.c.deleted_at.is_(None)),
    sqlite_where=and_(contact.c.email.isnot(None), contact.c.deleted_at.is_(None)),
)

uq_org_domain = Index(
    "uq_org_domain",
    organization.c.tenant_id,
    organization.c.workspace_id,
    organization.c.domain_key,
    unique=True,
    postgresql_where=and_(organization.c.domain.isnot(None), organization.c.deleted_at.is_(None)),
    sqlite_where=and_(organization.c.domain.isnot(None), organization.c.deleted_at.is_(None)),
)

# The name dedupe key, for organizations that have no domain. `domain` is the
# real identity of a company and takes precedence; `name_key` is the fallback
# `find_or_create_organization` falls back to, and this is what makes that
# fallback a database key rather than a read-then-insert two callers can both
# win. Partial on `domain IS NULL` on purpose: two organizations sharing a name
# but holding different domains are different companies (acme.com vs acme.co.uk),
# and a total index would refuse the second. A name is only an identity in the
# absence of the real one.
uq_org_name_when_domainless = Index(
    "uq_org_name_when_domainless",
    organization.c.tenant_id,
    organization.c.workspace_id,
    organization.c.name_key,
    unique=True,
    postgresql_where=and_(organization.c.domain.is_(None), organization.c.deleted_at.is_(None)),
    sqlite_where=and_(organization.c.domain.is_(None), organization.c.deleted_at.is_(None)),
)

# The lookups behind find-or-create. A read compares a stored key column with `=`,
# never `lower(col) = $v`: `lower()` is not leakproof, so under row-level security
# Postgres evaluates it only after the policy quals and cannot use an index on
# `lower(col)`, and each lookup scans the workspace, once per row of an import.
# `=` on text is leakproof, so these plain indexes serve the read under RLS.
# The key leads `workspace_id`: a lookup scoped by RLS alone cannot bound the workspace,
# because the read policy ORs it with the system role, so the probe is account + key.
# The unique indexes above are the dedupe and ON CONFLICT targets.
ix_org_domain_key = Index(
    "ix_org_domain_key",
    organization.c.tenant_id,
    organization.c.domain_key,
    organization.c.workspace_id,
    postgresql_where=organization.c.domain_key.isnot(None),
    sqlite_where=organization.c.domain_key.isnot(None),
)

ix_org_name_key = Index(
    "ix_org_name_key",
    organization.c.tenant_id,
    organization.c.name_key,
    organization.c.workspace_id,
)

ix_contact_email_key = Index(
    "ix_contact_email_key",
    contact.c.tenant_id,
    contact.c.email_key,
    contact.c.workspace_id,
    postgresql_where=contact.c.email_key.isnot(None),
    sqlite_where=contact.c.email_key.isnot(None),
)

ix_contact_name_key = Index(
    "ix_contact_name_key",
    contact.c.tenant_id,
    contact.c.name_key,
    contact.c.workspace_id,
)

# The listings, worklists and timelines. A listing finds its page's ids in the default
# order `(created_at, id)`; on Postgres the listing indexes also carry (INCLUDE) the keys
# and status a search and the default filter read, so that read never touches a wide row.
# SQLite has no INCLUDE and keeps the key columns alone.
ix_organization_listing = Index(
    "ix_organization_listing",
    organization.c.tenant_id,
    organization.c.workspace_id,
    organization.c.created_at,
    organization.c.id,
    postgresql_include=["name_key", "domain_key", "status"],
)
ix_org_status = Index(
    "ix_org_status",
    organization.c.tenant_id,
    organization.c.workspace_id,
    organization.c.status,
)
ix_contact_listing = Index(
    "ix_contact_listing",
    contact.c.tenant_id,
    contact.c.workspace_id,
    contact.c.created_at,
    contact.c.id,
    postgresql_include=["name_key", "email_key", "status", "organization_id"],
)
ix_contact_worklist = Index(
    "ix_contact_worklist",
    contact.c.tenant_id,
    contact.c.workspace_id,
    contact.c.status,
    contact.c.created_at,
    contact.c.id,
)
ix_contact_organization_id = Index(
    "ix_contact_organization_id",
    contact.c.tenant_id,
    contact.c.workspace_id,
    contact.c.organization_id,
)
ix_interaction_contact_id_occurred_at = Index(
    "ix_interaction_contact_id_occurred_at",
    interaction.c.tenant_id,
    interaction.c.workspace_id,
    interaction.c.contact_id,
    interaction.c.occurred_at,
)
ix_interaction_organization_id_occurred_at = Index(
    "ix_interaction_organization_id_occurred_at",
    interaction.c.tenant_id,
    interaction.c.workspace_id,
    interaction.c.organization_id,
    interaction.c.occurred_at,
)
# The has-been-touched predicate (an inbound reply or an outbound touch per contact).
ix_interaction_touch = Index(
    "ix_interaction_touch",
    interaction.c.tenant_id,
    interaction.c.workspace_id,
    interaction.c.contact_id,
    interaction.c.direction,
    interaction.c.kind,
)

# Idempotency dedupe for log_interaction: a retried batch step passing the same
# key collapses to one row under this partial unique index. Scoped PER CONTACT
# (tenant_id/workspace_id lead, then contact_id) so the same key reused across
# DIFFERENT contacts yields distinct rows — a key is only idempotent within the
# contact it was logged against. Partial so NULL keys (the plain-append default)
# never collide.
uq_interaction_idem = Index(
    "uq_interaction_idem",
    interaction.c.tenant_id,
    interaction.c.workspace_id,
    interaction.c.contact_id,
    interaction.c.idempotency_key,
    unique=True,
    postgresql_where=and_(
        interaction.c.idempotency_key.isnot(None), interaction.c.deleted_at.is_(None)
    ),
    sqlite_where=and_(
        interaction.c.idempotency_key.isnot(None), interaction.c.deleted_at.is_(None)
    ),
)

# --- Organization edge indexes ----------------------------------
#
# Unique on (tenant_id, workspace_id, from, to, relation) among live edges, so a
# re-linked lookalike set converges instead of duplicating (the idempotency contract),
# and an edge a merge marked holds no key against a new one. Plus a non-unique
# index for the list-by-seed read (a seed's edges of one relation).
uq_organization_edge = Index(
    "uq_organization_edge",
    organization_edge.c.tenant_id,
    organization_edge.c.workspace_id,
    organization_edge.c.from_organization_id,
    organization_edge.c.to_organization_id,
    organization_edge.c.relation,
    unique=True,
    postgresql_where=organization_edge.c.deleted_at.is_(None),
    sqlite_where=organization_edge.c.deleted_at.is_(None),
)

ix_organization_edge_from = Index(
    "ix_organization_edge_from",
    organization_edge.c.tenant_id,
    organization_edge.c.workspace_id,
    organization_edge.c.from_organization_id,
    organization_edge.c.relation,
)

# --- Opportunity indexes ---------------------
#
# Idempotency dedupe: a retried create — or the CRM data migration replaying a
# legacy deal id as the key — converges to one row. Partial (WHERE key IS NOT NULL)
# so plain creates never collide; mirrors the find_or_create_* dedupe contract.
uq_opportunity_idem = Index(
    "uq_opportunity_idem",
    opportunity.c.tenant_id,
    opportunity.c.workspace_id,
    opportunity.c.idempotency_key,
    unique=True,
    postgresql_where=and_(
        opportunity.c.idempotency_key.isnot(None), opportunity.c.deleted_at.is_(None)
    ),
    sqlite_where=and_(
        opportunity.c.idempotency_key.isnot(None), opportunity.c.deleted_at.is_(None)
    ),
)

# Pipeline-by-account read (an org's opportunities, by stage). Leads with the RLS
# keys so the policy predicate rides the same index.
ix_opportunity_org = Index(
    "ix_opportunity_org",
    opportunity.c.tenant_id,
    opportunity.c.workspace_id,
    opportunity.c.organization_id,
    opportunity.c.stage,
)

# Contact → opportunities read.
ix_opportunity_contact = Index(
    "ix_opportunity_contact",
    opportunity.c.tenant_id,
    opportunity.c.workspace_id,
    opportunity.c.primary_contact_id,
)

# Interaction → opportunity read (an opportunity's timeline).
ix_interaction_opportunity = Index(
    "ix_interaction_opportunity",
    interaction.c.tenant_id,
    interaction.c.workspace_id,
    interaction.c.opportunity_id,
)

# Signal — a dated, URL-attested fact about an account: a thing someone else published
# that we can quote and a reviewer can open. `signal_type` / `status` stay open Text (no
# CHECK, no DB enum, same as every other column here) and are validated app-side:
# `signal_type` against the workspace's `signal_type` rows, `status` against
# `evidence.SIGNAL_STATUSES`. A signal carries no grade: judging a fact against
# what it is used for belongs to the tool that uses it.
signal = Table(
    "signal",
    metadata,
    Column("id", Text, primary_key=True),  # sg_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # The account the fact is about. REQUIRED — a signal with no org is a note.
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=False),
    # The person it concerns, when it is about one (an executive hire, a quote).
    # Nullable: most signals are about the company, not a named individual.
    Column("contact_id", Text, ForeignKey("contact.id"), nullable=True),
    # One plain-English sentence, readable WITHOUT opening the source. This is
    # what a reader takes from the signal; if it only makes sense beside the article, it is
    # not a claim yet.
    Column("claim", Text, nullable=False),
    # The verbatim words from the source that support the claim. Required: the
    # quote is what makes the row evidence rather than an assertion with a link
    # stapled to it.
    Column("evidence_quote", Text, nullable=False),
    # Where it was read. Shape-validated and normalized on write; half of the
    # dedupe key below.
    Column("source_url", Text, nullable=False),
    # When the SOURCE is dated (not when we found it — that is created_at).
    # Nullable: plenty of pages carry no date, and inventing one would be the
    # exact failure this table exists to prevent.
    Column("source_date", Date, nullable=True),
    # When the EVENT happened, as the evidence states it — not when the source was
    # published (source_date). A month-only statement is the 1st of that month.
    # Nullable for the same reason: a source that states no event date gets none,
    # and nothing derives one from source_date.
    Column("event_date", Date, nullable=True),
    Column("signal_type", Text, nullable=False),  # a key of the workspace's `signal_type`
    Column("found_by", Text, nullable=True),  # provenance: which run/agent/human
    # active / withdrawn. There is no delete: a signal that turns out to be wrong
    # is withdrawn, so the record of having believed it survives.
    Column("status", Text, nullable=False, server_default="active"),
    Column("withdrawn_at", UTCDateTime(), nullable=True),
    Column("withdrawn_reason", Text, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
)

# ProofPoint — an attested claim about OUR OWN work. Same
# column convention as `signal`, different attestor and lifecycle: a signal is
# attested by a URL anyone can open, a proof point by a person who says they
# stand behind it. That is why they are two tables and not one `claim` table with
# a discriminator — one shared shape would have to make `source_url` nullable and
# `attested_by` nullable, and the required-ness IS the contract.
proof_point = Table(
    "proof_point",
    metadata,
    Column("id", Text, primary_key=True),  # pp_<ulid>, minted in the store layer
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    # The client the work was for, when they are in the CRM. NULLABLE by
    # decision: past work predates the CRM, and absence of a record is not
    # absence of the fact — `engagement` carries the label in that case.
    Column("organization_id", Text, ForeignKey("organization.id"), nullable=True),
    Column("engagement", Text, nullable=True),  # free-text client/engagement label
    Column("claim", Text, nullable=False),  # the citable sentence
    Column("metric", Text, nullable=True),  # the number, as it would be cited
    Column("timeframe", Text, nullable=True),  # over what period it was achieved
    # verified / unverified / fabricated-placeholder. Default `unverified`: a
    # claim starts un-attested and becomes evidence only when someone attests it.
    Column("verification", Text, nullable=False, server_default="unverified"),
    Column("attested_by", Text, nullable=True),  # who stood behind it
    Column("attested_at", UTCDateTime(), nullable=True),  # and when
    # Naming permissions: whether the client may be named, under what NDA, which
    # figures may ship. A verified number that cannot be attributed is still not
    # citable, so the permission travels with the claim.
    Column("scope_notes", Text, nullable=True),
    # Optional retry/import-dedupe token, same contract as `opportunity`.
    Column("idempotency_key", Text, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
    # Set when a delete marks this row. NULL is a live row; every read leaves
    # a marked one out and treats a link to it as absent. The row and its values stay.
    Column("deleted_at", UTCDateTime(), nullable=True),
)

# --- Evidence indexes ---------------------------
#
# Idempotency: the same fact, from the same page, typed the same way, is ONE
# signal — re-running research over an account converges instead of stacking
# near-duplicates. All three key columns are NOT NULL, so this is a plain
# (non-partial) unique index, and `source_url` is normalized on write so two
# spellings of one URL collide as intended.
uq_signal_source = Index(
    "uq_signal_source",
    signal.c.tenant_id,
    signal.c.workspace_id,
    signal.c.organization_id,
    signal.c.source_url,
    signal.c.signal_type,
    unique=True,
    postgresql_where=signal.c.deleted_at.is_(None),
    sqlite_where=signal.c.deleted_at.is_(None),
)

# The account read: this account's active signals, before citing or re-researching.
ix_signal_org = Index(
    "ix_signal_org",
    signal.c.tenant_id,
    signal.c.workspace_id,
    signal.c.organization_id,
    signal.c.status,
)

# Contact-scoped signals (an executive-level fact about one person).
ix_signal_contact = Index(
    "ix_signal_contact",
    signal.c.tenant_id,
    signal.c.workspace_id,
    signal.c.contact_id,
)

uq_proof_point_idem = Index(
    "uq_proof_point_idem",
    proof_point.c.tenant_id,
    proof_point.c.workspace_id,
    proof_point.c.idempotency_key,
    unique=True,
    postgresql_where=and_(
        proof_point.c.idempotency_key.isnot(None), proof_point.c.deleted_at.is_(None)
    ),
    sqlite_where=and_(
        proof_point.c.idempotency_key.isnot(None), proof_point.c.deleted_at.is_(None)
    ),
)

# An account's proof points.
ix_proof_point_org = Index(
    "ix_proof_point_org",
    proof_point.c.tenant_id,
    proof_point.c.workspace_id,
    proof_point.c.organization_id,
)

# The punch-list read — every `fabricated-placeholder` in the workspace, i.e. the
# case studies the business still has to obtain. Its own index because that read
# carries no organization filter, so it cannot ride the org index above.
ix_proof_point_verification = Index(
    "ix_proof_point_verification",
    proof_point.c.tenant_id,
    proof_point.c.workspace_id,
    proof_point.c.verification,
)

# The change read: a reader resumes the workspace's signals in
# `(updated_at, id)` order from its mark, so the order is an index range.
ix_signal_changes = Index(
    "ix_signal_changes",
    signal.c.tenant_id,
    signal.c.workspace_id,
    signal.c.updated_at,
    signal.c.id,
)

# Whom a signal is evidence for. A signal is a fact about its
# organization; why it was recorded is a separate fact, and one fact can serve several
# purposes, so each purpose is a row. `for_organization_id` NULL is the workspace's own
# pipeline; otherwise it is the organization the workspace researched the signal for.
# A removed edge keeps its row with `removed_at` set, so a reader of changes filtered to
# that party still sees the removal; every read of current edges filters it out.
signal_evidence_for = Table(
    "signal_evidence_for",
    metadata,
    Column("tenant_id", Text, nullable=False),  # RLS key
    Column("workspace_id", Text, nullable=False),  # RLS key
    Column("signal_id", Text, ForeignKey("signal.id"), nullable=False),
    Column("for_organization_id", Text, ForeignKey("organization.id"), nullable=True),
    Column("added_by", Text, nullable=True),  # provenance, like signal.found_by
    Column("removed_at", UTCDateTime(), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime(), nullable=False, server_default=func.now()),
)

# One edge per (signal, party). NULL is a party (the workspace), so the key folds it to
# '' — no organization id is empty.
uq_signal_evidence_for = Index(
    "uq_signal_evidence_for",
    signal_evidence_for.c.tenant_id,
    signal_evidence_for.c.workspace_id,
    signal_evidence_for.c.signal_id,
    func.coalesce(signal_evidence_for.c.for_organization_id, text("''")),
    unique=True,
)

# The listing filtered to one party: that party's signals.
ix_signal_evidence_for_party = Index(
    "ix_signal_evidence_for_party",
    signal_evidence_for.c.tenant_id,
    signal_evidence_for.c.workspace_id,
    signal_evidence_for.c.for_organization_id,
    signal_evidence_for.c.signal_id,
)
