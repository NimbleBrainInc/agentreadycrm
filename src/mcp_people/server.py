"""People MCP Server — the tools, the skills and the UI, on whichever runtime is installed.

The **only public door** to the People relationship graph (Organization · Contact ·
Interaction). Every tool:

  1. reads the call's verified :class:`~mcp_people.runtime.Scope` from the runtime
     (``runtime().identity()``) — identity is NEVER read from a tool argument or
     request body;
  2. opens one transaction through the runtime (``runtime().session(scope)``) and calls
     the matching store function (the store stamps ``tenant_id``/``workspace_id`` from the scope);
  3. returns the documented dict INCLUDING a non-empty ``next_step`` string — the
     idea that makes a data API agent-legible.

A failure is a tool result flagged ``isError`` with one body for every kind:
``{"error": {"code", "message", "retryable", ...}, "next_step"}``. Out-of-scope reads and
writes are ``not_found`` (404 semantics, never an existence oracle); a bad argument is
``invalid_request``; a key another record holds is ``conflict``; a call with no verified
identity is ``unauthenticated``; a call refused for capacity is ``rate_limited``; anything
else is ``internal_error``. None is a 500/stack trace to the agent.

The runtime (:mod:`mcp_people.runtime`) is what differs between runtimes: where identity
comes from, how a session opens, what an event records, what the server serves beyond these
tools (probes), and what wraps the HTTP app (observability). ``PEOPLE_RUNTIME`` selects it,
and :func:`install` binds it once per process. Importing this module stays free of the
environment and the database, so tooling can introspect the tool surface.

Entry points: ``mcp_people.server:app`` is the ASGI app (built on first access, for the
runtime ``PEOPLE_RUNTIME`` names), and ``python -m mcp_people.server`` serves stdio.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Annotated, Any, Literal, Required, TypedDict

import structlog
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.exceptions import ResourceError
from fastmcp.exceptions import ValidationError as ArgumentsRefused
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.server.lowlevel import NotificationOptions
from mcp.types import TextContent, ToolAnnotations
from nimblebrain_server_sdk import (
    Placement,
    SkillDefinition,
    add_error_contract,
    add_server_card,
    add_skills,
    announce_resource_list_changed,
)
from nimblebrain_server_sdk.errors import (
    ErrorCode,
    ServiceError,
    rate_limited_tool_error,
    to_tool_error,
)
from pgkeyset import INVALID_CURSOR_MESSAGE, InvalidCursor
from pydantic import ConfigDict, Field, ValidationError, with_config
from sqlalchemy.exc import DBAPIError

from mcp_people.addresses import (
    CONTACT_URI_TEMPLATE,
    OPPORTUNITY_URI_TEMPLATE,
    ORGANIZATION_URI_TEMPLATE,
    PROOF_POINT_URI_TEMPLATE,
    SIGNAL_URI_TEMPLATE,
    record_uri,
)
from mcp_people.db import store
from mcp_people.db.store import (
    ConflictError,
    ContactNotFoundError,
    DeleteRefusedError,
    LimitExceededError,
    OpportunityNotFoundError,
    OrganizationNotFoundError,
    ProofPointNotFoundError,
    RefConflictError,
    SignalNotFoundError,
    SignalWithdrawnError,
)
from mcp_people.evidence import FABRICATED_PLACEHOLDER, UNVERIFIED, VERIFIED
from mcp_people.fields import CORE_FIELDS, FieldError
from mcp_people.models import dump
from mcp_people.runtime import (
    AuthError,
    Overloaded,
    PeopleEvent,
    Runtime,
    RuntimeSelectionError,
    Scope,
    select_runtime,
)
from mcp_people.ui import VIEWS, load_ui

SERVICE = "people"
#: The distribution this package ships in, whose version the server reports.
DISTRIBUTION = "agentreadycrm"


def package_version() -> str:
    """The installed ``agentreadycrm`` version, or ``dev`` when it is not installed."""
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "dev"


# Logs go to stderr only; stdout stays clean JSON-RPC for the stdio transport.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = structlog.stdlib.get_logger("mcp_people")

load_dotenv()


# --------------------------------------------------------------------------- #
# The runtime: bound once per process.                                         #
# --------------------------------------------------------------------------- #

_runtime: Runtime | None = None


def install(rt: Runtime) -> Runtime:
    """Bind ``rt`` as this process's runtime, set its auth provider, and let it attach.

    A process serves one runtime: binding a second, different one is refused rather than
    letting tools and routes disagree about where identity comes from. ``FastMCP.auth`` is
    read when ``http_app()`` builds the Starlette app, which ``app`` does after installing.
    """
    global _runtime
    if _runtime is not None:
        if _runtime is rt:
            return rt
        raise RuntimeError(f"the {_runtime.name!r} runtime is already installed")
    mcp.auth = rt.auth()
    rt.attach(mcp)
    _runtime = rt
    return rt


def runtime() -> Runtime:
    """The installed runtime. Raises if none is, so no call runs without one."""
    if _runtime is None:
        raise RuntimeError("no People runtime is installed; set PEOPLE_RUNTIME")
    return _runtime


def installed() -> Runtime:
    """The installed runtime, installing the one ``PEOPLE_RUNTIME`` names if none is yet."""
    return _runtime if _runtime is not None else install(select_runtime())


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """The runtime's startup and shutdown work around the server's life.

    A server started with no runtime bound selects the one ``PEOPLE_RUNTIME`` names, and
    refuses to start without one.
    """
    async with installed().lifespan():
        logger.info("people_startup", extra={"service": SERVICE})
        try:
            yield
        finally:
            logger.info("people_shutdown", extra={"service": SERVICE})


#: The rules that span tools, stated once. A tool's description says when to call it; these
#: say how the tools work together, for a host that reads no skill.
INSTRUCTIONS = """\
A CRM of organizations, contacts, the interactions with them, deals in a pipeline, and the \
evidence behind a claim.

Resolve before you write. find_or_create_contact and find_or_create_organization return the \
record you already hold (by external reference, email or domain, else name), so call one \
once per person or company with everything you know. To change a record use update_*; to \
join two records of one person or company use merge_*, after the user confirms. A deal has \
no natural key: check list_opportunities(organization_id=..., query=...) before \
create_opportunity.

Facts go in fields. Anything you would filter, sort or count by is a field: list_fields lists \
them, define_field adds one, and update_*(fields={key: value}) writes it. A fact about a \
company goes on the organization, never on each of its contacts. A fact you would cite is a \
signal (record_signal) or a proof point (record_proof_point).

Query, don't walk. list_contacts, list_organizations and list_opportunities take a free-text \
query, a filter, a sort and columns, a page at a time; count_records says how a workspace \
divides in one call. A filter is {"field": path, "op": op, "value": v}, or {"and": [...]} / \
{"or": [...]} of those. Operators by field type: text eq, ne, in, not_in, contains, \
starts_with; select eq, ne, in, not_in; number and date eq, ne, gt, gte, lt, lte, between; \
timestamp gt, gte, lt, lte, between; boolean eq; list and multi_select has, has_all; any \
field is_set (true or false).

Ids (ct_, org_, op_, sg_, pp_) come from a result; never construct one. Every result \
carries next_step: follow it. Hosts that load skills can read skill://people/SKILL.md and \
skill://people-evidence/SKILL.md for more."""


#: The server's name, in the reverse-DNS form the Server Card requires, under the product's
#: own domain. Its display name is the card's ``title``.
SERVER_NAME = "com.agentreadycrm/agentreadycrm"

#: The product's site, reported in ``serverInfo`` and on the Server Card.
WEBSITE = "https://agentreadycrm.com"

mcp: FastMCP = FastMCP(
    SERVER_NAME,
    # SERVICE_VERSION when a deployment sets it, else the installed package's version.
    version=os.environ.get("SERVICE_VERSION") or package_version(),
    website_url=WEBSITE,
    lifespan=_lifespan,
    # An exception no handler classified reaches the host as fixed text, never its message:
    # a tool returns its failures or raises one the error contract returns (below), and a
    # resource read raises a ResourceError (passed through as written) or is told only which
    # read failed.
    mask_error_details=True,
    # Standalone: a host that never reads the skills still gets the rules that span tools.
    instructions=INSTRUCTIONS,
)


#: The service error contract's codes as this server names them. Any other code (a database
#: that did not answer, an unclassified fault) is ``internal_error``.
_CONTRACT_CODES = {
    "auth_required": "unauthenticated",
    "bad_input": "invalid_request",
    "not_found": "not_found",
    "conflict": "conflict",
    "rate_limited": "rate_limited",
}


def _contract_next_step(code: str, retryable: bool) -> str:
    if code == "unauthenticated":
        return (
            "Reconnect so the request carries a verified identity, then call again; nothing "
            "was read or written."
        )
    if code == "rate_limited":
        return "Wait a few seconds, then call again; nothing ran."
    if retryable:
        return "Call again shortly; nothing was read or written."
    return (
        "Read back what the call would have changed before calling again; if it keeps "
        "failing, the service may be degraded."
    )


def _from_contract(error: dict[str, Any]) -> dict[str, Any]:
    """The service error contract's ``error`` as this server's envelope."""
    code = _CONTRACT_CODES.get(str(error.get("code")), "internal_error")
    retryable = bool(error.get("retryable"))
    return {
        "error": {
            "code": code,
            "message": str(error.get("message") or "The call failed."),
            "retryable": retryable,
        },
        "next_step": _contract_next_step(code, retryable),
    }


