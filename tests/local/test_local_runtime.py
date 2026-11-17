"""The local runtime's contract: one fixed principal, no limits, nothing recorded, a busy file
refused as ``Overloaded``, and a command that serves this runtime and no other.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from local_harness import REPO, local_env

from mcp_people.runtime import Overloaded, PeopleEvent, Scope, select_runtime
from mcp_people_local import schema, sqlite
from mcp_people_local.runtime import LOCAL_SCOPE, LocalRuntime


def test_identity_is_always_the_one_principal(tmp_path: Path) -> None:
    rt = LocalRuntime(tmp_path / "crm.db")
    assert rt.identity() == Scope("local", "local", allowed_workspaces=frozenset({"local"}))
    assert rt.identity() is LOCAL_SCOPE
    assert LOCAL_SCOPE.allowed_workspaces == frozenset({"local"})


def test_nothing_is_limited_recorded_or_authorized_by_fastmcp(tmp_path: Path) -> None:
    rt = LocalRuntime(tmp_path / "crm.db")
    assert rt.auth() is None
    assert rt.limits(LOCAL_SCOPE) is None
    for event in PeopleEvent:
        assert rt.record(event, 3) is None


def test_the_local_runtime_is_selected_by_name() -> None:
    assert isinstance(select_runtime({"PEOPLE_RUNTIME": "local"}), LocalRuntime)


async def test_a_file_locked_past_the_busy_timeout_is_overloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "crm.db"
    schema.upgrade(db)
    monkeypatch.setattr(sqlite, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    rt = LocalRuntime(db)
    try:
        with pytest.raises(Overloaded) as refused:
            async with rt.session(LOCAL_SCOPE):
                pass
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        await rt.dispose()
    assert "busy" in refused.value.message
    assert refused.value.next_step


@pytest.mark.parametrize("value", ["hosted", "postgres"])
def test_the_command_refuses_another_runtime(tmp_path: Path, value: str) -> None:
    db = tmp_path / "crm.db"
    done = subprocess.run(
        [sys.executable, "-m", "mcp_people_local"],
        env=local_env(db, PEOPLE_RUNTIME=value),
        cwd=str(REPO),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "serves the local runtime only" in done.stderr
    assert done.stdout == ""
    # Refused before any runtime started: no database was made.
    assert not db.exists()
