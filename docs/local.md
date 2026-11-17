# Running locally

`agentreadycrm` runs the local runtime (`src/mcp_people_local`): everything in one SQLite file
on your machine, one principal, no account and no network beyond loopback. The tools, skills,
views and surface are the same as on any other runtime (`tests/golden/surface.json` holds
them); only where identity comes from and where the data lives differ.

```sh
agentreadycrm                # stdio (what an MCP client starts)
agentreadycrm --http         # HTTP on http://127.0.0.1:8766/mcp
agentreadycrm --print-token  # print the token HTTP requires, then exit
```

With uv, prefix each with `uvx` (`uvx agentreadycrm`). From a checkout, `uv run agentreadycrm`
(or `make run`, `make run-http`, `make token`). `python -m mcp_people.server` with
`PEOPLE_RUNTIME=local` serves stdio too. The MCP bundle in `mcpb/` starts
`python -m mcp_people_local`, the same command.

| Setting | Meaning |
|---|---|
| `PEOPLE_LOCAL_DB` | The database file. Default: `crm.db` in the per-user data directory |
| `PEOPLE_LOCAL_PORT` | The port for `--http`. Default: `8766` |
| `PEOPLE_RUNTIME` | `local`, or unset; `agentreadycrm` refuses any other value before starting a runtime |
| `PEOPLE_UI_DIR` | A directory holding a built `index.html` to serve instead of the one the package carries |

## One principal

Every call acts as `Scope("local", "local")`, whatever the request carries: headers that name
another account or user are ignored. SQLite has no row-level security, and a read by id
carries no account predicate, so the local runtime is safe only because it has exactly one
principal. It records no metrics, applies no limits, and has no FastMCP auth provider (HTTP is
guarded as below).

## The database file

The default file is `crm.db` in the per-user data directory:

