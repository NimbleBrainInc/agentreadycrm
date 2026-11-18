"""the baseline: every table and index the People tables declare, on SQLite

Revision ID: 0001
Revises:
Create Date: 2026-11-16

The local runtime's schema starts here, at the shape of ``mcp_people.db.tables.metadata`` when
the local runtime first shipped: thirteen tables, the dedupe and lookup indexes (each partial
one with its predicate), and the listing, worklist and timeline indexes. It differs from the
tables' Postgres form by what SQLite has no use for or cannot hold:

- no row-level security: a local database serves one principal;
- no GIN indexes on ``fields``: SQLite reads a custom field with ``json_extract``;
- no ``INCLUDE`` columns on the listing indexes, which SQLite does not support.

A revision here imports no application code, so it never moves with the package: types are
the plain SQLAlchemy ones the tables' SQLite variants compile to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What ``func.now()`` compiles to on SQLite. The store stamps every timestamp itself; a
#: default only fills a row written outside it.
NOW = sa.text("CURRENT_TIMESTAMP")

#: Dropped in reverse order of creation, so no foreign key is left pointing at a dropped table.
TABLES = (
    "field_def",
    "organization",
    "people_audit",
    "pipeline_stage",
    "signal_type",
    "contact",
    "organization_edge",
    "proof_point",
    "external_ref",
    "opportunity",
    "signal",
    "interaction",
    "signal_evidence_for",
)


def upgrade() -> None:
    op.create_table(
        "field_def",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("defined_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_entity", sa.Text(), nullable=True),
        sa.Column("replaced_by_key", sa.Text(), nullable=True),
        sa.Column("default_column", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("minimum", sa.Float(), nullable=True),
        sa.Column("maximum", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_field_def_key", "field_def", ["tenant_id", "workspace_id", "entity", "key"], unique=True
    )
    op.create_table(
        "organization",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("domain_key", sa.Text(), nullable=True),
        sa.Column("name_key", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("employee_range", sa.Text(), nullable=True),
        sa.Column("revenue_range", sa.Text(), nullable=True),
        sa.Column("headquarters", sa.Text(), nullable=True),
        sa.Column("dossier", sa.Text(), nullable=True),
        sa.Column("dossier_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("fields", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_org_domain_key",
        "organization",
        ["tenant_id", "domain_key", "workspace_id"],
        sqlite_where=sa.text("domain_key IS NOT NULL"),
    )
    op.create_index("ix_org_name_key", "organization", ["tenant_id", "name_key", "workspace_id"])
    op.create_index("ix_org_status", "organization", ["tenant_id", "workspace_id", "status"])
    op.create_index(
        "ix_organization_listing", "organization", ["tenant_id", "workspace_id", "created_at", "id"]
    )
    op.create_index(
        "uq_org_domain",
        "organization",
        ["tenant_id", "workspace_id", "domain_key"],
        unique=True,
        sqlite_where=sa.text("domain IS NOT NULL"),
    )
    op.create_index(
        "uq_org_name_when_domainless",
        "organization",
        ["tenant_id", "workspace_id", "name_key"],
        unique=True,
        sqlite_where=sa.text("domain IS NULL"),
    )
    op.create_table(
        "people_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pipeline_stage",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_pipeline_stage_key", "pipeline_stage", ["tenant_id", "workspace_id", "key"], unique=True
    )
    op.create_table(
        "signal_type",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_signal_type_key", "signal_type", ["tenant_id", "workspace_id", "key"], unique=True
    )
    op.create_table(
        "contact",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("email_key", sa.Text(), nullable=True),
        sa.Column("name_key", sa.Text(), nullable=True),
        sa.Column("email_status", sa.Text(), nullable=True),
        sa.Column("email_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("dossier", sa.Text(), nullable=True),
        sa.Column("dossier_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lead_source", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="sourced", nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("fields", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_contact_email_key",
        "contact",
        ["tenant_id", "email_key", "workspace_id"],
        sqlite_where=sa.text("email_key IS NOT NULL"),
    )
    op.create_index(
        "ix_contact_listing", "contact", ["tenant_id", "workspace_id", "created_at", "id"]
    )
    op.create_index("ix_contact_name_key", "contact", ["tenant_id", "name_key", "workspace_id"])
    op.create_index(
        "ix_contact_organization_id", "contact", ["tenant_id", "workspace_id", "organization_id"]
    )
    op.create_index(
        "ix_contact_worklist",
        "contact",
        ["tenant_id", "workspace_id", "status", "created_at", "id"],
    )
    op.create_index(
        "uq_contact_email",
        "contact",
        ["tenant_id", "workspace_id", "email_key"],
        unique=True,
        sqlite_where=sa.text("email IS NOT NULL"),
    )
    op.create_table(
        "organization_edge",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("from_organization_id", sa.Text(), nullable=False),
        sa.Column("to_organization_id", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["from_organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_organization_id"],
            ["organization.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organization_edge_from",
        "organization_edge",
        ["tenant_id", "workspace_id", "from_organization_id", "relation"],
    )
    op.create_index(
        "uq_organization_edge",
        "organization_edge",
        ["tenant_id", "workspace_id", "from_organization_id", "to_organization_id", "relation"],
        unique=True,
    )
    op.create_table(
        "proof_point",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("engagement", sa.Text(), nullable=True),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=True),
        sa.Column("timeframe", sa.Text(), nullable=True),
        sa.Column("verification", sa.Text(), server_default="unverified", nullable=False),
        sa.Column("attested_by", sa.Text(), nullable=True),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope_notes", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proof_point_org", "proof_point", ["tenant_id", "workspace_id", "organization_id"]
    )
    op.create_index(
        "ix_proof_point_verification", "proof_point", ["tenant_id", "workspace_id", "verification"]
    )
    op.create_index(
        "uq_proof_point_idem",
        "proof_point",
        ["tenant_id", "workspace_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "external_ref",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("contact_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "(organization_id IS NULL) <> (contact_id IS NULL)", name="ck_external_ref_one_owner"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_ref_contact", "external_ref", ["contact_id"])
    op.create_index("ix_external_ref_org", "external_ref", ["organization_id"])
    op.create_index(
        "uq_external_ref_uri", "external_ref", ["tenant_id", "workspace_id", "uri"], unique=True
    )
    op.create_table(
        "opportunity",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("primary_contact_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), server_default="lead", nullable=False),
        sa.Column("value", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("probability", sa.Integer(), nullable=True),
        sa.Column("close_date", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dossier", sa.Text(), nullable=True),
        sa.Column("dossier_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("fields", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(
            ["primary_contact_id"],
            ["contact.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_opportunity_contact", "opportunity", ["tenant_id", "workspace_id", "primary_contact_id"]
    )
    op.create_index(
        "ix_opportunity_org",
        "opportunity",
        ["tenant_id", "workspace_id", "organization_id", "stage"],
    )
    op.create_index(
        "uq_opportunity_idem",
        "opportunity",
        ["tenant_id", "workspace_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "signal",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text(), nullable=True),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("evidence_quote", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_date", sa.Date(), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("found_by", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_signal_changes", "signal", ["tenant_id", "workspace_id", "updated_at", "id"]
    )
    op.create_index("ix_signal_contact", "signal", ["tenant_id", "workspace_id", "contact_id"])
    op.create_index(
        "ix_signal_org", "signal", ["tenant_id", "workspace_id", "organization_id", "status"]
    )
    op.create_index(
        "uq_signal_source",
        "signal",
        ["tenant_id", "workspace_id", "organization_id", "source_url", "signal_type"],
        unique=True,
    )
    op.create_table(
        "interaction",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("opportunity_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunity.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interaction_contact_id_occurred_at",
        "interaction",
        ["tenant_id", "workspace_id", "contact_id", "occurred_at"],
    )
    op.create_index(
        "ix_interaction_opportunity", "interaction", ["tenant_id", "workspace_id", "opportunity_id"]
    )
    op.create_index(
        "ix_interaction_organization_id_occurred_at",
        "interaction",
        ["tenant_id", "workspace_id", "organization_id", "occurred_at"],
    )
    op.create_index(
        "ix_interaction_touch",
        "interaction",
        ["tenant_id", "workspace_id", "contact_id", "direction", "kind"],
    )
    op.create_index(
        "uq_interaction_idem",
        "interaction",
        ["tenant_id", "workspace_id", "contact_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "signal_evidence_for",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("signal_id", sa.Text(), nullable=False),
        sa.Column("for_organization_id", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signal.id"],
        ),
        sa.ForeignKeyConstraint(
            ["for_organization_id"],
            ["organization.id"],
        ),
    )
    op.create_index(
        "uq_signal_evidence_for",
        "signal_evidence_for",
        ["tenant_id", "workspace_id", "signal_id", sa.text("coalesce(for_organization_id, '')")],
        unique=True,
    )
    op.create_index(
        "ix_signal_evidence_for_party",
        "signal_evidence_for",
        ["tenant_id", "workspace_id", "for_organization_id", "signal_id"],
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
