"""The runtime contract: what a deployment supplies, and how one is chosen.

The core (models, tables, store, tools, UI, skills) is the same in every deployment. What
differs is a small set of implementations, a **runtime**: where identity comes from, which
bearer tokens the HTTP transport accepts, how a database session opens, what limits a scope
works under, what an event records, what else the server serves, and what wraps its HTTP app.
Configuration chooses the runtime: ``PEOPLE_RUNTIME`` names one, and each installed runtime
registers itself under the ``mcp_people.runtimes`` entry-point group, so this module names no
runtime package.

The server refuses to start when ``PEOPLE_RUNTIME`` is unset or names no installed runtime. It
never infers one from other settings: a multi-account deployment that silently fell back to a
weaker runtime would drop its isolation.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Protocol

from nimblebrain_server_sdk.errors import ErrorCode, ServiceError
from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import AuthProvider
    from starlette.types import ASGIApp

__all__ = [
    "ENTRY_POINT_GROUP",
    "RUNTIME_ENV",
    "AuthError",
    "Limits",
    "Overloaded",
    "PeopleEvent",
    "Principal",
    "Runtime",
    "RuntimeSelectionError",
    "Scope",
    "select_runtime",
]

RUNTIME_ENV = "PEOPLE_RUNTIME"
ENTRY_POINT_GROUP = "mcp_people.runtimes"


class Principal(Protocol):
    """Who a store call acts for: the identity every row it writes or reads is scoped to.

    ``allowed_workspaces`` is the set of workspaces the caller may read, the active one
    included; a store listing states it in its query so the planner can use an index.
    """

    @property
    def tenant_id(self) -> str: ...
    @property
    def workspace_id(self) -> str: ...
    @property
    def allowed_workspaces(self) -> frozenset[str]: ...
    @property
    def user_id(self) -> str | None: ...
    @property
    def correlation_id(self) -> str | None: ...
    def identity_columns(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class Scope:
    """The verified identity of one call. Never built from a tool argument.

    * ``tenant_id``: the hard isolation boundary.
    * ``workspace_id``: the active workspace, the only one a write goes to.
    * ``allowed_workspaces``: the workspaces the call may read. Always holds the active one;
      a read grant never implies a write.
    """

    tenant_id: str
    workspace_id: str
    allowed_workspaces: frozenset[str] = field(default_factory=frozenset)
    user_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "allowed_workspaces", frozenset(self.allowed_workspaces) | {self.workspace_id}
        )

    def identity_columns(self) -> dict[str, str]:
        """The ``tenant_id`` / ``workspace_id`` columns a row written under this scope carries."""
        return {"tenant_id": self.tenant_id, "workspace_id": self.workspace_id}


@dataclass(frozen=True)
class Limits:
    """How much a scope may hold. The store enforces none yet, so it has no field yet; each
    limit arrives as a field with the store's enforcement of it."""


class AuthError(ServiceError):
    """No verified identity could be established for the call: fail closed.

    It is the ``auth_required`` service error, raised before any session opens. ``message`` is
    all a caller sees; ``detail`` says what was missing, for the operator's log. The error
    contract (``add_error_contract``) returns it as the flagged tool error ``unauthenticated``.
    """

    MESSAGE = "This request carries no verified identity, so the server did not run it."

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.AUTH_REQUIRED, self.MESSAGE, detail=detail)