| OS | Directory |
|---|---|
| macOS | `~/Library/Application Support/agentreadycrm/` |
| Linux | `$XDG_DATA_HOME/agentreadycrm/`, or `~/.local/share/agentreadycrm/` |
| Windows | `%LOCALAPPDATA%\agentreadycrm\` |

The directory and the file are created on first start. Beside the file SQLite keeps
`crm.db-wal` and `crm.db-shm` while the database is open; they are part of it.

## Several processes, one file

Each MCP client starts its own server process, and they may all use the same file. The file is
in WAL mode, so readers never wait for a writer. Every transaction starts with
`BEGIN IMMEDIATE`, taking the write lock up front, so two processes deduping the same
organization or seeding the default pipeline take turns and each sees the other's committed
rows. A process waits up to 15 seconds for the lock, including when it switches a new file to
WAL while another process holds it; a call that still cannot get the lock fails with
`rate_limited`, having changed nothing, and is safe to retry.

**Keep the file on a local disk.** WAL mode does not work on network file systems, and a
synced folder can copy the file mid-write.

## HTTP and its token

`--http` binds to `127.0.0.1` and nowhere else; there is no setting to change that. Every
request is checked twice:

1. **Loopback, `Host` and `Origin` (403).** A request from a client address that is not
   loopback, with a `Host` header that does not name this machine, or with an `Origin` that
   is not a loopback origin is refused. The `Host` check stops a DNS-rebinding page from
   reaching the server through a browser; the `Origin` check is what the MCP specification
   asks of a local server. A request with no `Origin` (a client that is not a browser) is
   allowed.
2. **The bearer token (401).** Loopback keeps out other machines and web pages, not other
   programs on this machine, so HTTP also requires `Authorization: Bearer <token>`. The
   comparison is constant-time, and a refusal carries `WWW-Authenticate: Bearer` and nothing
   about the token.

The token lives beside the database, at `<database>.token` (`crm.db.token` by default). It is
made on first use, readable by your user only (`0600`), and reused on every later start, so a
client configured once keeps working. `agentreadycrm --print-token` prints it for the database
`PEOPLE_LOCAL_DB` names, making it first if there is none; an installer can run it before the
server first starts. To change the token, stop the server, delete the `.token` file and start
it again.

stdio needs no token: a process that can start the server can already read the database.

## The schema and upgrades

The local runtime has its own Alembic history, `src/mcp_people_local/migrations/`. It has no
row-level security, no GIN indexes on `fields`, and no `INCLUDE` columns on the listing
indexes. `tests/local/test_local_schema_parity.py` holds its head to
`mcp_people.db.tables.metadata`, so a table change in the core needs a revision here. A
revision imports no application code.

On start, the server brings the file's schema to the version it was built for:

- A file at the current version is opened as it is.
- A file at an older version is first copied to `<file>.<revision>.bak` (for example
  `crm.db.0001.bak`), then upgraded. The copy goes through SQLite's backup API, so it is
  consistent, WAL contents included.
- A file written by a newer version is refused rather than opened, and the server exits
  non-zero. Upgrade `agentreadycrm` to open it.
- A file that holds tables but no schema version is not an Agent Ready CRM database, and is
  refused.

Processes starting together upgrade one at a time; each later one finds the file current.

## What differs on SQLite

The tables and store run on Postgres and on SQLite. Each construct the two spell differently
is one helper in `src/mcp_people/db/dialect.py`, and `tests/test_store_dialects.py` holds each
to one answer. A few behaviors still differ:

| Behavior | Postgres | SQLite |
|---|---|---|
| Case-blind free text: a listing's `query`, a text filter's `contains`, `starts_with` and `eq` on a column with no stored key, `count_records` grouping such a column | `ILIKE` and `lower()` fold every cased letter | `lower()` and `LIKE` fold ASCII only, so `É` does not match `é`. The match keys (`name_key`, `email_key`, `domain_key`) are computed by the application, so dedupe and the key filters agree on both |
| Text sort (`sort="name"`) | The database collation | Byte order (`BINARY`): `Z` before `a`, accented letters after `z` |
| Deal values | `numeric(18, 2)`, exact | A float, which holds a value to the cent below about 10¹³. On read a value and a sum are rounded to the cent and a weighted value to ten places |
| The clock a signal write stamps | `now()`, the transaction's start | The process's UTC time at each statement. Writers are serialized, so the order is the commit order. Every keyset breaks ties on the id |
| Custom-field filters | `@>` rides a GIN index | `json_each` / `json_extract` over the workspace's rows |

A value Postgres would refuse is refused by the store before the database, so SQLite refuses it
too: a NUL character in a custom-field value fails the write, and an import skips the row.

SQLite 3.35 or newer is required (`RETURNING`); the JSON functions are built into the usual
builds.

## The views

The four views (`ui://people/contacts`, `organizations`, `opportunities`, `settings`) are one
built HTML file that every wheel carries as `mcp_people/ui_dist/index.html` (`hatch_build.py`
packs it, and refuses to build a wheel without it). An installed server serves it with no
setting. `scripts/check_wheel.py` checks the wheel carries it, and the local suites read each
view from a running server.

## The MCP bundle

`mcpb/` is a `uv` bundle: a manifest and a `pyproject.toml` that pins this distribution at its
own version, no code. The manifest describes stdio, takes the database path from `user_config`
into `PEOPLE_LOCAL_DB`, and lists exactly the registered tools. `tests/test_mcpb.py` holds each
of these. The release workflow locks the bundle's project against the version it publishes.

## Backups

To back up while servers may be running, use SQLite's own backup, which is consistent and
includes the WAL:

```sh
sqlite3 "$HOME/Library/Application Support/agentreadycrm/crm.db" ".backup crm-backup.db"
```

With every server stopped, copying `crm.db` alone is enough. To restore, stop the servers and
put the backup in place of `crm.db`, removing any `crm.db-wal` and `crm.db-shm` beside it. The
`<file>.<revision>.bak` copies an upgrade makes are ordinary database files and restore the
same way, with a version of `agentreadycrm` that matches them.
