"""Several processes share one database file.

Two processes start together on a file that does not exist yet (both run the schema upgrade)
and each performs 100 creates: an opportunity on one shared organization, which each create
finds or creates by its domain. The file then holds 200 opportunities with 200 distinct ids,
one organization, and exactly one set of default pipeline stages: ``BEGIN IMMEDIATE``
serializes the writers, so each reads the other's committed organization and stages before
it writes.

Switching a new file to WAL is the one connect step SQLite does not cover with the busy
timeout, so a process that meets another's lock there waits for it like everywhere else.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from local_harness import REPO
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from mcp_people.pipeline import DEFAULT_STAGES
from mcp_people_local import sqlite

CREATES_PER_PROCESS = 100

WORKER = """
import asyncio, sys
from pathlib import Path
from mcp_people.db import store
from mcp_people_local import schema
from mcp_people_local.runtime import LOCAL_SCOPE, LocalRuntime

async def main(path: str, tag: str, n: int) -> None:
    schema.upgrade(Path(path))
    rt = LocalRuntime(Path(path))
    for i in range(n):
        async with rt.session(LOCAL_SCOPE) as conn:
            org, _, _ = await store.find_or_create_organization(
                conn, LOCAL_SCOPE, name="Shared", domain="shared.example"
            )
            await store.find_or_create_opportunity(
                conn, LOCAL_SCOPE, title=f"{tag}-{i}", organization_id=org.id
            )
    await rt.dispose()

asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
"""


def test_two_processes_creating_at_once_keep_every_invariant(tmp_path: Path) -> None:
    db = tmp_path / "crm.db"
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(db), tag, str(CREATES_PER_PROCESS)],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("a", "b")
    ]
    for worker in workers:
        _out, err = worker.communicate(timeout=180)
        assert worker.returncode == 0, err

    with sqlite3.connect(db) as conn:
        deals = conn.execute("SELECT id, title, organization_id, stage FROM opportunity").fetchall()
        orgs = conn.execute("SELECT id FROM organization").fetchall()
        stages = [r[0] for r in conn.execute("SELECT key FROM pipeline_stage")]

    assert len(deals) == 2 * CREATES_PER_PROCESS
    assert len({d[0] for d in deals}) == len(deals)
    assert sorted(d[1] for d in deals) == sorted(
        f"{tag}-{i}" for tag in ("a", "b") for i in range(CREATES_PER_PROCESS)
    )
    assert len(orgs) == 1
    assert {d[2] for d in deals} == {orgs[0][0]}
    assert sorted(stages) == sorted(key for key, *_ in DEFAULT_STAGES)
    assert {d[3] for d in deals} == {DEFAULT_STAGES[0][0]}


def test_switch_to_wal_while_another_connection_writes_waits_for_it(tmp_path: Path) -> None:
    db = tmp_path / "crm.db"
    writer = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    writer.execute("CREATE TABLE t (x)")
    # The write lock on the rollback-journal file: the switch reads the header, then asks for
    # this lock while holding its read lock, and is refused at once, without the busy handler.
    writer.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, lambda: writer.execute("COMMIT"))
    release.start()
    engine = sqlite.configure(create_engine(f"sqlite:///{db}", poolclass=NullPool))
    try:
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    finally:
        release.join()
        engine.dispose()
        writer.close()

    assert mode == "wal"


class _Clock:
    """Stands in for the ``time`` module: ``sleep`` advances ``monotonic`` without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Cursor:
    def __init__(self, error: sqlite3.OperationalError) -> None:
        self.error = error
        self.calls = 0

    def execute(self, _statement: str) -> None:
        self.calls += 1
        raise self.error


def test_switch_to_wal_still_locked_at_the_busy_timeout_raises_the_lock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(
        sqlite, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    locked = sqlite3.OperationalError("database is locked")
    cursor = _Cursor(locked)

    with pytest.raises(sqlite3.OperationalError) as raised:
        sqlite._use_wal(cursor)

    assert raised.value is locked
    assert cursor.calls > 1
    assert clock.now >= sqlite.BUSY_TIMEOUT_MS / 1000


def test_switch_to_wal_failing_for_another_reason_raises_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(
        sqlite, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    other = sqlite3.OperationalError("disk I/O error")
    cursor = _Cursor(other)

    with pytest.raises(sqlite3.OperationalError) as raised:
        sqlite._use_wal(cursor)

    assert raised.value is other
    assert cursor.calls == 1
    assert clock.now == 0.0
