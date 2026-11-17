"""The local runtime: one principal, one SQLite file, no network beyond loopback.

| Concern    | Here                                                                 |
|------------|----------------------------------------------------------------------|
| identity   | always ``Scope("local", "local")``; request headers are ignored      |
| auth       | none from FastMCP; ``wrap_http`` guards HTTP itself                  |
| session    | aiosqlite, WAL, ``BEGIN IMMEDIATE``, busy timeout, foreign keys on;  |
|            | a lock held past the busy timeout is ``Overloaded``                  |
| limits     | none                                                                 |
| record     | nothing                                                              |
| attach     | the core's structured logs routed to stderr through stdlib logging   |
| lifespan   | the schema upgraded (:mod:`mcp_people_local.schema`) before serving  |
| wrap_http  | loopback-only clients, ``Host`` and ``Origin`` (:mod:`.http`), then  |
|            | the per-install bearer token (:mod:`.auth`)                          |

The store is safe here only because there is exactly one principal: SQLite has no row-level
security, and a read by id carries no account predicate. A runtime with more than one principal
needs a database that enforces isolation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mcp_people.runtime import Limits, Overloaded, PeopleEvent, Scope
from mcp_people_local import auth, schema, sqlite
from mcp_people_local.config import database_path
from mcp_people_local.http import LoopbackGuard

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import AuthProvider
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

#: The installation's one principal. It reads only its own workspace.
LOCAL_SCOPE = Scope(
    tenant_id="local", workspace_id="local", allowed_workspaces=frozenset({"local"})
)


def route_structured_logs_to_stdlib() -> None:
    """Send the core's ``structlog`` lines through stdlib logging, which writes to stderr.

    Unconfigured, ``structlog`` prints to stdout, which on stdio carries nothing but JSON-RPC.
    """
    structlog.configure(
        processors=[
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(key_order=["event"]),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class LocalRuntime:
    """The CRM on the user's own machine, stored in the SQLite file at ``db_path``."""

    name = "local"

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else database_path()
        self._engine: AsyncEngine | None = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # URL.create, not an f-string: a "?" or "#" in a filename is not URL syntax.
            url = URL.create("sqlite+aiosqlite", database=str(self.db_path))
            engine = create_async_engine(url)
            sqlite.configure(engine.sync_engine)
            self._engine = engine
        return self._engine

    def identity(self) -> Scope:
        return LOCAL_SCOPE

    def auth(self) -> AuthProvider | None:
        # Local HTTP has no authorization server to point a client at: the loopback checks
        # and the per-install bearer token in wrap_http guard it.
        return None

    def limits(self, scope: Scope) -> Limits | None:
        return None

    @asynccontextmanager
    async def session(self, scope: Scope) -> AsyncIterator[AsyncConnection]:
        try:
            async with self.engine().begin() as conn:
                yield conn
        except OperationalError as exc:
            if sqlite.is_busy(exc):
                raise Overloaded(
                    "The local database is busy with another process's write. Nothing was run.",
                    "Wait a moment, then retry the call.",
                ) from exc
            raise

    def record(self, event: PeopleEvent, count: int = 1) -> None:
        return None

    def attach(self, mcp: FastMCP) -> None:
        route_structured_logs_to_stdlib()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        await asyncio.to_thread(schema.upgrade, self.db_path)
        try:
            yield
        finally:
            await self.dispose()

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def wrap_http(self, app: ASGIApp) -> ASGIApp:
        # The loopback checks run first, so a foreign page or host is refused (403) before
        # its token is considered; the token then keeps out other local processes (401).
        path = auth.token_path(self.db_path)
        guarded = auth.BearerGuard(app, auth.load_or_create(path))
        logger.info("local HTTP requires the bearer token stored in %s", path)
        return LoopbackGuard(guarded)
