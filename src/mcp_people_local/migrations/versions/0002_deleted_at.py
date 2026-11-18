"""deleted_at: a deleted record is marked, and its keys free again

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16

Seven tables gain a
nullable ``deleted_at``, which ``delete_contact`` and ``delete_organization`` set and every
read filters on, and each unique index on those tables gains ``deleted_at IS NULL`` in its
predicate, so a deleted record holds no key against a new one. NULL is a live row, so the data
a file holds stays as it is.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "contact",
    "organization",
    "opportunity",
    "interaction",
    "signal",
    "proof_point",
    "external_ref",
)

#: Each unique index: its table, its columns, and its predicate before this revision.
INDEXES = {
    "uq_contact_email": (
        "contact",
        ["tenant_id", "workspace_id", "email_key"],
        "email IS NOT NULL",
    ),
    "uq_org_domain": (
        "organization",
        ["tenant_id", "workspace_id", "domain_key"],
        "domain IS NOT NULL",
    ),
    "uq_org_name_when_domainless": (
        "organization",
        ["tenant_id", "workspace_id", "name_key"],
        "domain IS NULL",
    ),
    "uq_interaction_idem": (
        "interaction",
        ["tenant_id", "workspace_id", "contact_id", "idempotency_key"],
        "idempotency_key IS NOT NULL",
    ),
    "uq_opportunity_idem": (
        "opportunity",
        ["tenant_id", "workspace_id", "idempotency_key"],
        "idempotency_key IS NOT NULL",
    ),
    "uq_proof_point_idem": (
        "proof_point",
        ["tenant_id", "workspace_id", "idempotency_key"],
        "idempotency_key IS NOT NULL",
    ),
    "uq_external_ref_uri": ("external_ref", ["tenant_id", "workspace_id", "uri"], None),
    "uq_signal_source": (
        "signal",
        ["tenant_id", "workspace_id", "organization_id", "source_url", "signal_type"],
        None,
    ),
}


def _create(name: str, *, live_only: bool) -> None:
    table, columns, before = INDEXES[name]
    predicates = [p for p in (before, "deleted_at IS NULL" if live_only else None) if p]
    op.drop_index(name, table_name=table)
    if predicates:
        op.create_index(
            name, table, columns, unique=True, sqlite_where=sa.text(" AND ".join(predicates))
        )
    else:
        op.create_index(name, table, columns, unique=True)


def upgrade() -> None:
    # SQLite adds a nullable column in place; no table is rebuilt.
    for table in TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    for name in INDEXES:
        _create(name, live_only=True)


def downgrade() -> None:
    for name in INDEXES:
        _create(name, live_only=False)
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("deleted_at")