class _OneErrorShape(Middleware):
    """Return every failure as one shape, flagged as an error result (``isError``).

    A tool answers a refusal with the envelope ``{"error": {code, message, retryable, ...},
    "next_step"}`` as a plain dict, which FastMCP scores a success, so this flags it. A
    failure the service error contract answered (no verified identity, a workspace at its
    limit, the database unreachable) carries the contract's ``{"error": {code, message,
    retryable}}``; this restates it in the envelope, codes renamed and ``next_step`` added.
    An argument the tool's schema refuses never reaches the tool: FastMCP raises it here,
    and it leaves as ``invalid_request`` naming the argument.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> ToolResult:
        try:
            result = await call_next(context)
        except ArgumentsRefused as exc:
            return _error_result(
                _envelope(
                    "invalid_request",
                    _schema_message(exc),
                    "Call it again with arguments its schema accepts: the message names the "
                    "one refused.",
                )
            )
        body = result.structured_content
        if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
            return result
        if "next_step" not in body:
            body = _from_contract(body["error"])
        return _error_result(body)


def _schema_message(exc: ArgumentsRefused) -> str:
    """What a schema refusal names: each refused argument and why."""
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return str(exc)
    parts = []
    for err in cause.errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "kwargs") or "arguments"
        parts.append(f"{where}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or str(exc)


# The capabilities a handshake-era client (protocol 2025-11-25 and earlier) reads. The tool
# and prompt sets never change, so neither promises list_changed; a write announces
# resources/list_changed, so resources does. FastMCP declares all three by default. A
# 2026-07-28-era client derives every listChanged from whether subscriptions/listen is
# served, which it is not here, and reads the ui and skills extensions, which the older
# era's schema has no field for (tests/test_capabilities.py holds both eras).
mcp._mcp_server.notification_options = NotificationOptions(
    prompts_changed=False, resources_changed=True, tools_changed=False
)

mcp.add_middleware(_OneErrorShape())

# A tool call with no verified identity raises ``AuthError`` from ``runtime().identity()``
# before anything runs. The contract classifies it ``auth_required``, counted in
# ``service_errors_total``, and ``_OneErrorShape`` returns it as ``unauthenticated``: the
# caller is told only that the request carried no verified identity, and the header that was
# missing is in the log line's ``detail``.
add_error_contract(mcp)

# Agent guidance, bundled inside the package so it ships with the wheel. Published through
# the MCP Skills extension, which lists it in skills/list and serves it at
# skill://people/SKILL.md; the server instructions above point at it.
SKILL_CONTENT = files("mcp_people").joinpath("SKILL.md").read_text(encoding="utf-8")
# Recording the facts behind a claim, bound to the evidence tools so a turn that never touches
# a signal or a proof point pays nothing for it.
EVIDENCE_SKILL_CONTENT = (
    files("mcp_people").joinpath("skills", "people-evidence.md").read_text(encoding="utf-8")
)
add_skills(
    mcp,
    [
        SkillDefinition("people", {"SKILL.md": SKILL_CONTENT}),
        SkillDefinition("people-evidence", {"SKILL.md": EVIDENCE_SKILL_CONTENT}),
    ],
)


# --------------------------------------------------------------------------- #
# Annotations. Every tool acts on this server's own database and nothing else,  #
# so each is closed-world (the spec's default is open). Idempotent means a       #
# second identical call leaves the records as the first did; the audit row and  #
# updated_at a write also stamps are bookkeeping, not part of that.             #
# --------------------------------------------------------------------------- #

#: A read: changes nothing.
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
#: An add that converges on a key (a domain, an email, a stage key, a source): a repeat
#: returns what the first call made.
ADD_CONVERGING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: An add with no natural key: without an idempotency_key each call adds another.
ADD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
#: An edit that overwrites values: setting the same values twice equals once.
EDIT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: A move another call undoes (a stage, a promotion, whom a signal is evidence for).
MOVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: A move another call undoes that stamps the time of each call.
MOVE_STAMPED = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
#: A withdrawal or delete: no tool brings the record back, and a repeat changes nothing.
REMOVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: A merge: no tool splits the records again, and a repeat names a record already merged.
MERGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)


# --------------------------------------------------------------------------- #
# ChatGPT's keys. ChatGPT reads two OpenAI-specific ``_meta`` keys beside the  #
# MCP Apps ones; the source of truth is the OpenAI Apps SDK reference,         #
# https://developers.openai.com/plugins/reference. They are plain metadata,    #
# the same in every runtime, and other hosts ignore them.                      #
# --------------------------------------------------------------------------- #

#: On every tool. ``openai/widgetAccessible`` defaults to ``false``, and ChatGPT reads it to
#: decide whether a view may call the tool from its frame. The views read, write, import and
#: merge through most of the tools, and no tool is for the model alone, so every tool carries
#: ``true``: a tool a view starts calling later is never refused in ChatGPT alone.
CHATGPT_TOOL_META: dict[str, Any] = {"openai/widgetAccessible": True}

#: The product's origin, which ChatGPT renders the views under. ``openai/widgetDomain`` is
#: ChatGPT's alias for ``_meta.ui.domain``. The alias carries it rather than ``ui.domain``
#: because other MCP Apps hosts validate ``ui.domain`` against domains of their own. It names
#: the product, not a deployment, so it is a constant.
CHATGPT_WIDGET_DOMAIN = "https://agentreadycrm.com"

#: On each view resource and its build template, in the listing and in what a read returns.
CHATGPT_UI_META: dict[str, Any] = {"openai/widgetDomain": CHATGPT_WIDGET_DOMAIN}


# --------------------------------------------------------------------------- #
# Error mapping: never let a store/validation error become a 500/stack trace    #
# on the wire. Out-of-scope → not_found (404 semantics, no existence oracle);    #
# validation → invalid_request. Missing identity is the error contract's.       #
# --------------------------------------------------------------------------- #


# How to go find the right id, per entity. The graph entities are resolved with the
# idempotent upsert; the evidence records have no find_or_create (a signal is
# resolved by the account it hangs off, not by a natural key a caller can restate).
_NOT_FOUND_HINT = {
    "contact": "find it with list_contacts(query=...)",
    "organization": "find it with list_organizations(query=...)",
    "opportunity": "find it with list_opportunities(query=...)",
    "signal": "list_signals(organization_id=...) to find the account's signals",
    "proof_point": "list_proof_points(organization_id=...) to find the recorded claims",
}


#: The denial event for each entity a not-found names.
_DENIED = {
    "organization": PeopleEvent.ORGANIZATION_DENIED,
    "contact": PeopleEvent.CONTACT_DENIED,
    "opportunity": PeopleEvent.OPPORTUNITY_DENIED,
    "signal": PeopleEvent.SIGNAL_DENIED,
    "proof_point": PeopleEvent.PROOF_POINT_DENIED,
}

#: The attestation event for each verification state a proof point is attested to.
_ATTESTED = {
    VERIFIED: PeopleEvent.PROOF_POINT_ATTESTED_VERIFIED,
    UNVERIFIED: PeopleEvent.PROOF_POINT_ATTESTED_UNVERIFIED,
    FABRICATED_PLACEHOLDER: PeopleEvent.PROOF_POINT_ATTESTED_FABRICATED_PLACEHOLDER,
}


def _envelope(
    code: str, message: str, next_step: str, *, retryable: bool = False, **context: Any
) -> dict[str, Any]:
    """The one failure body: ``{"error": {code, message, retryable, ...context}, next_step}``.

    ``code``, ``message`` and ``retryable`` are the service error contract's keys, so a
    refusal a tool returns and one the contract classifies read the same way.
    """
    return {
        "error": {"code": code, "message": message, "retryable": retryable, **context},
        "next_step": next_step,
    }


def _error_result(body: dict[str, Any]) -> ToolResult:
    """``body`` as a tool result flagged ``isError``, its JSON the text a model reads."""
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
        is_error=True,
    )


def _not_found(entity: str, entity_id: str) -> dict[str, Any]:
    """A not-found failure (404 semantics). Records the entity's denial event.

    Out-of-scope ids and genuinely-absent ids are indistinguishable by contract
    (RLS returns nothing either way), so we never reveal which it was — there is no
    existence oracle. The denial event is the only observable signal.
    """
    runtime().record(_DENIED[entity])
    return _envelope(
        "not_found",
        f"No {entity} with id {entity_id!r} is visible in this workspace.",
        f"Verify the {entity} id, or {_NOT_FOUND_HINT[entity]}.",
        entity=entity,
        id=entity_id,
    )


#: The step for a bad argument whose message names it.
_FIX_ARGUMENT = "Nothing was changed. Correct the argument the message names, then call again."

#: The step for a cursor no listing issued.
_FIX_CURSOR = (
    "Omit cursor to start from the first page, or pass the next_cursor the previous page "
    "returned, with the same arguments."
)


def _invalid(message: str, next_step: str = _FIX_ARGUMENT) -> dict[str, Any]:
    """An ``invalid_request`` failure: a bad argument, never a 500."""
    return _envelope("invalid_request", message, next_step)


def _internal_error(exc: Exception, *, where: str) -> dict[str, Any]:
    """The failure for what no specific handler owns — the final safety net.

    The specific handlers above route every KNOWN failure (not-found, conflict, bad argument)
    to its own code, and a call with no identity raises before a tool's ``try``. This
    classifies the rest through the service error contract (``to_tool_error``), which counts
    it in ``service_errors_total`` and logs its detail, and returns it in the envelope with a
    fixed message that never carries the exception's message, type or stack.

    * A session that failed on its own (``runtime().session``) is already classified: a
      database that did not answer is retryable, anything else is not.
    * A call refused for capacity (:class:`~mcp_people.runtime.Overloaded`: its workspace is
      at its concurrency limit, or no connection freed in time) ran nothing:
      ``rate_limited``, safe to retry, with the refusal's own next step.
    * Anything else the body raised (a query the database dropped mid-call, a bug) is
      ``internal_error``, its exception logged here with the tool's name.

    No audit write is attempted: the database may be exactly what is down.
    """
    if isinstance(exc, Overloaded):
        refused = rate_limited_tool_error(exc).structured_content or {}
        return {**_from_contract(refused.get("error") or {}), "next_step": exc.next_step}
    if not isinstance(exc, ServiceError):
        logger.error("people_internal_error", exc_info=True, extra={"where": where})
        exc = ServiceError(ErrorCode.INTERNAL, detail=f"{where}: {exc!r}")
    classified = to_tool_error(exc).structured_content or {}
    return _from_contract(classified.get("error") or {})


# Which find_or_create entity owns each dedupe field — for the conflict next_step.
_CONFLICT_ENTITY = {"email": "contact", "domain": "organization"}


def _conflict(field: str) -> dict[str, Any]:
    """A conflict failure (a unique-dedupe collision, never a 500).

    The new ``email`` / ``domain`` already belongs to another record in this
    workspace. The message names the FIELD but never the constraint/index name (no
    schema leak), and the ``next_step`` points the agent at the idempotent
    find_or_create path to locate the existing record instead of forcing a value.
    """
    entity = _CONFLICT_ENTITY.get(field, "record")
    return _envelope(
        "conflict",
        f"That {field} already belongs to another record in this workspace.",
        f"Use find_or_create_{entity} to locate the existing record, or choose a "
        f"different {field}.",
        field=field,
    )


async def _audit_attempt(
    sctx: Scope,
    *,
    entity: str,
    action: str,
    outcome: str,
    entity_id: str | None = None,
) -> None:
    """Record an attempted-but-failed access as a ``people_audit`` row.

    The failing op's own transaction rolled back (a NotFound raised before its
    success-path audit, or a validation error aborted it), so the attempt would
    otherwise leave NO trace — yet an out-of-scope access attempt is exactly what
    the audit table exists to capture. We open a FRESH ``runtime().session`` (the
    caller's own account and workspace, so the RLS ``WITH CHECK`` permits the insert)
    and write one ``outcome={denied,error}`` row.

    Best-effort by contract: an audit-write failure must NEVER mask the original
    error the caller is about to return, so we swallow it with a ``logger.warning``.
    """
    try:
        async with runtime().session(sctx) as conn:
            await store.write_audit(
                conn, sctx, entity=entity, action=action, outcome=outcome, entity_id=entity_id
            )
    except Exception:  # noqa: BLE001 - audit is best-effort; never mask the real error
        logger.warning(
            "people_audit_attempt_failed",
            extra={"entity": entity, "action": action, "outcome": outcome},
        )


async def _handle_tool_error(
    sctx: Scope, *, entity: str, action: str, exc: Exception
) -> dict[str, Any]:
    """Record the failed attempt, then return the matching structured tool result.

    Centralizes the except-branch handling shared by every tool: a NotFound is a
    ``denied`` attempt (out-of-scope/absent id) that returns the ``not_found``
    shape; a ConflictError is an ``error`` attempt (unique-dedupe collision) that
    returns the ``conflict`` shape; a ValueError/TypeError is an ``error`` attempt
    (bad argument) that returns the ``invalid_request`` shape. The returned shapes
    are identical to the per-tool returns they replace. ``AuthError`` is raised BEFORE a
    Scope exists (no account or workspace to scope an audit row to), so it never
    reaches here.
    """
    if isinstance(exc, OrganizationNotFoundError):
        await _audit_attempt(
            sctx, entity="organization", action=action, outcome="denied", entity_id=str(exc)
        )
        return _not_found("organization", str(exc))
    if isinstance(exc, ContactNotFoundError):
        await _audit_attempt(
            sctx, entity="contact", action=action, outcome="denied", entity_id=str(exc)
        )
        return _not_found("contact", str(exc))
    if isinstance(exc, OpportunityNotFoundError):
        await _audit_attempt(
            sctx, entity="opportunity", action=action, outcome="denied", entity_id=str(exc)
        )
        return _not_found("opportunity", str(exc))
    if isinstance(exc, SignalNotFoundError):
        await _audit_attempt(
            sctx, entity="signal", action=action, outcome="denied", entity_id=str(exc)
        )
        return _not_found("signal", str(exc))
    if isinstance(exc, ProofPointNotFoundError):
        await _audit_attempt(
            sctx, entity="proof_point", action=action, outcome="denied", entity_id=str(exc)
        )
        return _not_found("proof_point", str(exc))
    if isinstance(exc, SignalWithdrawnError):
        # A withdrawn signal's parties do not change; the store says why and what to do.
        await _audit_attempt(
            sctx, entity=entity, action=action, outcome="error", entity_id=exc.signal_id
        )
        return _envelope("conflict", exc.message, exc.next_step, field="evidence_for")
    if isinstance(exc, DeleteRefusedError):
        # Records the user owns still depend on the one named; the store says which and
        # how to clear them.
        await _audit_attempt(sctx, entity=entity, action=action, outcome="error")
        return _envelope("conflict", exc.message, exc.next_step, field=str(exc))
    if isinstance(exc, RefConflictError):
        # An external reference that would name two records: the store says which and
        # how to settle it, so the result carries its words rather than the field's.
        await _audit_attempt(sctx, entity=entity, action=action, outcome="error")
        return _envelope("conflict", exc.message, exc.next_step, field="external_refs")
    if isinstance(exc, LimitExceededError):
        # The write would pass one of the runtime's limits: refused, and nothing it did is
        # kept. A denied attempt, like an out-of-scope id.
        await _audit_attempt(sctx, entity=entity, action=action, outcome="denied")
        return _limit_refused(exc)
    if isinstance(exc, ConflictError):
        # A unique-dedupe collision on an explicit update. The op's own transaction
        # rolled back, so record the failed attempt as an error row (so the audit trail
        # keeps it) and return the structured conflict shape. ``str(exc)``
        # is the field name only — never a constraint/index name.
        field = str(exc)
        await _audit_attempt(sctx, entity=entity, action=action, outcome="error")
        return _conflict(field)
    # ValueError / TypeError → an error attempt against the tool's primary entity.
    await _audit_attempt(sctx, entity=entity, action=action, outcome="error")
    if isinstance(exc, FieldError):
        # A field write names the one call that fixes it (define the field, add the
        # option) in place of the generic hint.
        return _invalid(str(exc), exc.next_step)
    if isinstance(exc, InvalidCursor):
        return _invalid(str(exc), _FIX_CURSOR)
    return _invalid(str(exc))


def _limit_refused(exc: LimitExceededError) -> dict[str, Any]:
    """The ``invalid_request`` failure for a write that would pass the runtime's limits.

    ``limit`` names the :class:`~mcp_people.runtime.Limits` field that refused it, so a
    client can tell this refusal from a bad argument without a new error code, and the
    refusal is recorded as that field's event.
    """
    runtime().record(PeopleEvent(f"{exc.limit}_refused"))
    return _envelope("invalid_request", exc.message, exc.next_step, limit=exc.limit)


def _record_limited(skipped: list[dict[str, Any]]) -> None:
    """Record a limit's refusal event once per row an import skipped under it."""
    for group in skipped:
        if "limit" in group:
            runtime().record(PeopleEvent(f"{group['limit']}_refused"), len(group["rows"]))


#: How many of a record's newest interactions its ``get_*`` read carries.
RECENT_INTERACTIONS = 5

#: How many open deals a contact or organization read carries, newest first, and what each
#: holds. ``list_opportunities`` lists the rest; a read answers "show me Jane" with her deals
#: in one call.
RECORD_OPEN_DEALS = 5
_OPEN_DEAL_COLUMNS = ("title", "stage", "value", "currency")


@dataclass(frozen=True)
class _OpenDeals:
    rows: list[dict[str, Any]]
    total: int


async def _open_deals(conn: Any, sctx: Any, **owner: str) -> _OpenDeals:
    """The newest open deals on an account (``organization_id``) or naming a contact
    (``primary_contact_id``), each with its address, and how many are open in all."""
    page = await store.page_opportunities(
        conn, sctx, columns=list(_OPEN_DEAL_COLUMNS), limit=RECORD_OPEN_DEALS, **owner
    )
    return _OpenDeals([_with_uri(row) for row in page.rows], page.total or 0)


def _recent(interactions: list[Any]) -> list[dict[str, Any]]:
    """A record's newest interactions as ``get_*`` carries them: without the long body."""
    return [{k: v for k, v in dump(i).items() if k != "body"} for i in interactions]


# --------------------------------------------------------------------------- #
# Argument types. A set the server owns and refuses anything outside is an enum, #
# so a client sees it before calling; a set a workspace extends (a stage, a       #
# signal type, a contact's stage, an interaction's kind) stays open text.         #
# --------------------------------------------------------------------------- #

Entity = Literal["contact", "organization", "opportunity"]
FieldType = Literal["text", "number", "boolean", "date", "select", "multi_select", "list"]
Outcome = Literal["open", "won", "lost"]
Verification = Literal["verified", "unverified", "fabricated-placeholder"]
EmailStatus = Literal["valid", "invalid", "risky", "unknown"]
Direction = Literal["inbound", "outbound"]
SignalStatus = Literal["active", "withdrawn", "any"]
OrganizationStatus = Literal["active", "discovered"]
Probability = Annotated[int, Field(ge=0, le=100)]


_ROW_FIELDS = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Custom field values by key; list_fields lists the keys. A match keeps the values "
            "it holds."
        )
    ),
]
_ROW_REFS = Annotated[
    list[str],
    Field(
        description=(
            "What other systems call this record, as opaque URIs. One a record here holds is "
            "the match, ahead of every other key."
        )
    ),
]


@with_config(ConfigDict(extra="forbid"))
class OrganizationRow(TypedDict, total=False):
    """One company to find or create. Values are text; an empty string counts as absent."""

    name: Annotated[str, Field(description="The company's name; else its domain.")]
    domain: Annotated[str, Field(description="Its web domain, e.g. acme.com.")]
    website: Annotated[str, Field(description="Its website URL.")]
    industry: Annotated[str, Field(description="Its industry (open text).")]
    region: Annotated[str, Field(description="Where it is based (open text).")]
    fields: _ROW_FIELDS
    external_refs: _ROW_REFS


@with_config(ConfigDict(extra="forbid"))
class ContactRow(TypedDict, total=False):
    """One person to find or create. Values are text; an empty string counts as absent."""

    name: Annotated[str, Field(description="The person's full name.")]
    email: Annotated[str, Field(description="Their email address.")]
    phone: Annotated[str, Field(description="Their phone number.")]
    title: Annotated[str, Field(description="Their role, e.g. VP Sales.")]
    organization_id: Annotated[str, Field(description="The organization they work at, by id.")]
    organization_name: Annotated[
        str, Field(description="Their organization's name, found or created.")
    ]
    organization_domain: Annotated[
        str, Field(description="Their organization's domain, found or created.")
    ]
    lead_source: Annotated[
        str, Field(description="How they came to you (open text, e.g. referral).")
    ]
    status: Annotated[
        str,
        Field(description="Their stage: sourced, enriched, contacted, engaged, or your own."),
    ]
    email_status: Annotated[
        str,
        Field(
            description="Whether the address can receive mail: valid, invalid, risky or unknown."
        ),
    ]
    fields: _ROW_FIELDS
    external_refs: _ROW_REFS


@with_config(ConfigDict(extra="forbid"))
class SimilarOrganization(TypedDict, total=False):
    """One company a similar-companies lookup returned."""

    domain: Required[
        Annotated[str, Field(description="Its web domain, the key it is found or created by.")]
    ]
    name: Annotated[str, Field(description="Its name; else its domain.")]
    industry: Annotated[str, Field(description="Its industry (open text).")]
    employee_range: Annotated[str, Field(description="Its headcount band, e.g. 51-200.")]
    revenue_range: Annotated[str, Field(description="Its revenue band.")]
    headquarters: Annotated[str, Field(description="Where it is headquartered.")]
    similarity: Annotated[float, Field(ge=0, le=1, description="How similar it scored, 0 to 1.")]


# --------------------------------------------------------------------------- #
# UI resources: the views, MCP Apps resources a host renders in a frame.        #
# --------------------------------------------------------------------------- #

# Each view is declared as an MCP App (FastMCP's ``app=``), which publishes the spec's
# ``_meta.ui`` beside the ``text/html;profile=mcp-app`` MIME type, on the listing and on
# what a read returns. The bundle is one self-contained file that loads nothing from
# elsewhere, so the frame's policy names no outside origin. A view asks for a border, which a
# host draws where the frame sits among other content (inline in a conversation); a host
# that gives the view a full pane draws none.
_VIEW_APP = AppConfig(prefers_border=True, csp=ResourceCSP())

#: Each view's HTML, read once at start. Every URI of a view serves this one copy, and
#: :data:`VIEW_BUILDS` hashes exactly these bytes, so a build key always names what is served.
VIEW_HTML: dict[str, str] = {view: load_ui(view) for view in VIEWS}

#: Each view's build key: the first 12 hex characters of its served HTML's SHA-256.
VIEW_BUILDS: dict[str, str] = {
    view: hashlib.sha256(html.encode("utf-8")).hexdigest()[:12] for view, html in VIEW_HTML.items()
}


def view_uri(view: str) -> str:
    """The view's listed URI. A host placement names it, so it stays fixed across builds."""
    return f"ui://people/{view}"


def bound_view_uri(view: str) -> str:
    """The URI a tool binds the view to: the listed URI plus the build key.

    A host may cache a ``ui://`` resource by URI, so the bound URI carries the build: a new UI
    is a new URI, and a host fetches it instead of rendering its cached copy.
    """
    return f"{view_uri(view)}/{VIEW_BUILDS[view]}"


def _bound_to(view: str) -> AppConfig:
    """What a read tool declares to have an MCP Apps host render ``view`` for its call.

    Claude, ChatGPT and other MCP Apps hosts render a view only for a tool that names it in
    ``_meta.ui.resourceUri``. The app opens on the record or listing the call read (the
    host hands it the call's input and result). The views also call these tools from their
    frame, so both audiences may.
    """
    return AppConfig(resource_uri=bound_view_uri(view), visibility=["model", "app"])


