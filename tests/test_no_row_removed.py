"""No tool removes a row (AGENTS.md): no code outside the migrations issues
a ``DELETE`` or ``TRUNCATE`` against a table.

A delete marks ``deleted_at``, a vocabulary entry retires (``retired_at``), and a link row that
goes away is marked the same way. This reads the source of every package the server ships and
fails on any statement that would remove a row: SQLAlchemy's ``table.delete()`` and ``delete()``
construct, and ``DELETE`` / ``TRUNCATE`` in SQL text. Migrations are left out: a revision may
rewrite rows the way a schema change needs.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = (
    _ROOT / "src" / "mcp_people",
    _ROOT / "src" / "mcp_people_local",
)

#: A row-removing statement: ``x.delete(``, a bare ``delete(`` call (``sqlalchemy.delete``),
#: or ``DELETE FROM`` / ``TRUNCATE`` in SQL text.
_REMOVES = re.compile(
    r"\.delete\(|(?<![\w.])delete\(|\bdelete\s+from\b|\btruncate\b", re.IGNORECASE
)


def _sources() -> list[Path]:
    return sorted(
        path
        for package in _PACKAGES
        for path in package.rglob("*.py")
        if "migrations" not in path.relative_to(package).parts
    )


def test_the_scan_reads_the_store() -> None:
    names = {path.relative_to(_ROOT).as_posix() for path in _sources()}
    assert "src/mcp_people/db/store.py" in names
    assert "src/mcp_people/server.py" in names
    assert not any("/migrations/" in name for name in names)


def test_no_code_outside_the_migrations_removes_a_row() -> None:
    found = [
        f"{path.relative_to(_ROOT)}:{number}: {line.strip()}"
        for path in _sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _REMOVES.search(line)
    ]
    assert not found, "a row-removing statement outside the migrations:\n" + "\n".join(found)


def test_the_pattern_catches_each_spelling() -> None:
    for line in (
        "await conn.execute(external_ref.delete().where(x))",
        "stmt = delete(organization_edge).where(x)",
        'text("DELETE FROM signal WHERE id = :id")',
        'op.execute("truncate contact")',
    ):
        assert _REMOVES.search(line), line
    for line in ("async def delete_contact(", "await _mark_deleted(conn, t, p, stamp)"):
        assert not _REMOVES.search(line), line
