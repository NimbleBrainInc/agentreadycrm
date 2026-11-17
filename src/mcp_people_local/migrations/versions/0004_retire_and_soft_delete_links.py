"""retire stages and signal types; a merge marks the edges it drops

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16

``pipeline_stage`` and ``signal_type`` gain a nullable ``retired_at`` and ``replaced_by_key``, and
``organization_edge`` a nullable ``deleted_at``, with ``uq_organization_edge`` rebuilt to
leave marked edges out. NULL is live, so the data a file holds stays as it is.

The downgrade restores every retired stage and type, removes the marked edges (which the
previous release removed at the merge), restores the full index and drops the columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VOCABULARIES = ("pipeline_stage", "signal_type")

EDGE_KEY = ["tenant_id", "workspace_id", "from_organization_id", "to_organization_id", "relation"]


def upgrade() -> None:
    # SQLite adds a nullable column in place; no table is rebuilt.
    for table in VOCABULARIES:
        op.add_column(table, sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("replaced_by_key", sa.Text(), nullable=True))
    op.add_column(
        "organization_edge", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_index("uq_organization_edge", table_name="organization_edge")
    op.create_index(
        "uq_organization_edge",
        "organization_edge",
        EDGE_KEY,
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM organization_edge WHERE deleted_at IS NOT NULL")
    op.drop_index("uq_organization_edge", table_name="organization_edge")
    op.create_index("uq_organization_edge", "organization_edge", EDGE_KEY, unique=True)
    with op.batch_alter_table("organization_edge") as batch:
        batch.drop_column("deleted_at")
    for table in reversed(VOCABULARIES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("replaced_by_key")
            batch.drop_column("retired_at")
