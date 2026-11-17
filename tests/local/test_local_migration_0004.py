"""The local revision ``0004`` runs up, down and up on a SQLite file: the vocabularies' ``retired_at`` / ``replaced_by_key`` and the edge's
``deleted_at`` come and go, the downgrade removes the marked edges and restores the full
``uq_organization_edge``, and the upgrade after it leaves every row live.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine

from mcp_people_local import schema, sqlite


def _migrate(path: Path, direction: str, revision: str) -> None:
    config = schema.alembic_config()
    engine = sqlite.configure(create_engine(f"sqlite:///{path}"))
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            getattr(command, direction)(config, revision)
    finally:
        engine.dispose()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _edge_index(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'uq_organization_edge'"
    ).fetchone()[0]


def _seed(conn: sqlite3.Connection) -> None:
    for org in ("org_seed", "org_kept", "org_dup"):
        conn.execute(
            "INSERT INTO organization (id, tenant_id, workspace_id, name, name_key) "
            "VALUES (?, 't', 'w', ?, ?)",
            (org, org, org),
        )
    for edge, to, deleted in (("oe_live", "org_kept", None), ("oe_marked", "org_dup", "2026")):
        conn.execute(
            "INSERT INTO organization_edge (id, tenant_id, workspace_id, from_organization_id, "
            "to_organization_id, relation, deleted_at) VALUES (?, 't', 'w', 'org_seed', ?, "
            "'similar_to', ?)",
            (edge, to, deleted),
        )
    conn.execute(
        "INSERT INTO signal_type (id, tenant_id, workspace_id, key, label, retired_at) "
        "VALUES ('st_1', 't', 'w', 'hiring', 'Hiring', '2026-11-16')"
    )


def test_up_down_up(tmp_path: Path) -> None:
    path = tmp_path / "crm.db"
    _migrate(path, "upgrade", "0004")
    with closing(sqlite3.connect(path)) as conn, conn:
        assert {"retired_at", "replaced_by_key"} <= _columns(conn, "pipeline_stage")
        assert {"retired_at", "replaced_by_key"} <= _columns(conn, "signal_type")
        assert "deleted_at" in _columns(conn, "organization_edge")
        assert "WHERE deleted_at IS NULL" in _edge_index(conn)
        _seed(conn)

    _migrate(path, "downgrade", "0003")
    with closing(sqlite3.connect(path)) as conn:
        assert not {"retired_at", "replaced_by_key"} & _columns(conn, "pipeline_stage")
        assert not {"retired_at", "replaced_by_key"} & _columns(conn, "signal_type")
        assert "deleted_at" not in _columns(conn, "organization_edge")
        assert "WHERE" not in _edge_index(conn)
        assert [r[0] for r in conn.execute("SELECT id FROM organization_edge")] == ["oe_live"]
        assert [r[0] for r in conn.execute("SELECT key FROM signal_type")] == ["hiring"]

    _migrate(path, "upgrade", "0004")
    with closing(sqlite3.connect(path)) as conn:
        assert "WHERE deleted_at IS NULL" in _edge_index(conn)
        assert conn.execute("SELECT deleted_at FROM organization_edge").fetchall() == [(None,)]
        assert conn.execute("SELECT retired_at FROM signal_type").fetchall() == [(None,)]
