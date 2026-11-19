# Architecture

Agent Ready CRM is one Python distribution, `agentreadycrm`, with two import packages:

| Package | Holds |
|---|---|
| `mcp_people` | The core: wire models, ids and match keys, tables, the store, the 46 tools, the two skills, the views' resources, and the runtime contract |
| `mcp_people_local` | The local runtime: one SQLite file, one principal, stdio and loopback HTTP, and its own schema history |

The wheel also carries the built UI (`ui/`, packed by `hatch_build.py`). The core is the same
wherever the server runs. What differs between places it runs is a small set of
implementations, a **runtime**.

## Runtimes

A runtime implements the `Runtime` protocol in `mcp_people/runtime.py`:

| Member | Answers |
|---|---|
| `identity()` | Who is calling. Returns a `Scope` (`tenant_id`, the active `workspace_id`, the `allowed_workspaces` it may read, optional `user_id` and `correlation_id`), or raises `AuthError` when no verified identity exists |
| `auth()` | The FastMCP auth provider that gates the HTTP transport, or `None` |
| `session(scope)` | One database transaction for that scope. May raise `Overloaded` on entry, when it has no capacity for the call |
| `limits(scope)` | What the scope may hold (a `Limits`, below), or `None` for no limits |
| `record(event, count)` | What to do with a domain event (a record created, a stage changed, a denied lookup): count it, log it, or ignore it |
| `attach(mcp)` | What the server serves beyond the core's tools and resources |
| `lifespan()` | Startup and shutdown work, such as bringing the schema to head |
| `wrap_http(app)` | The HTTP app as this runtime serves it: the middleware in front of the MCP endpoint |

Every tool does the same three things: it asks the runtime for the caller's identity, opens one
transaction through the runtime for that identity, and calls the store. **Identity never comes
from a tool argument**, and no result carries `tenant_id` or `workspace_id`. A call with no
verified identity fails with `unauthenticated` before any session opens.

**Configuration chooses the runtime.** Runtimes register under the `mcp_people.runtimes`
entry-point group, and `PEOPLE_RUNTIME` names one. The server refuses to start when it is
unset, unknown or not installed: it never infers one, because a server meant for many
accounts that fell back to a single-principal runtime would drop its isolation. This package
registers `local`; `agentreadycrm`, its command, selects it and refuses any other value.

**The local runtime has one principal**, so SQLite is safe for it: there is no row-level
security, and a read by id carries no account predicate. A runtime with more than one principal
runs on Postgres with row-level security forced on every table, and the store's queries rely
on it. The tables carry `tenant_id` and `workspace_id` on every row for that reason, and their
Postgres forms (JSONB, GIN indexes on custom fields, native `ON CONFLICT` and `now()`) are kept
beside the SQLite ones in `mcp_people/db/dialect.py`.

### Limits

A runtime that serves many accounts can bound what one stores. `limits(scope)` returns a
`Limits`, and the store enforces each field that is not `None`; the local runtime returns
`None`, so a local install has no limits. The values are the deployment's configuration, not
the core's.

| Field | Counts | Bound by |
|---|---|---|
| `max_contacts`, `max_organizations` | Records not deleted. An archived record counts, and so do a discovered organization and the duplicate a merge archives | Every create of one: `find_or_create_contact`, `find_or_create_organization`, the imports, a contact's new organization, `link_similar_organizations` |
| `max_contacts_created`, `max_organizations_created` | Every record ever created, deleted ones included | The same creates. Deleting frees live room but the record stays stored, so this bounds delete-and-recreate |
| `max_open_opportunities` | Open deals: not deleted, in a stage whose outcome is neither won nor lost. The outcome is the stage row's, a retired stage's included; a stage the pipeline does not name is open | `create_opportunity` into an open stage, and `move_opportunity` from a won or lost stage into an open one |
| `max_opportunities_created` | Every deal ever created, closed and deleted ones included | `create_opportunity` |
| `max_interactions`, `max_signals`, `max_proof_points` | Every record ever created | `log_interaction`, `record_signal`, `record_proof_point` |
| `max_custom_fields` | Every field definition across the three entities, retired ones included: a retired key stays taken | `define_field` |
| `max_pipeline_stages`, `max_signal_types` | Every stage or type ever defined, the seeded ones included | `define_pipeline_stage`, `define_signal_type` |
| `max_dossier_bytes` | One dossier, in UTF-8 bytes as it would be stored | `dossier` and `dossier_append` on the three update tools; for an append, the joined text |
| `max_import_rows` | The rows of one call | `import_contacts`, `import_organizations` |