@mcp.resource("ui://people/contacts", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_contacts() -> str:
    """The CRM app, opening on the contacts list."""
    return VIEW_HTML["contacts"]


@mcp.resource("ui://people/organizations", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_organizations() -> str:
    """The CRM app, opening on the organizations list."""
    return VIEW_HTML["organizations"]


@mcp.resource("ui://people/opportunities", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_opportunities() -> str:
    """The CRM app, opening on the deal pipeline."""
    return VIEW_HTML["opportunities"]


@mcp.resource("ui://people/settings", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_settings() -> str:
    """The CRM app's settings: custom fields and pipeline stages."""
    return VIEW_HTML["settings"]


# A bound view under its build key. Any key resolves to the current build, so a conversation
# or a tool list that names an older build still renders the view. The key selects nothing
# and is never echoed. Settings is bound to no tool, so it has no build template.
@mcp.resource("ui://people/contacts/{build}", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_contacts_build(build: str) -> str:
    """The CRM app's contacts view under a build key. Every key serves the current build."""
    del build
    return VIEW_HTML["contacts"]


@mcp.resource("ui://people/organizations/{build}", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_organizations_build(build: str) -> str:
    """The CRM app's organizations view under a build key. Every key serves the current build."""
    del build
    return VIEW_HTML["organizations"]


@mcp.resource("ui://people/opportunities/{build}", app=_VIEW_APP, meta=CHATGPT_UI_META)
def people_ui_opportunities_build(build: str) -> str:
    """The CRM app's deal pipeline under a build key. Every key serves the current build."""
    del build
    return VIEW_HTML["opportunities"]


# --------------------------------------------------------------------------- #
# Server Card: what a host reads before it connects.                            #
# --------------------------------------------------------------------------- #

#: Where a NimbleBrain host places the views: the three lists as one app under the sidebar's
#: Apps, each at its own route named after the product, and settings on the connector's
#: settings page. They name the listed view URIs, which stay fixed across builds.
PLACEMENTS: list[Placement] = [
    {
        "slot": "sidebar.apps",
        "resourceUri": view_uri("contacts"),
        "route": "agentreadycrm/contacts",
        "label": "Contacts",
        "priority": 100,
    },
    {
        "slot": "sidebar.apps",
        "resourceUri": view_uri("organizations"),
        "route": "agentreadycrm/organizations",
        "label": "Organizations",
        "priority": 101,
    },
    {
        "slot": "sidebar.apps",
        "resourceUri": view_uri("opportunities"),
        "route": "agentreadycrm/opportunities",
        "label": "Opportunities",
        "priority": 102,
    },
    {"slot": "settings", "resourceUri": view_uri("settings")},
]

# Served at <mcp-url>/server-card over streamable HTTP only. Its name, version and website are
# serverInfo's; the title, description and placements are declared here, and the title is
# serverInfo's title too.
add_server_card(
    mcp,
    title="Agent Ready CRM",
    description="A CRM for AI agents: organizations, contacts, deals and the evidence behind them.",
    placements=PLACEMENTS,
)


# --------------------------------------------------------------------------- #
# Writes                                                                        #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def find_or_create_organization(
    name: str,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    source: str | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    external_refs: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Find an organization, or create it when this workspace does not hold it yet.

    Call it whenever a company comes up that you may record or act on: it is how you get an
    organization's id without making a duplicate. It matches by external reference, then
    domain, then name, and a match fills only the values the organization lacks, never
    overwriting one, so call it once with everything you know. To change a value it already
    holds use ``update_organization``; for a list of more than a few companies use
    ``import_organizations``. Matching a discovered lookalike promotes it to active.

    Args:
        name: The company's name (required).
        domain: Its web domain, e.g. "acme.com"; a URL is reduced to its domain. The strongest
            match key after an external reference.
        website: Its website URL.
        industry: Its industry in your own words (open text, e.g. "Logistics software").
        region: Where it is based or operates (open text, e.g. "EMEA").
        source: Where you learned of it (open text, e.g. "research", "manual", "import").
        fields: Custom field values by key, e.g. {"fit": "strong"}; list_fields lists the
            keys. A match keeps the values it holds and takes only the keys it lacks.
        fields_by: Who is setting these fields: an agent, a run, or a person.
        external_refs: What other systems call this record, as opaque URIs (e.g.
            "crm://people/123", or the source's own record URL). One a record here already
            holds is the match, ahead of every other key; one held by another record is
            refused as a conflict, never moved.

    Returns:
        {organization, action: "created"|"matched", promoted?, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            org, action, promoted = await store.find_or_create_organization(
                conn,
                sctx,
                name=name,
                domain=domain,
                website=website,
                industry=industry,
                region=region,
                source=source,
                fields=fields,
                fields_by=fields_by,
                external_refs=external_refs,
                limits=runtime().limits(sctx),
            )
    except (ConflictError, ValueError, TypeError) as exc:
        return await _handle_tool_error(
            sctx, entity="organization", action="find_or_create", exc=exc
        )
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="find_or_create_organization")
    if action == "created":
        runtime().record(PeopleEvent.ORGANIZATION_CREATED)
    next_step = (
        f"Add contacts with find_or_create_contact(organization_id={org.id!r}), "
        f"or set its fields with update_organization(organization_id={org.id!r}, fields=...)."
    )
    result: dict[str, Any] = {"organization": dump(org), "action": action, "next_step": next_step}
    if promoted:
        # Surface the discovered→active promotion so the caller sees the mutation, and
        # count it — a match-triggered promotion is as real as an explicit one.
        runtime().record(PeopleEvent.ORGANIZATION_PROMOTED)
        result["promoted"] = True
    await announce_resource_list_changed(ctx)
    return result


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def find_or_create_contact(
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    organization_id: str | None = None,
    organization_name: str | None = None,
    lead_source: str | None = None,
    source: str | None = None,
    status: str | None = None,
    email_status: EmailStatus | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    external_refs: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Find a contact, or create them when this workspace does not hold them yet.

    Call it whenever a person comes up that you may record or act on, with everything you
    know about them in one call. It matches by external reference, then email, then name
    within the same organization, and a match fills only the values the contact lacks. Never
    create a name-only contact and call again to add more: to change a value the contact
    holds use ``update_contact``, and to join two records of one person ``merge_contacts``.
    For a list of more than a few people use ``import_contacts``. Linking a person to a
    discovered lookalike organization promotes it to active.

    Args:
        name: The person's full name. Optional when email is given: a new contact is then
            named from the address (jane.doe@ becomes "Jane Doe"). Pass name or email.
        email: Their email address; the strongest match key after an external reference.
        phone: Their phone number.
        title: Their role at their organization, e.g. "VP Sales".
        organization_id: The organization they work at, by id.
        organization_name: The organization's name when you hold no id: it is found or
            created, and the contact is linked to it.
        lead_source: How they came to you (open text, e.g. "referral", "inbound", "event").
        source: Where you learned of them (open text, e.g. "research", "manual", "import").
        status: Their stage: sourced, enriched, contacted, engaged, or a stage of
            your own (default "sourced").
        email_status: Whether the address can receive mail. Omit when it has not been checked.
        fields: Custom field values by key, e.g. {"fit": "strong"}; list_fields lists the
            keys. A match keeps the values it holds and takes only the keys it lacks.
        fields_by: Who is setting these fields: an agent, a run, or a person.
        external_refs: What other systems call this record, as opaque URIs (e.g.
            "crm://people/123", or the source's own record URL). One a record here already
            holds is the match, ahead of every other key; one held by another record is
            refused as a conflict, never moved.

    Returns:
        {contact, action: "created"|"matched", org_promoted?, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            contact, action, org_promoted = await store.find_or_create_contact(
                conn,
                sctx,
                name=name,
                email=email,
                phone=phone,
                title=title,
                organization_id=organization_id,
                organization_name=organization_name,
                lead_source=lead_source,
                source=source,
                status=status,
                email_status=email_status,
                fields=fields,
                fields_by=fields_by,
                external_refs=external_refs,
                limits=runtime().limits(sctx),
            )
    except (OrganizationNotFoundError, ConflictError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="contact", action="find_or_create", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="find_or_create_contact")
    if action == "created":
        runtime().record(PeopleEvent.CONTACT_CREATED)
    next_step = (
        f"Log an interaction with log_interaction(contact_id={contact.id!r}, ...), "
        f"or set their fields with update_contact(contact_id={contact.id!r}, fields=...)."
    )
    if action == "created" and not (name and name.strip()):
        next_step += (
            f" The name {contact.name!r} was derived from the email; correct it with "
            f"update_contact(contact_id={contact.id!r}, name=...) if you learn the real one."
        )
    result: dict[str, Any] = {"contact": dump(contact), "action": action, "next_step": next_step}
    if org_promoted:
        # Surface the wired org's discovered→active promotion, and count it uniformly
        # with explicit promotions.
        runtime().record(PeopleEvent.ORGANIZATION_PROMOTED)
        result["org_promoted"] = True
    await announce_resource_list_changed(ctx)
    return result


#: Postgres's SQLSTATE for a deadlock. Two imports whose rows share organizations can
#: take their locks in opposite orders; Postgres ends one, and running it again
#: converges, because an import is idempotent.
_DEADLOCK = "40P01"


async def _run_import(sctx: Any, run: Any) -> dict[str, Any]:
    """Run one import in its own session, once more if Postgres ended it for a deadlock."""
    for attempt in (1, 2):
        try:
            async with runtime().session(sctx) as conn:
                return await run(conn)
        except DBAPIError as exc:
            if attempt == 1 and getattr(exc.orig, "sqlstate", None) == _DEADLOCK:
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _import_next_step(result: dict[str, Any], noun: str, read: str) -> str:
    steps = []
    limited = [g for g in result["skipped"] if "limit" in g]
    if len(limited) < len(result["skipped"]):
        steps.append(
            "Fix the rows listed under skipped (indices into this call's rows) and import "
            "them again; the rest are saved."
        )
    if limited:
        steps.append(
            "The rows under skipped with a limit would pass this account's limits, and the "
            "rest are saved. " + store.limit_next_step(limited[0]["limit"])
        )
    if result["kept"]:
        steps.append(
            f"The rows under kept matched {noun} that hold a different value, which an "
            f"import never overwrites; change one with update_{noun[:-1]} if the row is right."
        )
    if not steps:
        steps.append("Every row is saved; importing the same rows again changes nothing.")
    return " ".join([*steps, f"Read the {noun} with {read}."])


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def import_organizations(
    rows: list[OrganizationRow],
    source: str | None = "import",
    fields_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Find or create many organizations in one call, from a list loaded from a file or another source.

    Call it for a list of more than a few companies; for one, use
    ``find_or_create_organization``. Each row is matched and filled the way that tool does it,
    so importing the same list again saves nothing new and never overwrites a later edit. A
    row that fails is skipped and reported with its reason and index; every other row is
    saved. A matched row whose value differs from the one held is reported under ``kept``.
    At most 500 rows per call; split a longer list.

    Args:
        rows: Up to 500 companies, one object per row. A row needs a name, a domain, or an
            external reference a record here holds. Values are text; an empty string counts
            as absent.
        source: Where the list came from, recorded on each organization it creates (default
            "import").
        fields_by: Who is setting the rows' custom fields: an agent, a run, or a person.

    Returns:
        {received, created, matched, promoted, kept, skipped, next_step}.
    """
    sctx = runtime().identity()
    try:
        result = await _run_import(
            sctx,
            lambda conn: store.import_organizations(
                conn,
                sctx,
                rows=rows,
                source=source,
                fields_by=fields_by,
                limits=runtime().limits(sctx),
            ),
        )
    except ValueError as exc:
        return await _handle_tool_error(sctx, entity="organization", action="import", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="import_organizations")
    runtime().record(PeopleEvent.ORGANIZATION_CREATED, result["created"])
    runtime().record(PeopleEvent.ORGANIZATION_PROMOTED, result["promoted"])
    _record_limited(result["skipped"])
    if result["created"] or result["matched"]:
        await announce_resource_list_changed(ctx)
    return {**result, "next_step": _import_next_step(result, "organizations", "list_organizations")}


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def import_contacts(
    rows: list[ContactRow],
    source: str | None = "import",
    fields_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Find or create many contacts in one call, from a list loaded from a file or another source.

    Call it for a list of more than a few people; for one, use ``find_or_create_contact``.
    Each row is matched and filled the way that tool does it, so importing the same list again
    saves nothing new. A row's ``organization_domain`` names its organization, which is found
    or created. A row that fails is skipped and reported with its reason and index; every
    other row is saved. A matched row whose value differs from the one held is reported under
    ``kept``. At most 500 rows per call; split a longer list.

    Args:
        rows: Up to 500 people, one object per row. A row needs a name, an email, or an
            external reference a record here holds. Values are text; an empty string counts
            as absent.
        source: Where the list came from, recorded on each contact and organization it
            creates (default "import").
        fields_by: Who is setting the rows' custom fields: an agent, a run, or a person.

    Returns:
        {received, created, matched, promoted, kept, skipped, next_step}.
    """
    sctx = runtime().identity()
    try:
        result = await _run_import(
            sctx,
            lambda conn: store.import_contacts(
                conn,
                sctx,
                rows=rows,
                source=source,
                fields_by=fields_by,
                limits=runtime().limits(sctx),
            ),
        )
    except ValueError as exc:
        return await _handle_tool_error(sctx, entity="contact", action="import", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="import_contacts")
    runtime().record(PeopleEvent.CONTACT_CREATED, result["created"])
    runtime().record(PeopleEvent.ORGANIZATION_PROMOTED, result["promoted"])
    _record_limited(result["skipped"])
    if result["created"] or result["matched"]:
        await announce_resource_list_changed(ctx)
    return {**result, "next_step": _import_next_step(result, "contacts", "list_contacts")}


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_contact(
    contact_id: str,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    organization_id: str | None = None,
    lead_source: str | None = None,
    status: str | None = None,
    source: str | None = None,
    email_status: EmailStatus | None = None,
    email_checked_at: datetime | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    add_external_refs: list[str] | None = None,
    remove_external_refs: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change a contact you hold: their fields, custom fields or external references.

    Call it to correct or add to a contact once you have their id. Only the arguments you
    pass change; one you omit keeps its value, and a core field cannot be cleared here. To
    record that you spoke with them use ``log_interaction`` instead: setting status records no
    touch. To join a duplicate use ``merge_contacts``; for someone you may not hold yet,
    ``find_or_create_contact``.

    Args:
        contact_id: The contact to change.
        name: Their full name. Omit to keep it.
        email: Their email address. Changing it clears email_status unless you grade the new
            address in the same call.
        phone: Their phone number. Omit to keep it.
        title: Their role at their organization. Omit to keep it.
        organization_id: The organization they now work at. Interactions already logged stay
            with the organization they were logged under.
        lead_source: How they came to you (open text). Omit to keep it.
        status: Their stage: sourced, enriched, contacted, engaged, or a stage of
            your own. Setting it records no touch; log_interaction does.
        source: Where you learned of them (open text). Omit to keep it.
        email_status: Whether the address can receive mail.
        email_checked_at: When the address was checked (ISO-8601); defaults to now.
        fields: Custom field values by key. A key given replaces that key's value; null (or ""
            for a text field) removes it; keys not given are kept. To keep the reason with a
            value, pass {"value": ..., "why": "..."}. Keys must be defined (list_fields).
        fields_by: Who is setting these fields: an agent, a run, or a person.
        add_external_refs: References (opaque URIs) to attach. One another record holds is
            refused as a conflict, never moved.
        remove_external_refs: References this contact holds to detach, such as one attached to
            the wrong person.

    Returns:
        {contact, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            contact = await store.update_contact(
                conn,
                sctx,
                contact_id=contact_id,
                name=name,
                email=email,
                phone=phone,
                title=title,
                organization_id=organization_id,
                lead_source=lead_source,
                status=status,
                source=source,
                email_status=email_status,
                email_checked_at=email_checked_at,
                fields=fields,
                fields_by=fields_by,
                add_external_refs=add_external_refs,
                remove_external_refs=remove_external_refs,
            )
    except (
        ContactNotFoundError,
        OrganizationNotFoundError,
        ConflictError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="contact", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_contact")
    await announce_resource_list_changed(ctx)
    return {
        "contact": dump(contact),
        "next_step": (
            f"Review the contact with get_contact(contact_id={contact.id!r}) or its "
            f"history with list_interactions(contact_id={contact.id!r})."
        ),
    }


@mcp.tool(annotations=MERGE, meta=CHATGPT_TOOL_META)
async def merge_contacts(
    target_id: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict:
    """Merge a duplicate contact into the one to keep, so two records of one person become one.

    ALWAYS CONFIRM WITH THE USER BEFORE CALLING THIS. Show them both records, say which will
    be kept (``target_id``) and which merged away (``source_id``), and call this only after
    they say to proceed; never pick the duplicate on your own judgment. The kept contact takes
    every interaction, signal and external reference of the other, and every deal naming the
    other as its contact; its empty fields are filled from it, and a value it holds is never
    overwritten. The merged-away record is archived, not
    deleted (``archive_contact(archived=false)`` restores it as a record of its own). Use this only for duplicates that already exist (``find_or_create_contact``
    avoids making them), and never rename a duplicate instead: its interactions would stay on
    the wrong record.

    Args:
        target_id: The contact to keep.
        source_id: The duplicate to merge into it and archive.

    Returns:
        {contact, interactions_moved, signals_moved, opportunities_moved, fields_filled,
        next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            contact, summary = await store.merge_contacts(
                conn, sctx, target_id=target_id, source_id=source_id
            )
    except (ContactNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="contact", action="merge", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="merge_contacts")
    await announce_resource_list_changed(ctx)
    return {
        "contact": dump(contact),
        "interactions_moved": summary["interactions_moved"],
        "signals_moved": summary["signals_moved"],
        "opportunities_moved": summary["opportunities_moved"],
        "fields_filled": summary["fields_filled"],
        "next_step": (
            f"Review the merged contact with get_contact(contact_id={contact.id!r}) or its "
            f"combined history with list_interactions(contact_id={contact.id!r})."
        ),
    }


@mcp.tool(annotations=MERGE, meta=CHATGPT_TOOL_META)
async def merge_organizations(
    target_id: str,
    source_id: str,
    ctx: Context | None = None,
) -> dict:
    """Merge a duplicate organization into the one to keep, so two records of one company become one.

    ALWAYS CONFIRM WITH THE USER BEFORE CALLING THIS. Show them both records, say which will
    be kept (``target_id``) and which merged away (``source_id``), and call this only after
    they say to proceed. The kept organization takes every contact, interaction, deal,
    signal, proof point, external reference and lookalike of the other, and the signals
    filed as evidence for the other; its empty fields are filled from it, and a value it
    holds is never overwritten. A signal both hold (the same source URL and type) is kept
    once, with the purposes of both. The merged-away record is archived, not deleted.
    ``find_or_create_organization`` already prevents new duplicates by domain and name, so use
    this for two that exist, such as two domains that turn out to be one company. Never
    rename a duplicate instead: its contacts would stay on the wrong record.

    Args:
        target_id: The organization to keep.
        source_id: The duplicate to merge into it and archive.

    Returns:
        {organization, contacts_moved, interactions_moved, opportunities_moved, signals_moved,
        signals_dropped, proof_points_moved, evidence_moved, edges_moved, edges_dropped,
        fields_filled, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            org, summary = await store.merge_organizations(
                conn, sctx, target_id=target_id, source_id=source_id
            )
    except (OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="merge", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="merge_organizations")
    await announce_resource_list_changed(ctx)
    return {
        "organization": dump(org),
        **{
            k: summary[k]
            for k in (
                "contacts_moved",
                "interactions_moved",
                "opportunities_moved",
                "signals_moved",
                "signals_dropped",
                "proof_points_moved",
                "evidence_moved",
                "edges_moved",
                "edges_dropped",
                "fields_filled",
            )
        },
        "next_step": (
            f"Review the merged organization with get_organization(organization_id={org.id!r}) "
            f"or its combined history with list_interactions(organization_id={org.id!r})."
        ),
    }


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def archive_contact(
    contact_id: str, archived: bool = True, ctx: Context | None = None
) -> dict:
    """Archive a contact: hide them from the default lists and counts, reversibly. Or restore one.

    Call it when the user is done with someone but may want them back: a lead that went
    nowhere, a person who left the company, a stale record. Nothing is deleted: the contact,
    their interactions and their stage stay as they are, ``get_contact`` still reads them, and
    ``list_contacts(include_archived=true)`` lists them. ``archived=false`` restores one.
    Archiving is not a stage: to record how far along someone is, set ``status`` with
    ``update_contact``. To remove a person for good, only when the user asks, use
    ``delete_contact``; to join two records of one person, ``merge_contacts``.

    Args:
        contact_id: The contact to archive or restore.
        archived: true archives the contact (default); false restores them. A repeat changes
            nothing.

    Returns:
        {contact, next_step}.
    """
    return await _archive(
        "contact", contact_id, archived, ctx, tool="archive_contact", lister="list_contacts"
    )


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def archive_organization(
    organization_id: str, archived: bool = True, ctx: Context | None = None
) -> dict:
    """Archive an organization: hide it from the default lists and counts, reversibly. Or restore one.

    Call it when the user is done with a company but may want it back: a lost account, a
    company that no longer fits. Nothing is deleted: the organization, its contacts, deals
    and history stay as they are, ``get_organization`` still reads it, and
    ``list_organizations(include_archived=true)`` lists it. Its contacts are not archived
    with it. ``archived=false`` restores one. To remove a company for good, only when the
    user asks, use ``delete_organization``; to join two records of one company,
    ``merge_organizations``.

    Args:
        organization_id: The organization to archive or restore.
        archived: true archives the organization (default); false restores it. A repeat
            changes nothing.

    Returns:
        {organization, next_step}.
    """
    return await _archive(
        "organization",
        organization_id,
        archived,
        ctx,
        tool="archive_organization",
        lister="list_organizations",
    )


async def _archive(
    entity: str, record_id: str, archived: bool, ctx: Context | None, *, tool: str, lister: str
) -> dict:
    """Archive or restore one contact or organization, as ``archive_*`` does."""
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            record = await store.archive_record(
                conn, sctx, entity=entity, record_id=record_id, archived=archived
            )
    except (ContactNotFoundError, OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(
            sctx, entity=entity, action="archive" if archived else "restore", exc=exc
        )
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where=tool)
    await announce_resource_list_changed(ctx)
    if archived:
        next_step = (
            f"It is hidden from the default lists; {lister}(include_archived=true) shows it, "
            f"and {tool}({entity}_id={record.id!r}, archived=false) restores it."
        )
    else:
        next_step = f"It is back in the default lists, as {lister} shows."
    return {entity: dump(record), "next_step": next_step}


@mcp.tool(annotations=REMOVE, meta=CHATGPT_TOOL_META)
async def delete_contact(
    contact_id: str,
    confirm: str,
    ctx: Context | None = None,
) -> dict:
    """Delete a contact so that no read, listing or lookup returns them again.

    ALWAYS CONFIRM WITH THE USER BEFORE CALLING THIS. There is no undo. Call it only when the
    user asks for the person to be removed; to hide a contact they may want back, use
    ``archive_contact`` instead, which is reversible, and to join two records of one person use
    ``merge_contacts``. Show the user the contact, say it will be deleted, and call this only
    after they say to proceed. It deletes the contact, any duplicates already merged into it,
    their interactions, the signals about them and their external references. A deal that had
    them as its primary contact reads as having none. Their email is free for a new contact.

    Args:
        contact_id: The contact to delete.
        confirm: The same contact id again, exactly: a guard against deleting a misread id.
            Anything else deletes nothing.

    Returns:
        {deleted_id, deleted: {table: count}, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            summary = await store.delete_contact(conn, sctx, contact_id=contact_id, confirm=confirm)
    except (ContactNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="contact", action="delete", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="delete_contact")
    await announce_resource_list_changed(ctx)
    return {
        "deleted_id": contact_id,
        **summary,
        "next_step": (
            "Tell the user the contact is deleted. If a deal had them as its primary contact, "
            "set a new one with update_opportunity(primary_contact_id=...)."
        ),
    }


@mcp.tool(annotations=REMOVE, meta=CHATGPT_TOOL_META)
async def delete_organization(
    organization_id: str,
    confirm: str,
    delete_opportunities: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Delete an organization so that no read, listing or lookup returns it again.

    ALWAYS CONFIRM WITH THE USER BEFORE CALLING THIS. There is no undo. Call it only when the
    user asks for the company to be removed; to join two records of one company use
    ``merge_organizations``. Show the user the organization, say it will be deleted, and call
    this only after they say to proceed. It is refused while any contact belongs to the
    organization (delete each with ``delete_contact`` or move them with ``update_contact``),
    and while any deal names it unless ``delete_opportunities`` is true. It deletes the
    organization, its merged duplicates, signals, proof points and external references. Its
    domain is free for a new organization.

    Args:
        organization_id: The organization to delete.
        confirm: The same organization id again, exactly: a guard against deleting a misread
            id. Anything else deletes nothing.
        delete_opportunities: Also delete the deals that name it (default false, which
            refuses while any exist). A deal is the user's own record, so ask first.

    Returns:
        {deleted_id, deleted: {table: count}, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            summary = await store.delete_organization(
                conn,
                sctx,
                organization_id=organization_id,
                confirm=confirm,
                delete_opportunities=delete_opportunities,
            )
    except (OrganizationNotFoundError, ConflictError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="delete", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="delete_organization")
    await announce_resource_list_changed(ctx)
    return {
        "deleted_id": organization_id,
        **summary,
        "next_step": "Tell the user the organization is deleted.",
    }


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_organization(
    organization_id: str,
    name: str | None = None,
    domain: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    source: str | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    add_external_refs: list[str] | None = None,
    remove_external_refs: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change an organization you hold: its fields, custom fields or external references.

    Call it to correct or add to an organization once you have its id. Only the arguments you
    pass change; one you omit keeps its value, and a core field cannot be cleared here.
    Editing a discovered lookalike leaves it discovered: ``promote_organization`` is the
    decision to work it. For a company you may not hold yet use
    ``find_or_create_organization``; to join a duplicate, ``merge_organizations``.

    Args:
        organization_id: The organization to change.
        name: Its name. Omit to keep it.
        domain: Its web domain. Omit to keep it.
        website: Its website URL. Omit to keep it.
        industry: Its industry (open text). Omit to keep it.
        region: Where it is based or operates (open text). Omit to keep it.
        source: Where you learned of it (open text). Omit to keep it.
        fields: Custom field values by key. A key given replaces that key's value; null (or ""
            for a text field) removes it; keys not given are kept. To keep the reason with a
            value, pass {"value": ..., "why": "..."}. Keys must be defined (list_fields).
        fields_by: Who is setting these fields: an agent, a run, or a person.
        add_external_refs: References (opaque URIs) to attach. One another record holds is
            refused as a conflict, never moved.
        remove_external_refs: References this organization holds to detach, such as one
            attached to the wrong company.

    Returns:
        {organization, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            org = await store.update_organization(
                conn,
                sctx,
                org_id=organization_id,
                name=name,
                domain=domain,
                website=website,
                industry=industry,
                region=region,
                source=source,
                fields=fields,
                fields_by=fields_by,
                add_external_refs=add_external_refs,
                remove_external_refs=remove_external_refs,
            )
    except (OrganizationNotFoundError, ConflictError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_organization")
    await announce_resource_list_changed(ctx)
    return {
        "organization": dump(org),
        "next_step": (
            f"Review the organization with get_organization(organization_id={org.id!r}) or "
            f"its history with list_interactions(organization_id={org.id!r})."
        ),
    }


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def log_interaction(
    contact_id: str,
    kind: str,
    summary: str,
    body: str | None = None,
    direction: Direction | None = None,
    occurred_at: datetime | None = None,
    follow_up_at: datetime | None = None,
    opportunity_id: str | None = None,
    idempotency_key: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Record a touch with a contact: a meeting, email, call, note or piece of research.

    Call it after anything happens with a person that the next agent should know: a message
    sent or received, a call, a meeting, or research done on them. It is also filed under the
    organization the contact works at now, and under a deal when you give ``opportunity_id``.
    Give a direction on an email, call or meeting so the contact's stage advances (outbound to
    contacted, inbound to engaged). For lasting research about a person or company rather
    than one event, set a field with ``update_contact`` or ``update_organization``, or record a
    signal.

    Args:
        contact_id: The contact it was with.
        kind: What kind of touch: meeting, email, call, note or research, or your own word.
        summary: One line saying what happened.
        body: Longer markdown detail: notes, context, excerpts.
        direction: For an email, call or meeting, whether it came from them or went to them.
            Omit it for a note or research. A touch with none does not advance the stage.
        occurred_at: When it happened (ISO-8601); defaults to now.
        follow_up_at: When to follow up with the contact (ISO-8601).
        opportunity_id: The deal this touch is part of, so it shows in that deal's history.
        idempotency_key: A token of your choosing that makes a retry safe: a repeat with the
            same key for the same contact returns the interaction the first call logged.
            Without it every call logs a new one.

    Returns:
        {interaction, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            interaction = await store.log_interaction(
                conn,
                sctx,
                contact_id=contact_id,
                kind=kind,
                summary=summary,
                body=body,
                direction=direction,
                occurred_at=occurred_at,
                follow_up_at=follow_up_at,
                opportunity_id=opportunity_id,
                idempotency_key=idempotency_key,
                limits=runtime().limits(sctx),
            )
    except (ContactNotFoundError, OpportunityNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="interaction", action="log", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="log_interaction")
    runtime().record(PeopleEvent.INTERACTION_LOGGED)
    await announce_resource_list_changed(ctx)
    return {
        "interaction": dump(interaction),
        "next_step": (
            f"See the full history with list_interactions(contact_id={contact_id!r}), "
            "or set a follow-up by logging another interaction with follow_up_at."
        ),
    }


# --------------------------------------------------------------------------- #
# The pipeline                                                                  #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("opportunities"))
async def get_pipeline(
    organization_id: str | None = None,
    include_retired: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Read this workspace's deal pipeline: its stages in order, the deals in each, and the totals.

    Call it before moving or creating a deal, since ``move_opportunity`` and
    ``create_opportunity`` take a stage key from this list and refuse any other, and to
    answer how the pipeline stands (how many deals per stage, their value, how many won
    or lost). Each stage carries its key, label, default win probability, outcome (open,
    won or lost) and its deals' count, value and weighted value. To read the deals
    themselves, use ``list_opportunities(stage=...)``.

    A host that shows views opens the pipeline board for this call, so call it, not
    ``list_opportunities``, to show the user their deals or pipeline.

    Args:
        organization_id: Count one account's deals only. Omit for the whole workspace.
        include_retired: Also list retired stages, after the live ones, each with its
            replacement. No deal enters a retired stage; the totals count its deals either way.

    Returns:
        {stages: [{key, label, position, probability, outcome, deals, value_sum?,
        weighted_sum?, retired_at?, replaced_by?}], open_count, won_count, lost_count,
        total_value?, total_weighted?, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            pipeline = await store.get_pipeline(
                conn, sctx, organization_id=organization_id, include_retired=include_retired
            )
    except (OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="opportunity", action="pipeline", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_pipeline")
    return {
        **pipeline,
        "next_step": (
            "Move a deal with move_opportunity(opportunity_id=..., stage=<key>); list one "
            "stage's deals with list_opportunities(stage=<key>)."
        ),
    }


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def define_pipeline_stage(
    key: str,
    label: str,
    position: int | None = None,
    probability: Probability | None = None,
    outcome: Outcome | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add a stage to this workspace's deal pipeline.

    Call it when the user's sales process has a step the pipeline lacks (get_pipeline
    lists the stages). It only adds: a key the pipeline already holds is returned as
    it is and nothing changes, so to rename, reorder, reweight or change a stage's
    outcome use ``update_pipeline_stage``. The new stage lands last unless
    ``position`` says where, with probability 0 and outcome open unless given.

    Args:
        key: Stable identifier: lowercase letters, digits, underscores (e.g. "proposal").
            Deals store it, so choose it to last.
        label: Display label (e.g. "Proposal sent").
        position: Order on the board, 0 first. Omit to add it last.
        probability: Default win probability 0-100 for a deal entering the stage
            (default 0).
        outcome: open / won / lost: whether entering the stage closes a deal (default open).

    Returns:
        {stage, action: "created"|"matched", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            stage, action = await store.define_pipeline_stage(
                conn,
                sctx,
                key=key,
                label=label,
                position=position,
                probability=probability,
                outcome=outcome,
                limits=runtime().limits(sctx),
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="pipeline_stage", action="define", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="define_pipeline_stage")
    await announce_resource_list_changed(ctx)
    if action == "created":
        next_step = (
            f"Move deals into it with move_opportunity(opportunity_id=..., stage={stage.key!r})."
        )
    else:
        next_step = (
            f"The pipeline already holds {stage.key!r}; it was left as it is. Change it with "
            f"update_pipeline_stage(key={stage.key!r}, ...)."
        )
    return {"stage": dump(stage), "action": action, "next_step": next_step}


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_pipeline_stage(
    key: str,
    label: str | None = None,
    position: int | None = None,
    probability: Probability | None = None,
    outcome: Outcome | None = None,
    retired: bool | None = None,
    replaced_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Retire, restore or change a stage of this workspace's deal pipeline: its label, order, default probability or outcome.

    Call it to rename a stage, move its column, reweight it, make it close deals as won
    or lost, retire a stage the sales process no longer uses (naming the stage that
    replaces it), or restore a retired one. Only the arguments given change; the key
    never does, so deals in the stage stay in it. A deal in the stage takes the new
    default probability unless it has its own, and a stage that starts or stops closing
    deals moves their close time with it. A retired stage leaves get_pipeline and the
    board, and creating or moving a deal into it is refused with its replacement named;
    its deals keep it, with its label and outcome, and its key stays taken. Retiring is
    refused while open deals sit in the stage; a won or lost stage retires with its
    deals. The pipeline's only open stage cannot stop being open or retire. To add a
    stage use ``define_pipeline_stage``; to move one deal use ``move_opportunity``.

    Args:
        key: The stage's key (get_pipeline lists them; include_retired=true for retired ones).
        label: New display label. Omit to keep it.
        position: New order on the board, 0 first; the other stages shift. Omit to keep it.
        probability: New default win probability 0-100. Omit to keep it.
        outcome: open / won / lost. Omit to keep it.
        retired: true retires the stage; false restores it, last on the board unless
            position is given, and clears replaced_by.
        replaced_by: The key of the live stage that takes its place. Only on a retired stage.

    Returns:
        {stage, action: "retired"|"restored"|"updated"|"unchanged", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            stage, action = await store.update_pipeline_stage(
                conn,
                sctx,
                key=key,
                label=label,
                position=position,
                probability=probability,
                outcome=outcome,
                retired=retired,
                replaced_by=replaced_by,
                limits=runtime().limits(sctx),
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="pipeline_stage", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_pipeline_stage")
    await announce_resource_list_changed(ctx)
    if stage.retired_at is None:
        next_step = "get_pipeline() shows the pipeline as it now stands."
    else:
        next_step = (
            "get_pipeline(include_retired=true) shows it; restore it with "
            f"update_pipeline_stage(key={stage.key!r}, retired=false)."
        )
    return {"stage": dump(stage), "action": action, "next_step": next_step}


# --------------------------------------------------------------------------- #
# Custom fields                                                                 #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def define_field(
    entity: Entity,
    key: str,
    label: str,
    type: FieldType,  # noqa: A002 - the wire name; a field's type is what callers call it
    options: list[str] | None = None,
    description: str | None = None,
    defined_by: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    ctx: Context | None = None,
) -> dict:
    """Define a custom field on contacts, organizations or deals, so it can be set, filtered, sorted and shown as a column.

    Call it when the user wants to track something list_fields does not have: a fit grade, an
    audience, a headcount. Then write values with ``update_contact``, ``update_organization``
    or ``update_opportunity`` (``fields={key: value}``). Put a fact about a company on the
    organization, once, never on each of its contacts. Defining a key that exists returns it,
    adding any new options and filling an empty description; it never changes the label,
    type, range or removes an option, which ``update_field`` does.

    Args:
        entity: What the field describes.
        key: Stable identifier: lowercase letters, digits and underscores (e.g. "fit"). Writes,
            filters and other systems name it, so choose it to last.
        label: The name people see, e.g. "Fit".
        type: What the field holds.
        options: The allowed values, required for select and multi_select. Matched without
            regard to case on write.
        description: What the field means and how to fill it, written for the agent that will
            grade the next thousand records.
        defined_by: Who is defining it: an agent, a run, or a person.
        minimum: For a number field, the lowest value it accepts, inclusive (e.g. 1 for a 1-5
            score).
        maximum: For a number field, the highest value it accepts, inclusive.

    Returns:
        {field, action: "created"|"matched", options_added?, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            field, action, options_added = await store.define_field(
                conn,
                sctx,
                entity=entity,
                key=key,
                label=label,
                field_type=type,
                options=options,
                description=description,
                defined_by=defined_by,
                minimum=minimum,
                maximum=maximum,
                limits=runtime().limits(sctx),
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="field_def", action="define", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="define_field")
    result: dict[str, Any] = {
        "field": dump(field),
        "action": action,
        "next_step": (
            f"Set it with update_{field.entity}({field.entity}_id=..., "
            f"fields={{{field.key!r}: ...}})."
        ),
    }
    if options_added:
        result["options_added"] = options_added
    await announce_resource_list_changed(ctx)
    return result


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_field(
    entity: Entity,
    key: str,
    retired: bool | None = None,
    replaced_by: str | None = None,
    default_column: bool | None = None,
    label: str | None = None,
    description: str | None = None,
    remove_options: list[str] | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    clear_bounds: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Retire, restore or edit a custom field: its label, description, unused options, range, or whether it opens as a column.

    Call it when a field is replaced by another (retire it, naming the replacement), when a
    retired field is needed again (restore it), or to rename, redescribe or narrow one. A
    retired field leaves list_fields, listings and pickers, and a write, filter, sort or
    column naming it is refused with its replacement named; its values stay on the records
    and its key stays taken. To add a field use ``define_field``. Repeating a retire keeps the
    first retired_at.

    Args:
        entity: The field's entity.
        key: The field's key.
        retired: true retires the field; false restores it and clears replaced_by.
        replaced_by: The live field that takes its place: a key on the same entity, or
            "organization.<key>" / "contact.<key>". Only on a retired field.
        default_column: true opens the live field as a column in its lists; false stops that.
        label: The name people see. Its key never changes.
        description: What the field means and how to fill it. "" clears it.
        remove_options: Options of a select or multi_select field to drop. Refused while any
            record holds one, and when none would remain.
        minimum: The lowest value a number field accepts, inclusive. Refused while a record
            holds a lower value.
        maximum: The highest value a number field accepts, inclusive. Refused while a record
            holds a higher value.
        clear_bounds: true lifts the field's range before any minimum or maximum given applies,
            so a side not given becomes unbounded.

    Returns:
        {field, action: "retired"|"restored"|"updated"|"unchanged", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            field, action = await store.update_field(
                conn,
                sctx,
                entity=entity,
                key=key,
                retired=retired,
                replaced_by=replaced_by,
                default_column=default_column,
                label=label,
                description=description,
                remove_options=remove_options,
                minimum=minimum,
                maximum=maximum,
                clear_bounds=clear_bounds,
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="field_def", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_field")
    if field.retired_at is None:
        next_step = (
            f"Set it with update_{field.entity}({field.entity}_id=..., "
            f"fields={{{field.key!r}: ...}})."
        )
    else:
        next_step = (
            f"list_fields(entity={field.entity!r}, include_retired=true) shows it; "
            f"restore it with update_field(entity={field.entity!r}, key={field.key!r}, "
            "retired=false)."
        )
    await announce_resource_list_changed(ctx)
    return {"field": dump(field), "action": action, "next_step": next_step}


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_fields(
    entity: Entity | None = None, include_retired: bool = False, ctx: Context | None = None
) -> dict:
    """List the fields of contacts, organizations and deals: the core ones and this workspace's custom ones.

    Read it before writing ``fields={...}``, filtering, sorting or counting, to learn which
    keys exist, their types and options, and what each one means. Every key it lists is a
    path the list tools and count_records take, and a contact also reaches its
    organization's as ``organization.<key>``. When the field you need is missing and you
    cannot add it with ``define_field``, tell the user which field is missing rather than
    storing the value in another field.

    Args:
        entity: Only this entity's fields. Omit for all three.
        include_retired: Also list retired custom fields, each with its replacement. A retired
            key cannot be written.

    Returns:
        {fields: {<entity>: {core, custom, rollups}}, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            defined = await store.describe_fields(
                conn, sctx, entity=entity, include_retired=include_retired
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="field_def", action="list", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_fields")
    return {
        "fields": {
            name: {
                "core": [
                    {"key": key, "type": field_type, "description": description}
                    for key, field_type, description in CORE_FIELDS[name]
                ],
                "custom": [dump(field) for field in custom],
                "rollups": store.rollups(name),
            }
            for name, custom in defined.items()
        },
        "next_step": (
            "Set a custom field with update_contact / update_organization(fields={key: "
            "value}); add one with define_field(entity=..., key=..., label=..., type=...). "
            "Filter and sort on any of these with list_contacts / list_organizations("
            "filter=..., sort=...), and see how a field's values divide with count_records."
        ),
    }


# --------------------------------------------------------------------------- #
# Reads                                                                         #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("contacts"))
async def get_contact(contact_id: str, ctx: Context | None = None) -> dict:
    """Read one contact in full: fields, counts, external references, the 5 newest interactions and open deals.

    Call it when you hold a contact's id and need what is known about them before you act:
    before writing to them, before a call, or to answer a question about them. The 5 newest
    interactions come without their bodies; ``list_interactions(contact_id=...)`` pages
    through all of them in full. The 5 newest open deals naming them as their contact come
    with their title, stage and value; ``list_opportunities(primary_contact_id=...)`` lists
    every one. To find a contact whose id you do not hold, use
    ``list_contacts(query=...)``.

    A host that shows views opens this contact for this call, with its open deals.

    Args:
        contact_id: The contact's id (ct_...).

    Returns:
        {contact, counts, recent_interactions, open_opportunities, open_opportunities_total,
        external_refs, external_refs_total, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            contact, counts = await store.get_contact(conn, sctx, contact_id=contact_id)
            recent, _ = await store.list_interactions(
                conn, sctx, contact_id=contact.id, limit=RECENT_INTERACTIONS
            )
            refs, refs_total = await store.external_refs_of(
                conn, entity="contact", record_id=contact.id
            )
            deals = await _open_deals(conn, sctx, primary_contact_id=contact.id)
    except ContactNotFoundError as exc:
        return await _handle_tool_error(sctx, entity="contact", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_contact")
    return {
        "contact": dump(contact),
        "counts": counts,
        "recent_interactions": _recent(recent),
        "open_opportunities": deals.rows,
        "open_opportunities_total": deals.total,
        "external_refs": refs,
        "external_refs_total": refs_total,
        "next_step": (
            f"Read its whole history with list_interactions(contact_id={contact.id!r}), or "
            f"update it with update_contact(contact_id={contact.id!r}, ...)."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def resolve_external_refs(refs: list[str], ctx: Context | None = None) -> dict:
    """Say which of a batch of other systems' references this workspace already holds, and on which records.

    A reference is what another system calls a record (``crm://companies/123``). Call it
    before pulling records from a source: what comes back under ``found`` is already here,
    and only what is ``missing`` is new, so you fetch and import only those, giving each row
    its reference. One call answers the whole batch. It finds by reference only; to find a
    record by name use ``list_contacts`` or ``list_organizations`` with ``query``.

    Args:
        refs: Up to 500 references (opaque URIs). A malformed one is refused, not reported
            missing.

    Returns:
        {found: [{ref, entity, id, uri, name, status}], missing: [ref], next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            result = await store.find_by_external_refs(conn, sctx, refs=refs)
    except ValueError as exc:
        return await _handle_tool_error(sctx, entity="organization", action="resolve_refs", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="resolve_external_refs")
    found = [{**f, "uri": record_uri(f["id"])} for f in result["found"]]
    return {
        "found": found,
        "missing": result["missing"],
        "next_step": (
            f"{len(found)} held, {len(result['missing'])} new. Read a held record with "
            "get_organization or get_contact; load the new ones with import_organizations or "
            "import_contacts, giving each row its reference."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("organizations"))
async def get_organization(organization_id: str, ctx: Context | None = None) -> dict:
    """Read one organization in full: fields, counts, external references, the 5 newest interactions and open deals.

    Call it when you hold an organization's id and need what is known about the company
    before you act or answer. The 5 newest interactions, with anyone there, come without
    their bodies; ``list_interactions(organization_id=...)`` pages through all of them, and
    ``list_contacts(organization_id=...)`` lists its people. The 5 newest open deals on the
    account come with their title, stage and value;
    ``list_opportunities(organization_id=...)`` lists every one. To find an organization whose id
    you do not hold, use ``list_organizations(query=...)``.

    A host that shows views opens this organization for this call, with its open deals.

    Args:
        organization_id: The organization's id (org_...).

    Returns:
        {organization, counts, recent_interactions, open_opportunities,
        open_opportunities_total, external_refs, external_refs_total, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            org, counts = await store.get_organization(conn, sctx, org_id=organization_id)
            recent, _ = await store.list_interactions(
                conn, sctx, organization_id=org.id, limit=RECENT_INTERACTIONS
            )
            refs, refs_total = await store.external_refs_of(
                conn, entity="organization", record_id=org.id
            )
            deals = await _open_deals(conn, sctx, organization_id=org.id)
    except OrganizationNotFoundError as exc:
        return await _handle_tool_error(sctx, entity="organization", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_organization")
    return {
        "organization": dump(org),
        "counts": counts,
        "recent_interactions": _recent(recent),
        "open_opportunities": deals.rows,
        "open_opportunities_total": deals.total,
        "external_refs": refs,
        "external_refs_total": refs_total,
        "next_step": (
            f"List its people with list_contacts(organization_id={org.id!r}), or read its "
            f"whole history with list_interactions(organization_id={org.id!r})."
        ),
    }


def _list_error(exc: Exception) -> dict[str, Any]:
    """The tool result for a bad cursor, filter, sort or column list."""
    if isinstance(exc, FieldError):
        # A filter refusal names what would have worked: the fields, the operators, the options.
        return _invalid(str(exc), exc.next_step)
    if isinstance(exc, InvalidCursor):
        return _invalid(str(exc), _FIX_CURSOR)
    return _invalid(
        str(exc),
        "Correct the argument the message names; list_fields lists the paths a filter, sort, "
        "column or count takes, and the server instructions the operators.",
    )


def _with_uri(row: dict[str, Any]) -> dict[str, Any]:
    """A projected listing row, with the record's address beside its id.

    A row built from a model carries ``uri`` already; a row the store projects to the
    asked-for columns is a plain dict, and the address is what lets the agent open it.
    """
    uri = record_uri(row["id"]) if isinstance(row.get("id"), str) else None
    return {**row, "uri": uri} if uri else row


def _page_result(key: str, tool: str, page: store.Page, *, open_with: str) -> dict[str, Any]:
    """The wire shape of one listing page. ``total`` is on the first page only."""
    rows = [_with_uri(row) if isinstance(row, dict) else dump(row) for row in page.rows]
    if page.next_cursor is not None:
        next_step = (
            f"Fetch the next page with {tool}(cursor={page.next_cursor!r}, ...) and the "
            "same filter, sort and columns."
        )
    else:
        next_step = (
            f"This is the last page. Read one in full with {open_with}; "
            "its uri is the record's address for citing it or opening it in a view."
        )
    result: dict[str, Any] = {key: rows, "next_cursor": page.next_cursor, "next_step": next_step}
    if page.total is not None:
        result["total"] = page.total
    return result


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("contacts"))
async def list_contacts(
    query: str | None = None,
    organization_id: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    include_archived: bool = False,
    filter: dict[str, Any] | None = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List contacts: find people by name or email, or pull exactly the ones a task needs, a page at a time.

    Call it to find a contact whose id you do not hold (``query``), or to read the people
    that match: by organization, stage, lead source, or a ``filter`` on any field, including
    their organization's (``organization.fit``) and the rollups ``signals.active``,
    ``interactions.count`` and ``interactions.last_at``. Pass ``columns`` when reading more
    than a page, and follow ``next_cursor`` for the rest. To learn how many fall in each
    group, use ``count_records`` instead of walking the list; to read one contact in full,
    ``get_contact``. Archived contacts are left out unless ``include_archived``.

    A host that shows views opens the contacts list for this call, so call it to show the
    user their contacts.

    Args:
        query: Free text matched against name and email: split into words, each of which must
            appear in one of them, in any order ("matt calloway" finds "Matthew Calloway").
        organization_id: Only the people at this organization.
        status: Only contacts at this stage.
        lead_source: Only contacts with this lead source.
        include_archived: Also list archived contacts (default false). Filter on
            archived_at to list only them.
        filter: Rows to keep, as {"field": path, "op": op, "value": v}, or {"and": [...]} /
            {"or": [...]} of those. The server instructions list the operators for each field
            type; list_fields lists the paths.
        sort: A field path to order by, "-" first for descending (e.g. "-created_at"). Rows
            with no value sort last. Default: newest first.
        columns: Return only these paths per row (plus id and uri), e.g. ["name", "title"].
            Without it each row is the whole contact.
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page. Pass the same query, filter, sort and
            columns with it.

    Returns:
        {contacts, total, next_cursor, next_step}; total is on the first page.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            page = await store.page_contacts(
                conn,
                sctx,
                terms=query,
                organization_id=organization_id,
                status=status,
                lead_source=lead_source,
                include_archived=include_archived,
                filter=filter,
                sort=sort,
                columns=columns,
                limit=limit,
                cursor=cursor,
            )
    except (ValueError, TypeError) as exc:
        # Bad cursor/filter → an error attempt (entity_id is unavailable here).
        await _audit_attempt(sctx, entity="contact", action="list", outcome="error")
        return _list_error(exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_contacts")
    return _page_result("contacts", "list_contacts", page, open_with="get_contact(contact_id=...)")


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("organizations"))
async def list_organizations(
    query: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    status: OrganizationStatus | None = None,
    include_archived: bool = False,
    filter: dict[str, Any] | None = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List organizations: find companies by name or domain, or pull exactly the ones a task needs, a page at a time.

    Call it to find an organization whose id you do not hold (``query``), or to read the
    companies that match: by industry, region, or a ``filter`` on any field, including the
    rollups ``contacts.count`` and ``signals.active``. Pass ``columns`` when reading more than
    a page, and follow ``next_cursor`` for the rest. To learn how many fall in each group, use
    ``count_records``; to read one organization in full, ``get_organization``. Only active
    organizations are listed unless status or a filter asks for discovered lookalikes, and
    archived ones only with ``include_archived``.

    A host that shows views opens the organizations list for this call, so call it to show
    the user their companies.

    Args:
        query: Free text matched against name and domain: split into words, each of which must
            appear in one of them, in any order.
        industry: Only organizations in this industry, matched exactly.
        region: Only organizations in this region, matched exactly.
        status: Which organizations to list (default active).
        include_archived: Also list archived organizations (default false). Filter on
            archived_at to list only them.
        filter: Rows to keep, as {"field": path, "op": op, "value": v}, or {"and": [...]} /
            {"or": [...]} of those. The server instructions list the operators for each field
            type; list_fields lists the paths.
        sort: A field path to order by, "-" first for descending (e.g. "-created_at"). Rows
            with no value sort last. Default: newest first.
        columns: Return only these paths per row (plus id and uri), e.g. ["name", "fit"].
            Without it each row is the whole organization.
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page. Pass the same query, filter, sort and
            columns with it.

    Returns:
        {organizations, total, next_cursor, next_step}; total is on the first page.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            page = await store.page_organizations(
                conn,
                sctx,
                terms=query,
                industry=industry,
                region=region,
                status=status,
                include_archived=include_archived,
                filter=filter,
                sort=sort,
                columns=columns,
                limit=limit,
                cursor=cursor,
            )
    except (ValueError, TypeError) as exc:
        # Bad cursor/filter → an error attempt (entity_id is unavailable here).
        await _audit_attempt(sctx, entity="organization", action="list", outcome="error")
        return _list_error(exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_organizations")
    return _page_result(
        "organizations",
        "list_organizations",
        page,
        open_with="get_organization(organization_id=...)",
    )


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def count_records(
    entity: Entity,
    field: str,
    filter: dict[str, Any] | None = None,  # noqa: A002 - the wire name
    limit: int = 25,
    ctx: Context | None = None,
) -> dict:
    """Count contacts, organizations or deals by one field's values, most common first.

    Call it to see how a workspace divides without walking it: how many contacts per
    audience or email status, how many organizations per ``fit``, how many open deals per
    stage. It takes the same ``filter`` as the list tools, so it also answers "of the
    strong-fit companies, how many per industry". Deals count open ones only, unless the
    filter names stage or outcome. To read the records behind a value, use the list tool
    with a filter on it.

    Args:
        entity: What to count.
        field: The field path to count by (list_fields lists them). Not a timestamp.
        filter: Rows to keep, as {"field": path, "op": op, "value": v}, or {"and": [...]} /
            {"or": [...]} of those. The server instructions list the operators for each field
            type; list_fields lists the paths.
        limit: How many values to return (default 25, at most 100).

    Returns:
        {field, values: [{value, count}], total, truncated, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            counted = await store.count_by(
                conn, sctx, entity=entity, field=field, filter=filter, limit=limit
            )
    except (ValueError, TypeError) as exc:
        counted_entity = entity.strip().lower()
        await _audit_attempt(
            sctx,
            entity=counted_entity if counted_entity in ("contact", "organization") else "field_def",
            action="count",
            outcome="error",
        )
        return _list_error(exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="count_records")
    lister = {"contact": "list_contacts", "opportunity": "list_opportunities"}.get(
        entity.strip().lower(), "list_organizations"
    )
    return {
        "field": field,
        **counted,
        "next_step": (
            f'List the rows behind a value with {lister}(filter={{"field": {field!r}, '
            '"op": "eq", "value": ...}).'
            + (
                " More values exist; narrow the filter or raise limit."
                if counted["truncated"]
                else ""
            )
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_interactions(
    contact_id: str | None = None,
    organization_id: str | None = None,
    opportunity_id: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List the interaction history of one contact, organization or deal, newest first, a page at a time.

    Call it to read what happened with someone before you reach out again, to answer
    "when did we last talk to them", or to page past the 5 newest interactions that
    get_contact, get_organization and get_opportunity already carry. Pass exactly one
    of the three ids. Each interaction comes with its full body. To record a new touch
    use ``log_interaction``; to list an organization's people use
    ``list_contacts(organization_id=...)``.

    Args:
        contact_id: The contact whose interactions to list.
        organization_id: The organization whose interactions to list: every touch with
            anyone who worked there when it happened.
        opportunity_id: The deal whose interactions to list: the touches logged
            against it.
        kind: Only this kind of interaction, matched exactly (e.g. "meeting").
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page, with the same id and kind.

    Returns:
        {interactions, next_cursor, next_step}. ``next_cursor`` is null on the last page.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            interactions, next_cursor = await store.list_interactions(
                conn,
                sctx,
                contact_id=contact_id,
                organization_id=organization_id,
                opportunity_id=opportunity_id,
                kind=kind,
                limit=limit,
                cursor=cursor,
            )
    except (
        ContactNotFoundError,
        OrganizationNotFoundError,
        OpportunityNotFoundError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="interaction", action="list", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_interactions")
    if next_cursor is not None:
        next_step = (
            f"Older interactions remain: call list_interactions again with "
            f"cursor={next_cursor!r} and the same id and kind."
        )
    else:
        next_step = (
            "That is the whole history. Record the next touch with "
            "log_interaction(contact_id=..., kind=..., summary=...)."
        )
    return {
        "interactions": [dump(i) for i in interactions],
        "next_cursor": next_cursor,
        "next_step": next_step,
    }


# --------------------------------------------------------------------------- #
# Similar-companies graph                                                       #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def link_similar_organizations(
    organization_id: str,
    companies: list[SimilarOrganization],
    ctx: Context | None = None,
) -> dict:
    """Save the results of a similar-companies lookup as lookalikes of one organization.

    Call it after a similar-companies search (this server runs none itself) to keep what it
    found: each company is found or created by its domain and linked to the organization with
    its similarity score. A company new to this workspace is saved as discovered and stays out
    of the default organization views until ``promote_organization``; one already held is
    linked and otherwise left as it is. A company with no domain is skipped. Linking the same
    results again changes nothing. Read the lookalikes with ``list_similar_organizations``.

    Args:
        organization_id: The organization the lookalikes resemble.
        companies: The lookup's results, one object per company.

    Returns:
        {linked, created, skipped, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            counts = await store.link_similar_companies(
                conn,
                sctx,
                org_id=organization_id,
                companies=companies,
                limits=runtime().limits(sctx),
            )
    except (OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="link_similar", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="link_similar_organizations")
    runtime().record(PeopleEvent.SIMILAR_COMPANIES_LINKED, counts["linked"])
    next_step = (
        f"Review the discovered lookalikes with "
        f"list_similar_organizations(organization_id={organization_id!r}), then "
        "promote_organization(organization_id=...) for any you want in the CRM."
    )
    if "limit" in counts:
        runtime().record(PeopleEvent(f"{counts['limit']}_refused"))
        next_step += (
            f" Companies new to this workspace that would pass its {counts['limit']} limit "
            f"were not saved and are counted in skipped. {store.limit_next_step(counts['limit'])}"
        )
    await announce_resource_list_changed(ctx)
    return {**counts, "next_step": next_step}


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def promote_organization(
    organization_id: str,
    ctx: Context | None = None,
) -> dict:
    """Promote a discovered lookalike to active, so it appears in the default organization views.

    Call it when the user decides to work a company that ``link_similar_organizations`` saved
    as discovered. An organization already active is left as it is. Naming a lookalike in
    ``find_or_create_organization``, or linking a contact to it, promotes it too, so this is
    for the decision on its own.

    Args:
        organization_id: The organization to promote.

    Returns:
        {ok, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            org = await store.promote_organization(conn, sctx, org_id=organization_id)
    except (OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="promote", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="promote_organization")
    runtime().record(PeopleEvent.ORGANIZATION_PROMOTED)
    await announce_resource_list_changed(ctx)
    return {
        "ok": True,
        "next_step": (
            f"The organization now appears in list_organizations. Add people with "
            f"find_or_create_contact(organization_id={org.id!r}, ...)."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_similar_organizations(
    organization_id: str,
    ctx: Context | None = None,
) -> dict:
    """List an organization's lookalikes, most similar first, with each one's score and when it was found.

    Call it to review the companies a similar-companies lookup linked to an organization,
    before promoting the ones worth working with ``promote_organization``. It lists
    discovered and active lookalikes alike, and leaves archived ones out until
    ``archive_organization(archived=false)`` brings them back. To save new lookalikes use
    ``link_similar_organizations``.

    Args:
        organization_id: The organization whose lookalikes to list.

    Returns:
        {similar: [{organization, similarity, discovered_at}], next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            rows = await store.list_similar_companies(conn, sctx, org_id=organization_id)
    except (OrganizationNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="organization", action="list_similar", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_similar_organizations")
    runtime().record(PeopleEvent.SIMILAR_COMPANIES_LISTED)
    similar = [
        {
            "organization": dump(org),
            "similarity": score,
            "discovered_at": discovered_at.isoformat() if discovered_at is not None else None,
        }
        for org, score, discovered_at in rows
    ]
    if similar:
        next_step = (
            "Promote a lookalike into the CRM with promote_organization(organization_id=...), "
            "or open one with get_organization(organization_id=...)."
        )
    else:
        next_step = (
            f"No similar companies linked yet. Run a similar-companies lookup for this org, then "
            f"link_similar_organizations(organization_id={organization_id!r}, companies=[...])."
        )
    return {"similar": similar, "next_step": next_step}


# --------------------------------------------------------------------------- #
# Opportunities — the pipeline surface.                                         #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def create_opportunity(
    title: str,
    organization_id: str | None = None,
    primary_contact_id: str | None = None,
    stage: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    probability: Probability | None = None,
    close_date: date | None = None,
    source: str | None = None,
    idempotency_key: str | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Create a deal (an opportunity in the pipeline) on an account or a contact.

    A deal has no natural dedupe key: two deals on one account with the same title are
    often both real (a renewal each year, a second pilot), so this never matches an
    existing deal by title or account. Check first with
    ``list_opportunities(organization_id=..., query=...)``, and create only when nothing
    there is the deal you mean; to change one that exists, use ``update_opportunity`` or
    ``move_opportunity``. Pass ``idempotency_key`` when a retry or an import must not
    create a duplicate: a repeat with the same key returns the deal the first call
    created. Without that key every call creates a new deal.

    Give an ``organization_id`` (the account), a ``primary_contact_id``, or both; with
    only a contact, the account is taken from that contact's organization.

    Args:
        title: What the deal is (required), e.g. "Acme 2027 renewal".
        organization_id: The account the deal is with.
        primary_contact_id: The deal's main contact.
        stage: A stage key from this workspace's pipeline (get_pipeline); defaults to the
            first open stage. A key the pipeline does not hold is refused, naming the ones
            it does.
        value: The deal's size, in its currency.
        currency: An ISO 4217 code, e.g. "USD".
        probability: The deal's own win probability, 0-100; omit to take the stage's
            default.
        close_date: The target close date (ISO-8601 date).
        source: Where the deal came from (open text, e.g. "manual", "import").
        idempotency_key: A token of your choosing that makes a retry or an import converge:
            a repeat with the same key returns the deal the first call created.
        fields: Custom field values by key; list_fields(entity="opportunity") lists them.
            A repeat with the same idempotency_key keeps the values the deal holds.
        fields_by: Who is setting the fields, when the values do not say.

    Returns:
        {opportunity, action: "created"|"matched", next_step}. ``matched`` only for a
        repeated ``idempotency_key``.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            opp, action = await store.find_or_create_opportunity(
                conn,
                sctx,
                title=title,
                organization_id=organization_id,
                primary_contact_id=primary_contact_id,
                stage=stage,
                value=value,
                currency=currency,
                probability=probability,
                close_date=close_date,
                source=source,
                idempotency_key=idempotency_key,
                fields=fields,
                fields_by=fields_by,
                limits=runtime().limits(sctx),
            )
    except (
        OpportunityNotFoundError,
        ContactNotFoundError,
        OrganizationNotFoundError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="opportunity", action="create", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="create_opportunity")
    if action == "created":
        runtime().record(PeopleEvent.OPPORTUNITY_CREATED)
    await announce_resource_list_changed(ctx)
    return {
        "opportunity": dump(opp),
        "action": action,
        "next_step": (
            f"Advance it with move_opportunity(opportunity_id={opp.id!r}, stage=...), "
            f"or log a touch with log_interaction(..., opportunity_id={opp.id!r})."
        ),
    }


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_opportunity(
    opportunity_id: str,
    title: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    probability: Probability | None = None,
    close_date: date | None = None,
    primary_contact_id: str | None = None,
    source: str | None = None,
    fields: dict[str, Any] | None = None,
    fields_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change a deal you hold: its title, value, probability, close date, primary contact or custom fields.

    Call it to correct or add to a deal once you have its id. Only the arguments you pass
    change; one you omit keeps its value, and a core field cannot be cleared here. It never
    changes the stage: ``move_opportunity`` does, with its side effects. To create a deal use
    ``create_opportunity``.

    Args:
        opportunity_id: The deal to change.
        title: What the deal is. Omit to keep it.
        value: The deal's size, in its currency. Omit to keep it.
        currency: An ISO 4217 code, e.g. "USD". Omit to keep it.
        probability: The deal's own win probability, 0-100, kept until its next stage move.
        close_date: The target or actual close date (ISO-8601 date).
        primary_contact_id: The deal's main contact. Omit to keep them.
        source: Where the deal came from (open text). Omit to keep it.
        fields: Custom field values by key. A key given replaces that key's value; null (or ""
            for a text field) removes it; keys not given are kept. To keep the reason with a
            value, pass {"value": ..., "why": "..."}. Keys must be defined (list_fields).
        fields_by: Who is setting these fields, when the values do not say.

    Returns:
        {opportunity, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            opp = await store.update_opportunity(
                conn,
                sctx,
                opportunity_id=opportunity_id,
                title=title,
                value=value,
                currency=currency,
                probability=probability,
                close_date=close_date,
                primary_contact_id=primary_contact_id,
                source=source,
                fields=fields,
                fields_by=fields_by,
            )
    except (OpportunityNotFoundError, ContactNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="opportunity", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_opportunity")
    await announce_resource_list_changed(ctx)
    return {
        "opportunity": dump(opp),
        "next_step": (
            f"Advance the stage with move_opportunity(opportunity_id={opp.id!r}, stage=...), "
            "or review the pipeline with get_pipeline()."
        ),
    }


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def move_opportunity(
    opportunity_id: str,
    stage: str,
    probability: Probability | None = None,
    ctx: Context | None = None,
) -> dict:
    """Move a deal to another stage of this workspace's pipeline.

    Call it when a deal advances, stalls or closes; it is the only way a deal's stage
    changes. Entering a won or lost stage stamps the close time and an open stage clears it,
    and the deal takes the stage's default probability unless ``probability`` gives its own.
    A stage the pipeline does not hold is refused, naming the ones it does (get_pipeline lists
    them). To change anything else about the deal use ``update_opportunity``.

    Args:
        opportunity_id: The deal to move.
        stage: The key of the stage to move it to.
        probability: The deal's own win probability in the new stage, 0-100; omit to take the
            stage's default.

    Returns:
        {opportunity, outcome, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            opp = await store.set_opportunity_stage(
                conn,
                sctx,
                opportunity_id=opportunity_id,
                stage=stage,
                probability=probability,
                limits=runtime().limits(sctx),
            )
    except (OpportunityNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="opportunity", action="move", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="move_opportunity")
    runtime().record(PeopleEvent.OPPORTUNITY_STAGE_CHANGED)
    await announce_resource_list_changed(ctx)
    return {
        "opportunity": dump(opp),
        "outcome": opp.outcome,
        "next_step": (
            f"See the deal's history with list_interactions(opportunity_id={opp.id!r}), "
            "or review the pipeline with get_pipeline()."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META, app=_bound_to("opportunities"))
async def get_opportunity(opportunity_id: str, ctx: Context | None = None) -> dict:
    """Read one deal in full: fields, counts and the 5 newest interactions logged against it.

    Call it when you hold a deal's id and need where it stands before you act or answer. The
    5 newest interactions come without their bodies; ``list_interactions(opportunity_id=...)``
    pages through all of them in full. To find a deal whose id you do not hold, use
    ``list_opportunities(query=...)``.

    A host that shows views opens this deal for this call.

    Args:
        opportunity_id: The deal's id (op_...).

    Returns:
        {opportunity, counts, recent_interactions, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            opp, counts = await store.get_opportunity(conn, sctx, opportunity_id=opportunity_id)
            recent, _ = await store.list_interactions(
                conn, sctx, opportunity_id=opp.id, limit=RECENT_INTERACTIONS
            )
    except (OpportunityNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="opportunity", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_opportunity")
    return {
        "opportunity": dump(opp),
        "counts": counts,
        "recent_interactions": _recent(recent),
        "next_step": (
            f"Read its whole history with list_interactions(opportunity_id={opp.id!r}), "
            f"or advance it with move_opportunity(opportunity_id={opp.id!r}, stage=...)."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_opportunities(
    query: str | None = None,
    organization_id: str | None = None,
    primary_contact_id: str | None = None,
    stage: str | None = None,
    outcome: Outcome | None = None,
    filter: dict[str, Any] | None = None,  # noqa: A002 - the wire name
    sort: str | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List deals: find them by title or account, or pull exactly the ones a task needs, a page at a time.

    Call it to find a deal whose id you do not hold (``query``), to check for an existing
    deal before ``create_opportunity``, or to read the deals that match: by account, primary
    contact, stage, outcome, or a ``filter`` on any field, including the account's
    (``organization.industry``), the primary contact's (``contact.title``) and the rollups
    ``interactions.count`` and ``interactions.last_at``. Only open deals are listed unless
    stage, outcome or a filter on either says otherwise. For per-stage totals use
    ``get_pipeline``; to read one deal in full, ``get_opportunity``.

    It opens no view: to show the user their pipeline, call ``get_pipeline``.

    Args:
        query: Free text matched against the deal's title and its account's name: split into
            words, each of which must appear in one of them.
        organization_id: Only this account's deals.
        primary_contact_id: Only deals with this primary contact.
        stage: Only deals in this stage (get_pipeline lists the keys).
        outcome: Only deals with this outcome.
        filter: Rows to keep, as {"field": path, "op": op, "value": v}, or {"and": [...]} /
            {"or": [...]} of those. The server instructions list the operators for each field
            type; list_fields lists the paths.
        sort: A field path to order by, "-" first for descending (e.g. "-weighted_value",
            "close_date"). Rows with no value sort last. Default: newest first.
        columns: Return only these paths per row (plus id and uri), e.g. ["title", "stage",
            "value", "organization.name"]. Without it each row is the whole deal.
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page. Pass the same query, filter, sort and
            columns with it.

    Returns:
        {opportunities, total, next_cursor, next_step}; total is on the first page.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            page = await store.page_opportunities(
                conn,
                sctx,
                terms=query,
                organization_id=organization_id,
                primary_contact_id=primary_contact_id,
                stage=stage,
                outcome=outcome,
                filter=filter,
                sort=sort,
                columns=columns,
                limit=limit,
                cursor=cursor,
            )
    except (ValueError, TypeError) as exc:
        await _audit_attempt(sctx, entity="opportunity", action="list", outcome="error")
        return _list_error(exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_opportunities")
    return _page_result(
        "opportunities",
        "list_opportunities",
        page,
        open_with="get_opportunity(opportunity_id=...)",
    )


# --------------------------------------------------------------------------- #
# Evidence — the signal and proof-point records.                                #
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_signal_types(include_retired: bool = False, ctx: Context | None = None) -> dict:
    """List this workspace's signal types: the kinds of fact it records about an account.

    Read it before recording a signal: ``record_signal`` takes a key from this list and
    refuses any other. Each type carries its label, what it means, and how many active
    signals carry it. A workspace starts with none; ``define_signal_type`` adds one.

    Args:
        include_retired: Also list retired types, each with its replacement. No new signal
            takes a retired type; the signals that carry one keep it.

    Returns:
        {signal_types: [{key, label, description, signal_count, retired_at?, replaced_by?,
        ...}], next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            types = await store.describe_signal_types(conn, sctx, include_retired=include_retired)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_signal_types")
    if types:
        next_step = (
            "Record a fact with record_signal(signal_type=<key>, ...); list the signals of "
            "one type with list_signals(signal_type=<key>)."
        )
    else:
        next_step = (
            "This workspace defines no signal types yet. Add one with "
            "define_signal_type(key=..., label=..., description=...)."
        )
    return {"signal_types": [dump(t) for t in types], "next_step": next_step}


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def define_signal_type(
    key: str,
    label: str,
    description: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add a signal type to this workspace's vocabulary: a kind of fact it records about an account.

    Call it when the user wants to track a kind of fact the vocabulary lacks
    (list_signal_types lists it); record_signal refuses any type not defined. It only
    adds: a key the vocabulary already holds is returned as it is and nothing changes,
    so to relabel or redescribe a type use ``update_signal_type``.

    Args:
        key: Stable identifier: lowercase letters, digits, underscores (e.g. "hiring").
            Signals store it, so choose it to last.
        label: Display label (e.g. "Hiring").
        description: What the type means, written for the agent deciding whether a
            fact is one.

    Returns:
        {signal_type, action: "created"|"matched", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            defined, action = await store.define_signal_type(
                conn,
                sctx,
                key=key,
                label=label,
                description=description,
                limits=runtime().limits(sctx),
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal_type", action="define", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="define_signal_type")
    await announce_resource_list_changed(ctx)
    if action == "created":
        next_step = (
            f"Record a fact of this type with record_signal(signal_type={defined.key!r}, ...)."
        )
    else:
        next_step = (
            f"The vocabulary already holds {defined.key!r}; it was left as it is. Change it "
            f"with update_signal_type(key={defined.key!r}, ...)."
        )
    return {"signal_type": dump(defined), "action": action, "next_step": next_step}


@mcp.tool(annotations=EDIT, meta=CHATGPT_TOOL_META)
async def update_signal_type(
    key: str,
    label: str | None = None,
    description: str | None = None,
    retired: bool | None = None,
    replaced_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Retire, restore or change a signal type: its label or description.

    Call it to rename a type or sharpen what it means, so the next agent files facts
    under it correctly; to retire a type the workspace no longer records (naming the type
    that replaces it); or to restore a retired one. Only the arguments given change; the
    key never does, so every signal of the type keeps it and none is touched. A retired
    type leaves list_signal_types, and record_signal refuses it with its replacement
    named; the signals that carry it keep it, list_signals still filters on it, and its
    key stays taken. A type retires while signals carry it. To add a type use
    ``define_signal_type``.

    Args:
        key: The signal type's key (list_signal_types lists them; include_retired=true for
            retired ones).
        label: New display label. Omit to keep it.
        description: New meaning, for the agent deciding whether a fact is one. ""
            clears it; omit to keep it.
        retired: true retires the type; false restores it and clears replaced_by.
        replaced_by: The key of the live type that takes its place. Only on a retired type.

    Returns:
        {signal_type, action: "retired"|"restored"|"updated"|"unchanged", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            updated, action = await store.update_signal_type(
                conn,
                sctx,
                key=key,
                label=label,
                description=description,
                retired=retired,
                replaced_by=replaced_by,
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal_type", action="update", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_signal_type")
    await announce_resource_list_changed(ctx)
    if updated.retired_at is None:
        next_step = "list_signal_types() shows the vocabulary as it now stands."
    else:
        next_step = (
            "list_signal_types(include_retired=true) shows it; restore it with "
            f"update_signal_type(key={updated.key!r}, retired=false)."
        )
    return {"signal_type": dump(updated), "action": action, "next_step": next_step}


@mcp.tool(annotations=ADD_CONVERGING, meta=CHATGPT_TOOL_META)
async def record_signal(
    organization_id: str,
    claim: str,
    evidence_quote: str,
    source_url: str,
    signal_type: str,
    contact_id: str | None = None,
    source_date: date | None = None,
    event_date: date | None = None,
    found_by: str | None = None,
    evidence_for: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Record one fact about an organization, with the source's own words that prove it.

    Call it when you have read a real source saying something about an account that anyone
    citing it could check: the ``claim`` is your sentence, the ``evidence_quote`` is theirs,
    verbatim, and the ``source_url`` is where a reviewer opens it. Finding nothing worth
    recording is a good outcome: record no signal rather than a weak one. For a claim about
    our own work use ``record_proof_point``. Recording the same source and type for the
    account again returns the signal already held; if that key holds a different fact, yours
    is not stored and next_step says so.

    Args:
        organization_id: The account the fact is about.
        claim: One plain sentence, readable without opening the source. It keeps the source's
            hedges: if the quote says "alleged", "reportedly" or "suspected", the claim says so
            and names who says it, or it is refused.
        evidence_quote: The source's words that support the claim, verbatim.
        source_url: The http(s) URL where it was read.
        signal_type: A key of this workspace's signal vocabulary (list_signal_types). Any other
            key is refused, naming the ones defined.
        contact_id: The person, when the fact is about one.
        source_date: The source's own date (ISO-8601 date), when it carries one: for an
            article, when it was published.
        event_date: The date the event happened (ISO-8601 date), as the evidence states it,
            often earlier than source_date. A month-only statement ("in March 2026") is the 1st
            of that month. Omit it when the source states none; never copy source_date into it.
            An announced plan takes the date of the announcement, never the date the plan is
            for. Refused when after today (UTC) or after source_date.
        found_by: Which run, agent or person found it.
        evidence_for: Whom the fact is evidence for: "workspace" when it says the organization
            might buy from this workspace, and/or the id of each organization you researched it
            for (a client or prospect whose outreach will cite it). At most 10. Adds to any
            already recorded.

    Returns:
        {signal, action: "created"|"matched", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with (
            asyncio.timeout(store.SIGNAL_WRITE_DEADLINE.total_seconds()),
            runtime().session(sctx) as conn,
        ):
            sig, action, stored = await store.record_signal(
                conn,
                sctx,
                organization_id=organization_id,
                claim=claim,
                evidence_quote=evidence_quote,
                source_url=source_url,
                signal_type=signal_type,
                contact_id=contact_id,
                source_date=source_date,
                event_date=event_date,
                found_by=found_by,
                evidence_for=evidence_for,
                limits=runtime().limits(sctx),
            )
    except (OrganizationNotFoundError, ContactNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal", action="record", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="record_signal")
    if action == "created":
        runtime().record(PeopleEvent.SIGNAL_RECORDED)
    if not stored:
        # The key matched a row recording a different fact from the same page and
        # type. The caller is holding a signal it did not write, so say that
        # before anything tells it to cite the id.
        #
        # Two independent things are true here and BOTH have to be said: the
        # caller's claim was discarded, AND the row it got back may itself be one
        # nothing should cite. Naming only the first is how a collision against a
        # withdrawn row ends up reading as an invitation.
        runtime().record(PeopleEvent.SIGNAL_COLLIDED)
        if sig.status != "active":
            held = "and it is WITHDRAWN, so do not cite it at all"
        else:
            held = "so cite it ONLY if its quote supports your point"
        next_step = (
            f"NOT STORED. This source and signal type already hold a different signal: "
            f"{sig.claim!r} (quote: {sig.evidence_quote!r}) — {held}. Your claim was "
            f"discarded{_purposes_not_recorded(evidence_for)}; record it under the signal "
            f"type that fits it, or from the source that states it."
        )
    elif sig.status != "active":
        next_step = (
            f"This signal was withdrawn and stays withdrawn — re-finding the source does not "
            f"revive it{_purposes_not_recorded(evidence_for)}. Record a different signal, or "
            f"cite another for organization_id={sig.organization_id!r}."
        )
    else:
        next_step = (
            f"Cite it by its uri ({sig.uri}), and check what else this account has with "
            f"list_signals(organization_id={sig.organization_id!r})."
        )
    await announce_resource_list_changed(ctx)
    return {"signal": dump(sig), "action": action, "next_step": next_step}


def _purposes_not_recorded(evidence_for: list[str] | None) -> str:
    """The clause a record_signal next_step adds when the evidence_for it was given was not
    recorded, because the call stored no fact or matched a withdrawn one."""
    return ", and the evidence_for purposes were not recorded" if evidence_for else ""


@mcp.tool(annotations=REMOVE, meta=CHATGPT_TOOL_META)
async def withdraw_signal(
    signal_id: str,
    reason: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Withdraw a signal that turned out to be wrong, stale or misread.

    Call it when the fact itself no longer holds. Nothing is deleted: the signal stays on
    record, so anything that cited it can still be traced, and it leaves the default
    ``list_signals`` read. Withdrawing again keeps the first withdrawal's time and reason. If
    the fact holds but was filed for the wrong purpose, use ``update_signal`` instead.

    Args:
        signal_id: The signal to withdraw.
        reason: Why it is withdrawn, recorded on the signal.

    Returns:
        {signal, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with (
            asyncio.timeout(store.SIGNAL_WRITE_DEADLINE.total_seconds()),
            runtime().session(sctx) as conn,
        ):
            sig = await store.withdraw_signal(conn, sctx, signal_id=signal_id, reason=reason)
    except (SignalNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal", action="withdraw", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="withdraw_signal")
    runtime().record(PeopleEvent.SIGNAL_WITHDRAWN)
    await announce_resource_list_changed(ctx)
    return {
        "signal": dump(sig),
        "next_step": (
            f"Anything citing this signal needs re-checking. See what evidence the account "
            f"still has with list_signals(organization_id={sig.organization_id!r})."
        ),
    }


@mcp.tool(annotations=MOVE, meta=CHATGPT_TOOL_META)
async def update_signal(
    signal_id: str,
    add_evidence_for: list[str] | None = None,
    remove_evidence_for: list[str] | None = None,
    added_by: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Change whom a recorded signal is evidence for: add a purpose, or remove one filed by mistake.

    A signal is a fact about its organization; whom it is evidence for is why it was
    recorded. "workspace" means the organization might buy from this workspace; an
    organization id means the fact was researched for that organization (a client or
    prospect whose outreach will cite it). Call this when a fact you already recorded
    turns out to serve another purpose too, or was filed under the wrong one. The
    signal's claim, quote, dates and status never change here; to say the fact itself
    is wrong, use ``withdraw_signal``. A withdrawn signal is refused.

    Adding a party already recorded, or removing one not recorded, changes nothing; adding
    one that was removed restores it.

    Args:
        signal_id: The signal.
        add_evidence_for: Parties to add: "workspace" and/or organization ids, at most 10.
        remove_evidence_for: Parties to remove, the same way. Not a party also in
            add_evidence_for.
        added_by: Provenance for what this adds: which run, agent, or person decided it.

    Returns:
        {signal, added, removed, next_step}; ``added`` and ``removed`` name the parties
        this call changed.
    """
    sctx = runtime().identity()
    try:
        if add_evidence_for is None and remove_evidence_for is None:
            raise ValueError("pass add_evidence_for, remove_evidence_for, or both")
        both = set(add_evidence_for or ()) & set(remove_evidence_for or ())
        if both:
            raise ValueError(
                f"{sorted(both)[0]!r} is in both add_evidence_for and remove_evidence_for"
            )
        added: list[str] = []
        removed: list[str] = []
        async with (
            asyncio.timeout(store.SIGNAL_WRITE_DEADLINE.total_seconds()),
            runtime().session(sctx) as conn,
        ):
            if add_evidence_for is not None:
                sig, added = await store.add_signal_for(
                    conn,
                    sctx,
                    signal_id=signal_id,
                    evidence_for=add_evidence_for,
                    added_by=added_by,
                )
            if remove_evidence_for is not None:
                sig, removed = await store.remove_signal_for(
                    conn, sctx, signal_id=signal_id, evidence_for=remove_evidence_for
                )
    except (
        SignalNotFoundError,
        OrganizationNotFoundError,
        ConflictError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(
            sctx, entity="signal_evidence_for", action="update", exc=exc
        )
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="update_signal")
    await announce_resource_list_changed(ctx)
    held = ", ".join(sig.evidence_for) if sig.evidence_for else "no party (not stated)"
    return {
        "signal": dump(sig),
        "added": added,
        "removed": removed,
        "next_step": (
            f"It is now evidence for {held}. Cite it by its uri ({sig.uri}) in outreach for "
            "those parties."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def get_signal(signal_id: str, ctx: Context | None = None) -> dict:
    """Read one signal: its claim, the verbatim quote, the source URL, its dates and whom it is evidence for.

    Call it when you hold a signal's id, such as one cited in a message, and need to check
    what it says or whether it still stands (a withdrawn one says so). To find an account's
    signals use ``list_signals(organization_id=...)``.

    Args:
        signal_id: The signal's id (sg_...).

    Returns:
        {signal, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            sig = await store.get_signal(conn, sctx, signal_id=signal_id)
    except (SignalNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_signal")
    return {
        "signal": dump(sig),
        "next_step": (
            f"Open {sig.source_url} to check the quote against the source, or withdraw it with "
            f"withdraw_signal(signal_id={sig.id!r}) if it no longer holds."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_signals(
    organization_id: str | None = None,
    contact_id: str | None = None,
    signal_type: str | None = None,
    status: SignalStatus | None = None,
    evidence_for: str | None = None,
    changed_since: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List recorded signals, newest first, or the ones changed since a mark. Read it before researching an account again.

    Call it before researching an account (the work may be done), before citing a fact, or to
    follow new and changed signals. Withdrawn signals are left out unless status asks for
    them. An empty result for an account already researched means nothing qualified, which is
    an answer. To follow changes, pass ``changed_since``: "" the first time, then the
    next_cursor the last call returned; signals then come oldest change first (created,
    edited, withdrawn, or whom they are evidence for), a change shows once it is a few
    minutes old, and the same signal can come back more than once.

    Args:
        organization_id: Only this account's signals.
        contact_id: Only signals about this person.
        signal_type: Only signals of this type.
        status: Which signals to list (default active).
        evidence_for: "workspace" (evidence for this workspace's own pipeline), an organization
            id (evidence for that organization), or "unstated" (no party).
        changed_since: A change mark: "" to start, then the last call's next_cursor.
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page (not with changed_since).

    Returns:
        {signals, next_cursor, next_step}.
    """
    sctx = runtime().identity()
    filters: dict[str, Any] = {
        "organization_id": organization_id,
        "contact_id": contact_id,
        "signal_type": signal_type,
        "status": status,
        "evidence_for": evidence_for,
        "limit": limit,
    }
    if changed_since is not None:
        return await _list_signal_changes(sctx, changed_since, cursor, filters)
    try:
        async with runtime().session(sctx) as conn:
            signals, next_cursor = await store.list_signals(conn, sctx, cursor=cursor, **filters)
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal", action="list", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_signals")
    if signals:
        next_step = (
            "Cite each by its uri — the claim is the fact, the quote is what backs it. "
            "Read one in full with get_signal(signal_id=...)."
        )
    else:
        next_step = (
            "No signals recorded here. Research the account, then record what you find with "
            "record_signal(...) — and record nothing if nothing qualifies."
        )
    result: dict[str, Any] = {"signals": [dump(s) for s in signals], "next_step": next_step}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


async def _list_signal_changes(
    sctx: Scope, changed_since: str, cursor: str | None, filters: dict[str, Any]
) -> dict[str, Any]:
    """``list_signals`` reading changes since a mark. Its ``next_cursor`` is always the
    mark to keep, so a reader that has caught up still holds one."""
    if cursor is not None:
        await _audit_attempt(sctx, entity="signal", action="list_changes", outcome="error")
        return _invalid(
            "cursor and changed_since are exclusive",
            "Pass the next_cursor of a changed_since call back as changed_since, and omit cursor.",
        )
    try:
        async with runtime().session(sctx) as conn:
            signals, mark, more = await store.list_signal_changes(
                conn, sctx, changed_since=changed_since, **filters
            )
    except InvalidCursor:
        await _audit_attempt(sctx, entity="signal", action="list_changes", outcome="error")
        return _invalid(
            f"changed_since: {INVALID_CURSOR_MESSAGE}",
            'changed_since takes "" to start from the beginning, or the next_cursor a '
            "previous changed_since call returned; a listing cursor is not a change mark.",
        )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="signal", action="list_changes", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_signals")
    if more:
        next_step = (
            "More changes are waiting: call again with changed_since=next_cursor and the "
            "same filters."
        )
    else:
        next_step = (
            "Caught up. Keep next_cursor as your mark and pass it as changed_since next time; "
            "a signal can come back more than once, so handle a repeat."
        )
    return {
        "signals": [dump(s) for s in signals],
        "next_cursor": mark,
        "next_step": next_step,
    }


@mcp.tool(annotations=ADD, meta=CHATGPT_TOOL_META)
async def record_proof_point(
    claim: str,
    organization_id: str | None = None,
    engagement: str | None = None,
    metric: str | None = None,
    timeframe: str | None = None,
    verification: Verification | None = None,
    attested_by: str | None = None,
    scope_notes: str | None = None,
    idempotency_key: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Record a citable claim about our own work, with whether anyone has attested it.

    Call it when a number or outcome about work this workspace did may be cited in copy: a
    case-study result, a customer outcome. It starts unverified unless ``attested_by`` names
    who stands behind it as verified; a claim is evidence only once attested. For a number
    invented so copy can be drafted, record it as ``fabricated-placeholder``: marking it is
    what stops it being read as evidence later. For a fact about another company use
    ``record_signal``.

    Args:
        claim: The citable sentence.
        organization_id: The client, when they are in the CRM.
        engagement: A label for the client or engagement when there is no organization record.
        metric: The number, as it would be cited (e.g. "42% faster onboarding").
        timeframe: The period it was achieved over.
        verification: Whether it is attested (default unverified). verified needs attested_by.
        attested_by: Who stands behind it. Required to record it as verified.
        scope_notes: Naming permissions: NDA, attribution, which figures may ship.
        idempotency_key: A token of your choosing that makes a retry safe: a repeat with the
            same key returns the proof point the first call recorded.

    Returns:
        {proof_point, action: "created"|"matched", next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            proof, action = await store.record_proof_point(
                conn,
                sctx,
                claim=claim,
                organization_id=organization_id,
                engagement=engagement,
                metric=metric,
                timeframe=timeframe,
                verification=verification,
                attested_by=attested_by,
                scope_notes=scope_notes,
                idempotency_key=idempotency_key,
                limits=runtime().limits(sctx),
            )
    except (
        OrganizationNotFoundError,
        ProofPointNotFoundError,
        ValueError,
        TypeError,
    ) as exc:
        return await _handle_tool_error(sctx, entity="proof_point", action="record", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="record_proof_point")
    if action == "created":
        runtime().record(PeopleEvent.PROOF_POINT_RECORDED)
    if proof.verification == "verified":
        next_step = (
            f"Citable as it stands. Cite it by its uri ({proof.uri}) wherever the claim ships."
        )
    else:
        next_step = (
            f"This claim is {proof.verification} and is not evidence yet. Attest it with "
            f"verify_proof_point(proof_point_id={proof.id!r}, verification='verified', "
            f"attested_by=...) once someone can stand behind it."
        )
    await announce_resource_list_changed(ctx)
    return {"proof_point": dump(proof), "action": action, "next_step": next_step}


@mcp.tool(annotations=MOVE_STAMPED, meta=CHATGPT_TOOL_META)
async def verify_proof_point(
    proof_point_id: str,
    verification: Verification,
    attested_by: str,
    scope_notes: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Attest a proof point: the moment a claim becomes, or stops being, evidence.

    Call it when a person stands behind a claim (verified), withdraws that backing
    (unverified), or declares it a placeholder (fabricated-placeholder). Every direction names
    who made the call and stamps when. To record a new claim use ``record_proof_point``.

    Args:
        proof_point_id: The proof point to attest.
        verification: Its new state.
        attested_by: Who is making this call.
        scope_notes: Naming permissions; omit to keep them.

    Returns:
        {proof_point, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            proof = await store.verify_proof_point(
                conn,
                sctx,
                proof_point_id=proof_point_id,
                verification=verification,
                attested_by=attested_by,
                scope_notes=scope_notes,
            )
    except (ProofPointNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="proof_point", action="verify", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="verify_proof_point")
    runtime().record(_ATTESTED[proof.verification])
    if proof.verification == "verified":
        next_step = (
            f"Attested by {proof.attested_by}. Check scope_notes before naming the client in copy."
        )
    else:
        next_step = (
            f"Recorded as {proof.verification}. It must not ship as evidence; list the rest of "
            "the punch list with "
            "list_proof_points(verification='fabricated-placeholder')."
        )
    await announce_resource_list_changed(ctx)
    return {"proof_point": dump(proof), "next_step": next_step}


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def get_proof_point(proof_point_id: str, ctx: Context | None = None) -> dict:
    """Read one proof point: its claim, metric, timeframe, verification and naming permissions.

    Call it when you hold a proof point's id and need to know whether it may be cited: only a
    verified one is evidence, and its scope_notes say what may be named. To find proof points
    use ``list_proof_points``.

    Args:
        proof_point_id: The proof point's id (pp_...).

    Returns:
        {proof_point, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            proof = await store.get_proof_point(conn, sctx, proof_point_id=proof_point_id)
    except (ProofPointNotFoundError, ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="proof_point", action="get", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="get_proof_point")
    return {
        "proof_point": dump(proof),
        "next_step": (
            f"Only a verified proof point is citable; this one is {proof.verification}. "
            f"Change that with verify_proof_point(proof_point_id={proof.id!r}, ...)."
        ),
    }


@mcp.tool(annotations=READ, meta=CHATGPT_TOOL_META)
async def list_proof_points(
    organization_id: str | None = None,
    verification: Verification | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """List recorded proof points, newest first. Read it before citing a number about our own work.

    Call it before any outcome number goes into copy, to see what is recorded and whether it
    is attested. Nothing is hidden by default: an unverified or placeholder claim is what you
    most need to see. Filter to fabricated-placeholder for the list of case studies still to
    obtain.

    Args:
        organization_id: Only this client's proof points.
        verification: Only proof points in this state.
        limit: Page size (default 50, at most 200).
        cursor: The next_cursor of the previous page.

    Returns:
        {proof_points, next_cursor, next_step}.
    """
    sctx = runtime().identity()
    try:
        async with runtime().session(sctx) as conn:
            proofs, next_cursor = await store.list_proof_points(
                conn,
                sctx,
                organization_id=organization_id,
                verification=verification,
                limit=limit,
                cursor=cursor,
            )
    except (ValueError, TypeError) as exc:
        return await _handle_tool_error(sctx, entity="proof_point", action="list", exc=exc)
    except Exception as exc:  # noqa: BLE001 - final safety net; never a raw 500
        return _internal_error(exc, where="list_proof_points")
    unattested = [p for p in proofs if p.verification != "verified"]
    if not proofs:
        next_step = (
            "No proof points recorded. Record one with record_proof_point(...) before any "
            "outcome number goes into copy."
        )
    elif unattested:
        next_step = (
            f"{len(unattested)} of {len(proofs)} are not verified and must not ship as "
            "evidence. Attest one with verify_proof_point(proof_point_id=..., "
            "verification='verified', attested_by=...)."
        )
    else:
        next_step = "All verified. Check each one's scope_notes before naming a client in copy."
    result: dict[str, Any] = {
        "proof_points": [dump(p) for p in proofs],
        "next_step": next_step,
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


# --------------------------------------------------------------------------- #
# Record addresses — a contact, an organization, an opportunity, a signal and   #
# a proof point, each readable at the URI its record carries (`addresses.py`).  #
# The UI reports the first three in its trail, so the host can open the view at  #
# any of them the agent has read.                                                #
# --------------------------------------------------------------------------- #


async def _record_resource(
    read: Awaitable[dict[str, Any] | ToolResult], uri: str, entity: str
) -> str:
    """A record read's tool result as the resource's body, or the read's refusal.

    The read runs the tool itself, so the resource and the tool cannot disagree. A
    not-found names the address asked for: RLS hides another workspace's row, so absent
    and out of scope are one answer, as through the tool. Any other failure (the database
    unreachable, a workspace at its limit, an internal fault) answers with its own
    host-safe message.

    A read with no verified identity is refused here: the tool raises ``AuthError`` for the
    error contract, which covers tool calls, and a resource read has no in-band error
    result for it to return.
    """
    try:
        result = await read
    except AuthError:
        raise ResourceError(f"The {entity} could not be read.") from None
    if isinstance(result, ToolResult):
        result = result.structured_content or {}
    error = result.get("error")
    if isinstance(error, dict):
        if error.get("code") == "not_found":
            raise ResourceError(f"No {entity} found at {uri}.")
        raise ResourceError(str(error.get("message") or f"The {entity} could not be read."))
    return json.dumps(result, default=str)


@mcp.resource(
    CONTACT_URI_TEMPLATE,
    name="contact",
    title="Contact",
    description="One contact as get_contact returns it.",
    mime_type="application/json",
)
async def contact_resource(contact_id: str) -> str:
    return await _record_resource(
        get_contact(contact_id=contact_id),
        CONTACT_URI_TEMPLATE.format(contact_id=contact_id),
        "contact",
    )


@mcp.resource(
    ORGANIZATION_URI_TEMPLATE,
    name="organization",
    title="Organization",
    description="One organization as get_organization returns it.",
    mime_type="application/json",
)
async def organization_resource(organization_id: str) -> str:
    return await _record_resource(
        get_organization(organization_id=organization_id),
        ORGANIZATION_URI_TEMPLATE.format(organization_id=organization_id),
        "organization",
    )


@mcp.resource(
    OPPORTUNITY_URI_TEMPLATE,
    name="opportunity",
    title="Opportunity",
    description="One deal as get_opportunity returns it.",
    mime_type="application/json",
)
async def opportunity_resource(opportunity_id: str) -> str:
    return await _record_resource(
        get_opportunity(opportunity_id=opportunity_id),
        OPPORTUNITY_URI_TEMPLATE.format(opportunity_id=opportunity_id),
        "opportunity",
    )


@mcp.resource(
    SIGNAL_URI_TEMPLATE,
    name="signal",
    title="Signal",
    description=(
        "One signal as get_signal returns it: the claim, the quote, its source, and the "
        "source's and event's dates."
    ),
    mime_type="application/json",
)
async def signal_resource(signal_id: str) -> str:
    return await _record_resource(
        get_signal(signal_id=signal_id), SIGNAL_URI_TEMPLATE.format(signal_id=signal_id), "signal"
    )


@mcp.resource(
    PROOF_POINT_URI_TEMPLATE,
    name="proof_point",
    title="Proof point",
    description="One proof point as get_proof_point returns it, with its verification.",
    mime_type="application/json",
)
async def proof_point_resource(proof_point_id: str) -> str:
    return await _record_resource(
        get_proof_point(proof_point_id=proof_point_id),
        PROOF_POINT_URI_TEMPLATE.format(proof_point_id=proof_point_id),
        "proof point",
    )


# --------------------------------------------------------------------------- #
# Entry points.                                                                 #
# --------------------------------------------------------------------------- #

_app: Any = None


def __getattr__(name: str) -> Any:
    """``app``: the ASGI app for the runtime ``PEOPLE_RUNTIME`` names, built on first access.

    Built lazily so importing this module needs no runtime; ``uvicorn mcp_people.server:app``
    resolves it, and refuses to start when no runtime can be selected.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = installed().wrap_http(mcp.http_app())
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """Serve stdio on the runtime ``PEOPLE_RUNTIME`` names; exit non-zero without one."""
    try:
        installed()
    except RuntimeSelectionError as exc:
        print(f"mcp-people: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    logger.info("people_stdio_start", extra={"service": SERVICE})
    mcp.run()


# Stdio entrypoint (Claude Desktop via python -m mcp_people.server).
if __name__ == "__main__":
    main()
