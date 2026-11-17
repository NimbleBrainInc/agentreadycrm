"""Shared fixtures: every suite runs on SQLite, through the local runtime.

**The store.** Every test gets a fresh copy of a SQLite file already at the local schema's
head, behind the installed local runtime. :func:`session_for` opens that runtime's session.
The store suites act for one principal, which is the only case the SQLite store serves.

**The server.** The tools run in this process on the local runtime, installed once here. A
test's tool calls reach the same per-test database file as its store calls, because the
installed runtime is pointed at that file before the test runs. The tools act as the local
runtime's one principal (:data:`LOCAL_SCOPE`), so a suite that mixes store calls and tool
calls makes its store calls as that principal too.

``tests/local/`` starts its own server processes on its own files, so it needs no per-test
database here.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_people import server
from mcp_people.db import store
from mcp_people.keys import address_key, match_key
from mcp_people.runtime import Scope
from mcp_people_local import schema as local_schema
from mcp_people_local.runtime import LOCAL_SCOPE, LocalRuntime

_PEOPLE_ROOT = Path(__file__).resolve().parent.parent


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """In CI (``CI`` set), a skipped test fails: a skip there would report green having
    proved nothing."""
    report = yield
    if os.environ.get("CI") and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"skipped in CI: {report.longrepr}"
    return report


#: The runtime every in-process tool call uses. Its database file changes per test.
RUNTIME = LocalRuntime(db_path=_PEOPLE_ROOT / ".pytest-unused.db")
server.install(RUNTIME)

#: The local runtime's process suites, which start their own servers on their own files.
LOCAL_SUITES = _PEOPLE_ROOT / "tests" / "local"


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One SQLite file at the local schema's head; each test gets a copy of it."""
    path = tmp_path_factory.mktemp("sqlite") / "template.db"
    local_schema.upgrade(path)
    return path


@pytest_asyncio.fixture(autouse=True)
async def backend(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[str]:
    """A fresh database file at head, behind the installed runtime, for this test.

    It lives in a directory of its own, so it never collides with a file the test makes in
    its ``tmp_path``. A test under ``tests/local/`` gets none (``"none"``).
    """
    if request.path.is_relative_to(LOCAL_SUITES):
        yield "none"
        return
    path = tmp_path_factory.mktemp("runtime") / "people.db"
    shutil.copyfile(request.getfixturevalue("sqlite_template"), path)
    await RUNTIME.dispose()
    RUNTIME.db_path = path
    try:
        yield "sqlite"
        await _assert_keys_in_sync()
    finally:
        await RUNTIME.dispose()


@pytest_asyncio.fixture
async def app_engine(backend: str):
    """The engine this test's sessions use (the installed runtime's, on this test's file)."""
    yield RUNTIME.engine()


# Each keyed table's (source column, key column, key function): the keys the store
# writes from mcp_people.keys, never the database.
_KEYED = {
    "organization": (("name", "name_key", match_key), ("domain", "domain_key", address_key)),
    "contact": (("name", "name_key", match_key), ("email", "email_key", address_key)),
}


async def _assert_keys_in_sync() -> None:
    """After each test, every organization and contact row holds the keys of its values.

    The key columns are plain columns the store writes on every write of ``name``,
    ``domain`` and ``email``; a write path that forgot one leaves a stale key that the
    dedupe indexes and lookups then read. Checking every row after every test runs the
    whole suite's write paths against that rule.
    """
    from sqlalchemy import text

    stale = []
    async with RUNTIME.engine().connect() as conn:
        for table, keys in _KEYED.items():
            columns = ", ".join(c for pair in keys for c in pair[:2])
            rows = (await conn.execute(text(f"SELECT id, {columns} FROM {table}"))).mappings()
            stale += [
                (table, row["id"], key, row[key], fold(row[source]))
                for row in rows
                for source, key, fold in keys
                if row[key] != fold(row[source])
            ]
    assert not stale, f"rows whose key is not the key of its value: {stale}"


# --------------------------------------------------------------------------- #
# Identity + session helpers                                                   #
# --------------------------------------------------------------------------- #


def make_ctx(
    tenant_id: str,
    *,
    workspace_id: str = "ws_default",
    allowed_workspaces: frozenset[str] | set[str] | None = None,
    user_id: str | None = "user_test",
    correlation_id: str | None = "corr_test",
) -> Scope:
    """The verified identity a store call acts for.

    When ``allowed_workspaces`` is not given, the read set is exactly ``{workspace_id}``.
    """
    return Scope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        allowed_workspaces=frozenset(allowed_workspaces or ()),
        user_id=user_id,
        correlation_id=correlation_id,
    )


@pytest.fixture
def ctx_factory():
    """Expose :func:`make_ctx` as a fixture for callers that prefer injection."""
    return make_ctx


def session_for(ctx: Scope) -> AbstractAsyncContextManager[AsyncConnection]:
    """One transaction on this test's database, through the installed local runtime.

    The local runtime has one principal, so the session is the same for every ``ctx``; the
    store stamps the rows it writes with ``ctx``'s identity columns and reads by them.
    """
    del ctx
    return RUNTIME.session(LOCAL_SCOPE)


async def define_signal_types(conn, ctx: Scope, *keys: str) -> None:
    """Define ``keys`` in ``ctx``'s signal vocabulary, each labelled with its key.

    ``record_signal`` takes only a type its workspace defines, and a fresh workspace
    defines none, so a suite that records a signal defines its type first.
    """
    for key in keys:
        await store.define_signal_type(conn, ctx, key=key, label=key)


@pytest_asyncio.fixture
async def seed_contact(app_engine):
    """Return an async helper that creates a contact under a given context, through the
    real store ``find_or_create_contact``. Returns the new contact id."""

    async def _seed(ctx: Scope, *, name: str, email: str | None = None) -> str:
        async with session_for(ctx) as conn:
            contact, _action, _promoted = await store.find_or_create_contact(
                conn, ctx, name=name, email=email
            )
        return contact.id

    return _seed


@pytest_asyncio.fixture
async def seed_org(app_engine):
    """Return an async helper that creates an organization under a given context, through
    the real store. Returns the new organization id."""

    async def _seed(ctx: Scope, *, name: str, domain: str | None = None) -> str:
        async with session_for(ctx) as conn:
            org, _action, _promoted = await store.find_or_create_organization(
                conn, ctx, name=name, domain=domain
            )
        return org.id

    return _seed