**Only a create counts, and a reopen.** A match (by reference, key or idempotency key) is never
refused, and neither is an edit, a merge, an archive, a withdrawal or a delete: each frees room
or leaves it as it was. A move is refused only when it takes a deal from a won or lost stage
into an open one, which makes it open again; moving a deal to a won or lost stage frees open
room. The deal's current outcome is read under the account's lock, so a concurrent move cannot
change it between the read and the count. A call whose match would create an organization to wire a contact to is a
create of that organization. The workspace's seeded pipeline and signal types are never
refused, even past a limit below their number; the `define_*` call after them is.

**A count runs in the write's own transaction, after the create**, and a write over it raises,
so the transaction rolls back and nothing the call did is kept. Before anything it writes, a
limited write takes a transaction-scoped lock on its account (`serialize_on` in
`mcp_people/db/dialect.py`: an advisory lock on Postgres; SQLite's `BEGIN IMMEDIATE` already
serializes writers), so two concurrent creates cannot both take the last slot. The count runs
under the caller's session, so row-level security still applies: where it scopes a session to
one workspace, a limit holds per workspace. When a create passes a lifetime and a live count,
the refusal names the lifetime one, since freeing live room would not help.

**Batches.** An import longer than `max_import_rows` is refused whole, before any row runs,
as the core's own cap refuses a longer one. Inside an import, a row that would create past a
count is rolled back alone and reported under `skipped`, its group carrying `limit`, and the
other rows commit: the import's per-row contract. `link_similar_organizations` skips a new
company the same way, counts it in `skipped` and names the limit as `limit`; a company the
workspace already holds still links. A dossier past `max_dossier_bytes` is refused before it
is written, and the core's own cap on a dossier applies as well.

**A refusal** is `invalid_request` with `limit` naming the `Limits` field, so a client can tell
it from a bad argument without a new code. Its `next_step` names what frees room (deleting
records no longer worked, closing decided deals, shortening the dossier, splitting the import),
says when nothing does, and names the local install, which has no limits. It is a denied attempt in
`people_audit`, and the runtime records the field's event (`<field>_refused`). Limits add no
argument and no tool, so the surface is the same under every runtime.

## Data model

Every record has an opaque, prefixed id (`org_`, `ct_`, `ix_`, `op_`, `sg_`, `pp_`, ...) minted
in the store, never a database sequence.

| Table | Holds |
|---|---|
| `organization` | A company: name, domain, website, industry, region, firmographics, a research dossier, custom fields, a status (`active`, or `discovered` for a lookalike not yet promoted) |
| `contact` | A person: name, email, phone, title, the organization they work at, a lifecycle stage, an email status, a dossier, custom fields |
| `interaction` | A touch with a contact (meeting, email, call, note, research), with its direction, optionally against a deal. It keeps the contact's organization as it was when logged |
| `opportunity` | A deal on an organization or a contact: value, currency, stage, close time, a dossier, custom fields |
| `pipeline_stage` | A workspace's stages in order, each with a default probability and an outcome (open, won, lost) |
| `organization_edge` | The org-to-org graph; `similar_to` holds lookalikes with their similarity |
| `signal`, `signal_type`, `signal_evidence_for` | An attested fact about an account (claim, verbatim quote, source URL, dates), the workspace's vocabulary of signal types, and whom each signal is evidence for |
| `proof_point` | A citable claim about your own work, with whether anyone has verified it |
| `field_def` | A custom field on contacts, organizations or deals: type, options, range, default column |
| `external_ref` | Another system's URI for a record, matched before any other key |
| `people_audit` | The append-only change log: every write, and every denied or failed attempt |

