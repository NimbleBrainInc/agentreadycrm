"""archived_at: archiving is its own flag, and a contact's status holds only its stage

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16

``contact`` and ``organization`` gain a nullable ``archived_at``, and every ``status = 'archived'`` row moves
onto it, stamped with its ``updated_at``; a contact takes the stage ``sourced`` and an
organization ``active``. The downgrade writes ``archived`` back into ``status`` and drops the
columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each table, and the status an archived row takes when the flag replaces the value.
TABLES = {"contact": "sourced", "organization": "active"}


def upgrade() -> None:
    # SQLite adds a nullable column in place; no table is rebuilt.
    for table, status in TABLES.items():
        op.add_column(table, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(
            f"UPDATE {table} SET archived_at = updated_at, status = '{status}' "
            "WHERE status = 'archived'"
        )


def downgrade() -> None:
    for table in reversed(list(TABLES)):
        op.execute(f"UPDATE {table} SET status = 'archived' WHERE archived_at IS NOT NULL")
        with op.batch_alter_table(table) as batch:
            batch.drop_column("archived_at")