class Overloaded(Exception):
    """The call was refused for capacity before it ran anything; a retry is safe.

    ``message`` and ``next_step`` are host-safe text, returned as the ``rate_limited`` result.
    """

    def __init__(
        self,
        message: str = "The server is at capacity right now. Nothing was run.",
        next_step: str = "Wait a few seconds, then retry the call.",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step


class PeopleEvent(StrEnum):
    """A domain event a runtime may record (count, log, or ignore)."""

    CONTACT_CREATED = "contact_created"
    ORGANIZATION_CREATED = "organization_created"
    #: A discovered organization became active, explicitly or through a match.
    ORGANIZATION_PROMOTED = "organization_promoted"
    INTERACTION_LOGGED = "interaction_logged"
    SIMILAR_COMPANIES_LINKED = "similar_companies_linked"
    SIMILAR_COMPANIES_LISTED = "similar_companies_listed"
    OPPORTUNITY_CREATED = "opportunity_created"
    OPPORTUNITY_STAGE_CHANGED = "opportunity_stage_changed"
    SIGNAL_RECORDED = "signal_recorded"
    SIGNAL_WITHDRAWN = "signal_withdrawn"
    #: A record_signal whose key matched a row recording a different fact.
    SIGNAL_COLLIDED = "signal_collided"
    PROOF_POINT_RECORDED = "proof_point_recorded"
    #: A proof point attested to each verification state.
    PROOF_POINT_ATTESTED_VERIFIED = "proof_point_attested_verified"
    PROOF_POINT_ATTESTED_UNVERIFIED = "proof_point_attested_unverified"
    PROOF_POINT_ATTESTED_FABRICATED_PLACEHOLDER = "proof_point_attested_fabricated_placeholder"
    #: A tool resolved an id to not-found: absent, or out of the caller's scope.
    ORGANIZATION_DENIED = "organization_denied"
    CONTACT_DENIED = "contact_denied"
    OPPORTUNITY_DENIED = "opportunity_denied"
    SIGNAL_DENIED = "signal_denied"
    PROOF_POINT_DENIED = "proof_point_denied"


class Runtime(Protocol):
    """What a deployment supplies. The tools call ``identity`` then ``session``."""

    #: The name it registers under in the ``mcp_people.runtimes`` group.
    name: str

    def identity(self) -> Scope:
        """The calling identity. Raises :class:`AuthError` when none is verified."""
        ...

    def auth(self) -> AuthProvider | None:
        """The FastMCP auth provider that gates the HTTP transport, or ``None`` for none.

        :func:`mcp_people.server.install` sets it on the server before the HTTP app is built.
        A runtime whose identity is verified in front of the server returns ``None``.
        """
        ...

    def session(self, scope: Scope) -> AbstractAsyncContextManager[AsyncConnection]:
        """One transaction for ``scope``. May raise :class:`Overloaded` on entry.

        A failure of the session itself (it cannot open, or its commit fails) is raised as a
        classified, host-safe ``ServiceError``; a failure the body raised propagates as its own.
        """
        ...

    def limits(self, scope: Scope) -> Limits | None:
        """What ``scope`` may hold, or ``None`` when nothing is limited."""
        ...

    def record(self, event: PeopleEvent, count: int = 1) -> None:
        """Record ``count`` occurrences of ``event``."""
        ...

    def attach(self, mcp: FastMCP) -> None:
        """Register what this deployment serves beyond the tools: routes and resources."""
        ...

    def lifespan(self) -> AbstractAsyncContextManager[None]:
        """Startup and shutdown work around the server's life (settings checks, disposal)."""
        ...

    def wrap_http(self, app: ASGIApp) -> ASGIApp:
        """The HTTP app as this deployment serves it."""
        ...


class RuntimeSelectionError(RuntimeError):
    """``PEOPLE_RUNTIME`` does not name an installed runtime; the server must not start."""


def select_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    available: Iterable[EntryPoint] | None = None,
) -> Runtime:
    """The runtime ``PEOPLE_RUNTIME`` names, constructed. Refuses rather than guesses.

    ``available`` is the registered entry points, for a test; by default the installed ones.
    """
    env = os.environ if environ is None else environ
    name = (env.get(RUNTIME_ENV) or "").strip()
    registered = {
        ep.name: ep
        for ep in (entry_points(group=ENTRY_POINT_GROUP) if available is None else available)
    }
    installed = ", ".join(sorted(registered)) or "none"
    if not name:
        raise RuntimeSelectionError(
            f"{RUNTIME_ENV} is not set. Set it to an installed runtime (installed: {installed})."
        )
    if name not in registered:
        raise RuntimeSelectionError(
            f"{RUNTIME_ENV}={name!r} names no installed runtime (installed: {installed})."
        )
    factory = registered[name].load()
    return factory()
