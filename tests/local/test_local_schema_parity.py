"""Schema parity: the local Alembic history, at head, is exactly the tables' metadata.

The core's tables (``mcp_people.db.tables``) are the one shape; the local SQLite history must
produce it. A table change in the core needs a migration in that history.

- After upgrading to head, Alembic autogenerate against ``metadata`` reports nothing, but for
  the indexes ``metadata`` creates on Postgres only (the GIN indexes on ``fields``).
- Autogenerate cannot compare an index's predicate or an expression index on SQLite, so the
  DDL the history leaves is also held to the DDL ``metadata.create_all`` writes, statement by
  statement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Index, create_engine

from mcp_people.db.tables import metadata
from mcp_people_local import schema


def _postgres_only(obj: Any) -> bool:
    ddl_if = getattr(obj, "_ddl_if", None)
    return isinstance(obj, Index) and ddl_if is not None and ddl_if.dialect == "postgresql"


def _ddl(path: Path) -> dict[tuple[str, str], str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name != 'alembic_version'"
        ).fetchall()
    return {(kind, name): " ".join(sql.replace('"', "").split()) for kind, name, sql in rows}


def test_the_history_at_head_matches_metadata(tmp_path: Path) -> None:
    path = tmp_path / "crm.db"
    schema.upgrade(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"include_object": lambda obj, *_: not _postgres_only(obj)},
            )
            diff = compare_metadata(context, metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_the_history_writes_the_ddl_metadata_writes(tmp_path: Path) -> None:
    upgraded, created = tmp_path / "upgraded.db", tmp_path / "created.db"
    schema.upgrade(upgraded)
    engine = create_engine(f"sqlite:///{created}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    history, tables = _ddl(upgraded), _ddl(created)
    assert history == tables
    assert sum(kind == "table" for kind, _ in history) == len(metadata.tables) == 13
