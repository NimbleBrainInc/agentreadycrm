---
name: people
description: Working the CRM well beyond the server's rules - examples of resolving, importing, filtering and counting, external references, and what to do when a field is missing. Read when a task spans several records or a list.
metadata:
  nimblebrain:
    loading-strategy: dynamic
    tool-affinity:
      - find_or_create_contact
      - find_or_create_organization
      - import_contacts
      - import_organizations
      - resolve_external_refs
      - list_contacts
      - list_organizations
      - count_records
      - update_contact
      - update_organization
      - define_field
      - merge_contacts
      - merge_organizations
---

# Working the CRM

The server's instructions hold the rules (resolve before you write, field or dossier,
query rather than walk). This adds the patterns behind them.

## Bringing records in

- One person or company: `find_or_create_*` with everything you know in one call.
- A list: `import_contacts` / `import_organizations`, up to 500 rows a call. Rows that fail
  come back under `skipped` with their index; fix those and import them again, since the
  rest are saved and a repeat changes nothing.
- From another system: put its id for each record in `external_refs` as a URI
  (`crm://companies/123`, or its record URL), never in a field. Before fetching from the
  source, `resolve_external_refs` says which references are already here, so you fetch only
  the missing ones.

## Filtering and counting

People at strong-fit companies with a live signal and a valid email:

```json
{"and": [
  {"field": "organization.fit", "op": "eq", "value": "strong"},
  {"field": "signals.active", "op": "gte", "value": 1},
  {"field": "email_status", "op": "eq", "value": "valid"}
]}
```

"How many strong-fit companies, by industry" is one `count_records(entity="organization",
field="industry", filter=...)`, not a walk. A refused filter names the paths, operators and
options that would have worked; use them rather than guessing again. Pass `columns` whenever
you read more than one page.

## When a field is missing

If the field you need is not in `list_fields` and you cannot define it, tell the user which
field is missing. Do not store the value in a dossier or another field meanwhile: it could
not be filtered, and the next agent would not find it.

## Showing a record

To show someone a contact, organization or deal, open the CRM at the record's `uri` (with
the host's open-app tool, where it has one) rather than describing where to click.
