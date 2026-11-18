"""Runtime selection refuses rather than guesses.

``PEOPLE_RUNTIME`` unset, unknown, or naming a runtime that is not installed each refuse to
start: through :func:`select_runtime`, through the ASGI app, and through the stdio entry point
as a non-zero exit. Nothing infers a runtime from other settings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import EntryPoint, entry_points

import pytest

from mcp_people import server
from mcp_people.runtime import (
    ENTRY_POINT_GROUP,
    RuntimeSelectionError,
    Scope,
    select_runtime,
)
from mcp_people_local.runtime import LocalRuntime


def _only(name: str) -> list[EntryPoint]:
    return [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]


def test_the_runtimes_register_under_the_entry_point_group() -> None:
    assert {ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)} == {"local"}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_refuses(value: str | None) -> None:
    env = {} if value is None else {"PEOPLE_RUNTIME": value}
    with pytest.raises(RuntimeSelectionError, match="not set"):
        select_runtime(env)


@pytest.mark.parametrize("value", ["hosted", "Local", "postgres"])
def test_unknown_refuses(value: str) -> None:
    with pytest.raises(RuntimeSelectionError, match="names no installed runtime"):
        select_runtime({"PEOPLE_RUNTIME": value})


def test_a_runtime_that_is_not_installed_refuses() -> None:
    # A runtime this process has no entry point for, though another one is installed.
    with pytest.raises(
        RuntimeSelectionError, match=r"names no installed runtime \(installed: none\)"
    ):
        select_runtime({"PEOPLE_RUNTIME": "hosted"}, available=[])
    with pytest.raises(
        RuntimeSelectionError, match=r"names no installed runtime \(installed: local\)"
    ):
        select_runtime({"PEOPLE_RUNTIME": "hosted"}, available=_only("local"))


def test_an_installed_runtime_is_selected_by_name() -> None:
    assert isinstance(select_runtime({"PEOPLE_RUNTIME": "local"}), LocalRuntime)


def test_a_process_serves_one_runtime() -> None:
    # The suite installed the local runtime; a second one is refused, not swapped in.
    installed = server.runtime()
    assert installed.name == "local"
    with pytest.raises(RuntimeError, match="already installed"):
        server.install(LocalRuntime())
    assert server.runtime() is installed


def test_a_scope_always_reads_its_active_workspace() -> None:
    assert Scope("t", "ws_1").allowed_workspaces == frozenset({"ws_1"})
    granted = Scope("t", "ws_1", allowed_workspaces=frozenset({"ws_2"}))
    assert granted.allowed_workspaces == frozenset({"ws_1", "ws_2"})


def _run(statement: str, value: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PEOPLE_RUNTIME"}
    if value is not None:
        env["PEOPLE_RUNTIME"] = value
    return subprocess.run(
        [sys.executable, *statement.split(" ", 1)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("value", [None, "", "hosted"])
def test_the_stdio_entry_point_exits_non_zero(value: str | None) -> None:
    done = _run("-m mcp_people.server", value)
    assert done.returncode == 2
    assert "PEOPLE_RUNTIME" in done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize("value", [None, "hosted"])
def test_the_asgi_app_refuses_to_build(value: str | None) -> None:
    done = _run("-c from mcp_people.server import app", value)
    assert done.returncode != 0
    assert "RuntimeSelectionError" in done.stderr
    assert "PEOPLE_RUNTIME" in done.stderr


def test_a_server_started_in_process_refuses_without_one() -> None:
    # The lifespan selects the runtime when nothing bound one, so an in-memory client cannot
    # start the server on none.
    statement = (
        "-c import asyncio; from fastmcp import Client; from mcp_people.server import mcp\n"
        "async def main():\n"
        "    async with Client(mcp):\n"
        "        pass\n"
        "asyncio.run(main())"
    )
    done = _run(statement, None)
    assert done.returncode != 0
    assert "PEOPLE_RUNTIME" in done.stderr
