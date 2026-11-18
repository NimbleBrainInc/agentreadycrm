# Agent Ready CRM

A CRM for AI agents, served as an MCP server: organizations, contacts, interactions, deals,
and the evidence behind them. An agent resolves a company or a person before it writes, logs
every touch, moves deals through a pipeline, and records the facts it found with the quote and
the URL that prove them. A person sees the same records in views that render inside MCP Apps
hosts.

It runs on your own machine: one SQLite file, no account, no cloud service. A hosted service
at `mcp.agentreadycrm.com`, serving the same tools to a team, is coming soon.

## Install

Run it with [uv](https://docs.astral.sh/uv/) (Python 3.13 or later):

```sh
uvx agentreadycrm
```

That serves MCP over stdio. To add it to Claude Code:

```sh
claude mcp add agentreadycrm -- uvx agentreadycrm
```

For Claude Desktop and other hosts that install MCP bundles, the bundle
`@nimblebraininc/agentreadycrm` is available from [mpak](https://www.mpak.dev). It runs the
same package and asks where to keep the database.

Any other MCP client that starts a stdio server can run `uvx agentreadycrm` the same way. To
serve HTTP on `127.0.0.1` instead, run `uvx agentreadycrm --http`; HTTP requires a bearer
token, which `uvx agentreadycrm --print-token` prints. See [`docs/local.md`](docs/local.md).

## Where your data lives

Everything is in one SQLite file, `crm.db`, in your user data directory:

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/agentreadycrm/crm.db` |
| Linux | `$XDG_DATA_HOME/agentreadycrm/crm.db` (`~/.local/share/...` when unset) |
| Windows | `%LOCALAPPDATA%\agentreadycrm\crm.db` |

Set `PEOPLE_LOCAL_DB` to use another file. Keep it on a local disk, not a network share.
Several clients may use the same file at once. Upgrades, backups and the HTTP token are
covered in [`docs/local.md`](docs/local.md).

## Local or hosted

| | Local (this package) | Hosted (coming soon) |
|---|---|---|
| Runs | On your machine, started by your MCP client | At `mcp.agentreadycrm.com` |
| Data | One SQLite file you own | A managed Postgres database |
| Who | One person | A team, each signed in |
| Tools, views, skills | The same | The same |

Both serve the same core: a client sees the same tools, schemas, views and skills, and only
where identity comes from and where the data lives differ.

## Tools

46 tools, in these families:

| Family | Tools |
|---|---|
| Resolve and import | `find_or_create_organization`, `find_or_create_contact`, `import_organizations`, `import_contacts` (up to 500 rows a call) |
| Read and list | `get_contact`, `get_organization`, `get_opportunity`, `list_contacts`, `list_organizations`, `list_opportunities`, `list_interactions`, `count_records`, `resolve_external_refs` |
| Change | `update_contact`, `update_organization`, `update_opportunity`, `log_interaction` |
| Duplicates, hide and delete | `merge_contacts`, `merge_organizations`, `archive_contact`, `archive_organization`, `delete_contact`, `delete_organization` |
| Deals and the pipeline | `create_opportunity`, `move_opportunity`, `get_pipeline`, `define_pipeline_stage`, `update_pipeline_stage` |
| Custom fields | `define_field`, `update_field`, `list_fields` |
| Lookalike companies | `link_similar_organizations`, `list_similar_organizations`, `promote_organization` |
| Evidence: signals | `record_signal`, `withdraw_signal`, `update_signal`, `get_signal`, `list_signals`, `list_signal_types`, `define_signal_type`, `update_signal_type` |
| Evidence: proof points | `record_proof_point`, `verify_proof_point`, `get_proof_point`, `list_proof_points` |

A few rules shape all of them:

- **Resolve before you write.** `find_or_create_*` matches by external reference, then domain
  or email, then name, and a match fills only empty fields, so repeating a call converges on
  one record.
- **Every result carries a `next_step`** telling the agent what to do with it, and every
  failure is one error envelope with a `next_step` that names the fix.
- **Listings query, they don't walk.** `list_*` take a free-text `query`, a `filter` and
  `sort` on any core, custom, related or rollup field, and `columns`, and page with a cursor.
- **Nothing is removed.** A delete marks a record so no read finds it; archiving is the
  reversible hide.

The server also serves two skills (`skill://people/SKILL.md` and
`skill://people-evidence/SKILL.md`) with worked examples for hosts that load skills.

## Views

Four views, `ui://people/contacts`, `organizations`, `opportunities` and `settings`, render
in Claude, ChatGPT and other hosts that support [MCP Apps](https://modelcontextprotocol.io/).
Ask for your contacts, a company or the pipeline, and the host shows the view for what the
agent read: the list, or the one record. Each record also has an address
(`people://contacts/<id>` and so on) that a host can open in a view. The views read and change
data only through the same tools.

## More

- [`docs/architecture.md`](docs/architecture.md): runtimes, the data model, deletion and
  archiving, the surface, errors and annotations.
- [`docs/local.md`](docs/local.md): the database file, several processes, the HTTP token,
  upgrades, backups, and what differs on SQLite.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): building and testing.

## Licence

MIT. See [`LICENSE`](LICENSE).
