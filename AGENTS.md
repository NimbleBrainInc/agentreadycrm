# Agent Ready CRM

A CRM for AI agents, as an MCP server with views. One distribution, `agentreadycrm`, holds the
core (`src/mcp_people/`) and the local runtime (`src/mcp_people_local/`); the views are built
from `ui/` into the wheel. `docs/architecture.md` explains the design; `docs/local.md` the
local runtime.

## This repository is public

Every file, commit message, pull request, issue and comment here is public and indexed. Never
name an internal host, cloud account, registry path, private repository or package, internal
service, or customer, not even as an example; use `acme-corp`, `tenant-a`,
`example.com`. Decode any base64 in a fixture before committing it, and check the full
diff and commit message for these before pushing.

`scripts/check_public.py` (`make scrub`) fails on the patterns it knows, in the tree and in a
pull request's title and body; CI runs it, and `verify` requires it. It catches names, not
meaning, so it is a floor, not the review. A generic word that trips it goes in
`scripts/public_allowlist.txt` with a comment saying why. Run it on a commit message or a
comment before posting: `python scripts/check_public.py --text <file>`.

## Rules the code holds

- **Identity comes from the runtime, never from a tool argument.** A tool asks the runtime for
  the caller's `Scope`, then opens a session for it. No result returns `tenant_id` or
  `workspace_id`.
- **The core names no runtime.** Runtimes register under the `mcp_people.runtimes` entry-point
  group; `PEOPLE_RUNTIME` selects one, and the server refuses to start without a valid one.
  `agentreadycrm` is the local runtime's own command and selects it.
- **The local runtime has one principal.** It is safe on SQLite only because of that. Do not
  add a second principal to it; a runtime with more than one belongs on Postgres with
  row-level security.
- **No Postgres driver, no optional SDK modules.** The package imports no `asyncpg` and none of
  `nimblebrain_server_sdk.db` or `nimblebrain_server_sdk.observability`
  (`tests/test_import_boundary.py`); the SDK core is allowed.
- **The tables and store run on Postgres and SQLite.** A construct the two spell differently
  goes through a helper in `src/mcp_people/db/dialect.py`, whose Postgres form is the native
  construct; the store never writes one directly. `tests/test_store_dialects.py` holds both.
- **Every tool is annotated** by its kind (read, add, edit, move, withdraw or delete, merge),
  closed-world, with `idempotentHint` where true. An unannotated tool is treated as
  destructive.
- **Every failure is one envelope**, `{"error": {"code", "message", "retryable", ...},
  "next_step"}`, flagged `isError`. A `next_step` names what to change.
- **A tool description says when to call it and what to use instead**; the server
  instructions hold the rules that span tools, once. Neither names anything but this server.
- **Writes announce, reads don't.** A committed write sends `resources/list_changed`; a read
  sends none, even one that writes an audit row.
- **No tool removes a row.** A delete sets `deleted_at`, a vocabulary entry retires
  (`retired_at`), and every read leaves a marked row out. A unique index carries
  `deleted_at IS NULL`. `tests/test_no_row_removed.py` fails on a `DELETE` or `TRUNCATE` in
  shipped code, and `tests/test_delete_reads.py` fails until a new read tool is covered.
- **Paging is keyset**, owned by the store through `pgkeyset` (pinned exactly).
- **A table change needs a migration** in `src/mcp_people_local/migrations/`;
  `tests/local/test_local_schema_parity.py` holds the history to
  `mcp_people.db.tables.metadata`. A revision imports no application code.

## Golden files

`tests/golden/` holds frozen output with no regenerate path:

| File | Holds |
|---|---|
| `surface.json` | Every tool with its schemas and annotations, the resources and templates, the capabilities, the instructions, and a digest of each skill, as every runtime serves them |
| `ui_surface.json`, `ui_bundle/` | The UI a host reads: the `ui://` resources, each tool's and record template's `_meta`, against a fixed bundle |
| `paging.txt` | The cursors agents already hold, and the SQL each resumes with |

A deliberate change edits the golden file by hand in the same pull request, so review sees the
diff. A dependency bump that changes one is a reviewed change too. `uv.lock` is what CI and
the golden tests run, and only a pull request moves it; the released MCP bundle is held to the
same SDK version by the constraint in `mcpb/pyproject.toml` (`tests/test_mcpb.py`). The lock
does not gate an install of the published package: `uvx agentreadycrm` or pip takes the newest
release in the package's range. `nimblebrain-server-sdk` is held to a minor range (a 0.x minor
may break), so such an install takes the newest 0.8.x on the SDK's patch policy, untested here.
`pgkeyset` is pinned exactly, because `paging.txt` freezes its cursor format. A
change to a tool's name, schema, annotations or `_meta` also moves `CONTRACT_SHA256` in
`tests/test_public_surface.py`.

## Working here

- `make verify` runs everything CI runs: the scrub, format, lint, ty, the UI build, every
  Python suite, the wheel check, and the UI typecheck and tests. It passes with no skips: in
  CI (`CI` set) a skipped test fails.
- Python tooling is uv, ruff and ty. Suppress a ty diagnostic with `# ty: ignore[rule]`.
- The UI needs Node 24 (`cd ui && npm ci`).
- `tests/local/` drives the server as a separate process over stdio and HTTP, the way a
  client sees it. Keep a behavior test there when it depends on process boundaries.
- Every change lands by pull request with the `verify` check green.
- Comments and docs describe the current state and why it is that way; what changed belongs in
  git history.