**Open strings, not enums.** A contact's stage, `lead_source`, an interaction's `kind`, an
industry and a source are plain text, so the agents writing them can extend them without a
migration. A contact's displayed stage is a floor derived at read time from facts the CRM
holds (a dossier, an outbound or inbound touch), never a write.

**Dedupe is the core contract.** `find_or_create_*` and the imports match by external
reference, then the normalized domain or email, then the name (and, for a contact, the
organization). The match keys (`name_key`, `email_key`, `domain_key`) are computed by the
application with Unicode folding and stored, and partial unique indexes on them make two
concurrent creates converge on one row: an insert that loses the race re-selects the winner's
row. A match fills only empty fields, except that explicitly creating an organization that was
only `discovered` promotes it.

**Custom fields** live in one JSON column per table, each value with who set it, when and why.
A definition's type validates every write, and a listing can filter, sort, project and count
by any custom field the same way as a core one.

## Deletion and archiving

**No tool removes a row.** `delete_contact` and `delete_organization` set `deleted_at` on the
record, on the duplicates merged into it, and on what exists only for it (its interactions,
signals, proof points and external references; a company's deals only when asked). No value is
rewritten and no link is cleared. `confirm` must repeat the id. Deleting a company is refused
while a live contact belongs to it, and, unless asked, while a live deal names it.

- **Every read leaves a deleted row out**, and reads a link from a live record to a deleted
  one as absent. `tests/test_delete_reads.py` calls every read-only tool against a deleted
  record, so a read added later fails until it is covered.
- **A deleted record holds no key.** Every unique index on these tables carries
  `deleted_at IS NULL`, so a new record may take the email, domain, name or reference a
  deleted one held.
- **A link row that goes away is marked the same way**: a detached external reference, a
  lookalike edge a merge drops, and a duplicate signal a merge folds into the one kept.
- **A vocabulary entry retires instead.** A custom field, a pipeline stage and a signal type
  carry `retired_at` and an optional replacement: records that hold the key still read it,
  and the key stays taken.
- `tests/test_no_row_removed.py` fails on a `DELETE` or `TRUNCATE` anywhere in the shipped
  code outside the migrations.

**Archiving is the reversible hide.** `archive_contact` and `archive_organization` set
`archived_at`, which takes a record out of the default listings and counts; `archived=false`
brings it back.

**A merge moves everything onto the record kept, then archives its source.** A company's
contacts, interactions, deals, signals, proof points, external references and lookalikes move,
and each signal filed as evidence for the source becomes evidence for the record kept (the
source's evidence row is kept as removed, so a reader of changes for either party sees the
move). A signal both hold, with the same source URL and type, is kept once with the purposes of
both; the other copy is marked, the target's unless only the source's is active. A person's
interactions, the signals about them, the deals naming them as contact and their references
move. Erasing a person's data on request is an
administrative process outside the tools.

## The surface

`tests/golden/surface.json` freezes what a client sees: every tool with its schemas and
annotations, the resources and templates, the capabilities, the instructions, and a digest of
each skill. Every runtime must serve it unchanged; the local runtime's is read over stdio from
a separate process. `tests/test_public_surface.py` holds the contract (names, schemas,
annotations, URIs) apart from the prose, and checks that no prose a client reads names
anything but this server.

- **Names** are `verb_noun`: `get_` reads one record, `list_` a paged collection, `count_` an
  aggregate; `define_` adds vocabulary and `update_` changes it; a transition verb (`move`,
  `promote`, `verify`, `withdraw`, `merge`, `delete`) owns its side effects.
- **Every result carries `next_step`**: what to do with the result, naming the tool to call.
- **Listings are keyset-paged.** A list tool takes `cursor` and returns `next_cursor`; the
  store owns the cursor (`pgkeyset`, pinned exactly, and `tests/golden/paging.txt` freezes
  the cursors agents already hold). The first page carries `total`.
- **Record addresses.** Each contact, organization, deal, signal and proof point has an
  address, `people://<kind>/<id>`, carried as `uri` on every record a tool returns. Each is a
  readable resource that runs the matching `get_*` tool under the caller's identity.
- **Views.** `ui://people/contacts`, `organizations`, `opportunities` and `settings` are MCP
  Apps resources, one self-contained HTML file with the view named on its root element. The
  read tools bind the first three, so a host renders a view for the call (see
  [MCP Apps hosts](#mcp-apps-hosts)); a host with placements of its own also opens a view by
  its URI or a record by its address.
- **Server Card.** Over streamable HTTP the server serves its MCP Server Card at
  `<mcp-url>/server-card`: its name (`com.agentreadycrm/agentreadycrm`), version and website as
  `serverInfo` reports them, a title and description, and the placements a NimbleBrain host
  reads to put the views in its navigation when the server is added by URL: contacts,
  organizations and deals under the sidebar's Apps, settings on the connector's settings page.
  Over stdio nothing is served.
- **Writes announce, reads don't.** A tool that changes stored data sends one
  `notifications/resources/list_changed`; a read sends none, even one that writes an audit
  row, or every view would refresh itself forever.
- **Both protocol eras.** The server serves the 2026-07-28 era and the session-based eras from
  one process (`tests/test_era.py`, `tests/test_capabilities.py`).

## MCP Apps hosts

Claude, ChatGPT and other MCP Apps hosts render a view for a tool call: the host renders the
resource the called tool names in `_meta.ui.resourceUri`, inline in the conversation, and hands
the view the call's input and result. A host renders a view for every call to a bound tool, so
a tool is bound only where its call is the user asking to see something: six read tools, each
visible to the model and to the app, and each description says which view it opens.

| Tool | View | Opens on |
|---|---|---|
| `list_contacts` | contacts | The list, searching for the call's `query` |
| `get_contact` | contacts | That contact, with its open deals |
| `list_organizations` | organizations | The list, searching for the call's `query` |
| `get_organization` | organizations | That organization, with its open deals |
| `get_pipeline` | opportunities | The board |
| `get_opportunity` | opportunities | That deal |

Two reads are left unbound because an agent calls them as lookups in the middle of other work,
where a view would interrupt it. `list_opportunities` finds deals (its description sends "show
the pipeline" to `get_pipeline`), and `list_fields` is the lookup an agent makes before
filtering or setting a field, so the settings view is bound to no tool; a host with placements
opens settings by its URI. `get_contact` and `get_organization` carry their newest open deals,
so showing a person or a company is one call and one view.

**The app opens on what the call read.** `ui/src/launch.ts` reads the tool's name, input and
result from the host: a `get_*` call opens its record (from the result's id, or from the
input's until the result arrives), and a `list_*` call opens the list searched for its `query`.
Each record opens once, so going back to the list stays there. A record opens at its top, and
going back puts the list where the reader left it. Opened with no tool context (a
host's own placement), the view opens on its list. Every datum the view shows, it reads through
the same tools, by way of the host.

**The bound URI is keyed by the build.** Tools bind to `ui://people/<view>/<build>`, where
`<build>` is the first 12 hex characters of the SHA-256 of the HTML that view serves. The HTML
is read once at start, and that copy is hashed and served. A host may cache a `ui://` resource
by its URI; with the hash in the URI, a new UI is a new URI, and a host fetches it instead of
rendering a stale copy. A template per bound view resolves any key to the current UI, so a
conversation or tool list that names an older build still renders. The listed resources keep
their plain URIs, so a host placement that names one keeps working across builds.

ChatGPT reads two keys of its own beside the MCP Apps ones, both defined in the OpenAI Apps SDK
reference (<https://developers.openai.com/plugins/reference>). Every tool carries
`openai/widgetAccessible: true`, since the views call reads and writes across the server from
their frame. The view resources and their templates carry
`openai/widgetDomain: "https://agentreadycrm.com"`, ChatGPT's alias for `ui.domain`; the alias
holds it because other hosts check `ui.domain` against domains of their own.

Each view asks for a border (`ui.prefersBorder`), which a host draws where the frame sits among
other content, and its frame's content security policy allows no outside origin, since the
bundle loads nothing from elsewhere. A host that renders a view inline sizes the frame to the
height the document reports, measured on `<html>` at `max-content`, where the view's content no
longer scrolls inside it. `<html>` carries a floor, so the frame shows a list or a record, and a
ceiling of the floor or the frame's own height, so the frame does not grow to a record's whole
length as it loads (a conversation kept scrolled to its end would then show the record from
partway down). An inline view keeps the floor's height and scrolls inside; a taller pane still
fills.

## Errors

A failure is a tool result flagged `isError`, with one body:

```json
{"error": {"code": "not_found", "message": "...", "retryable": false}, "next_step": "..."}
```

| Code | When |
|---|---|
| `not_found` | The id names nothing the caller can see. An absent record and one outside the caller's scope are indistinguishable. The envelope carries `entity` and `id` |
| `invalid_request` | An argument is wrong, or the write would pass one of the runtime's limits; nothing was changed. The message names the argument, or the envelope carries `limit` |
| `conflict` | A key another record holds, a delete something still depends on, or a change to a withdrawn signal. The envelope carries `field` |
| `unauthenticated` | The runtime established no identity. Returned before any database session opens |
| `rate_limited` | The runtime had no capacity for the call and ran nothing; a retry is safe |
| `internal_error` | Anything else. The message is fixed and generic; the details go to the server log |

A `next_step` names what to change, never "correct the argument" alone. A denied or failed
attempt is also written to `people_audit`, in a transaction of its own.

## Annotations

Every tool is annotated and closed-world: it acts on this server's database and nothing else.
Under the MCP specification an unannotated tool is treated as destructive and open-world.

| Kind | Tools | Read-only | Destructive | Idempotent |
|---|---|---|---|---|
| Read | `get_*`, `list_*`, `count_records`, `resolve_external_refs` | yes | | yes |
| Add, converging | `find_or_create_*`, `import_*`, `define_*`, `record_signal`, `link_similar_organizations` | no | no | yes |
| Add | `create_opportunity`, `log_interaction`, `record_proof_point` | no | no | no |
| Edit, with append | `update_contact`, `update_organization`, `update_opportunity` | no | yes | no |
| Edit | `update_field`, `update_pipeline_stage`, `update_signal_type` | no | yes | yes |
| Move | `move_opportunity`, `promote_organization`, `update_signal`, `archive_contact`, `archive_organization` | no | no | yes |
| Move, stamped | `verify_proof_point` | no | no | no |
| Withdraw or delete | `withdraw_signal`, `delete_contact`, `delete_organization` | no | yes | yes |
| Merge | `merge_contacts`, `merge_organizations` | no | yes | no |

A field edit overwrites values, so it is destructive; a move another call undoes is not. A
record update can append to the dossier on every call, so it is not idempotent;
`verify_proof_point` stamps the time of each call.

## Evidence

A **signal** is one fact about an account: a one-sentence claim, the verbatim quote that proves
it, the URL it came from, and when it was published and when the event happened. Its type comes
from the workspace's vocabulary (`define_signal_type`). Recording the same fact again converges
on one row; withdrawing a signal keeps it on record and takes it out of the default reads.
`list_signals` can also return the signals changed since a mark, oldest change first, so a
reader can follow the evidence as it moves.

A **proof point** is a claim about your own work (a result for a client, with a metric). It is
citable only once someone verifies it with `verify_proof_point`, which records who made the
call.
