import type { FieldSpec } from "./schema";

/**
 * An import, in the browser: a file read into a table of text cells, its columns mapped to
 * the fields an import row takes, filtered, and cut into the batches the import tools
 * accept. Nothing here talks to the server; `ImportPage` sends the batches.
 */

/** The record kinds a list can be imported as. */
export type ImportEntity = "contact" | "organization";

/** A file's rows, the first being its headers, every cell as text. */
export interface Table {
  headers: string[];
  rows: string[][];
}

/** Rows per import call. The tool takes up to 500; a smaller batch holds its transaction briefly. */
export const BATCH_SIZE = 250;

/**
 * Where one column's values go: a core column the tool names, a custom field by key, or the
 * record's external reference, the column's value under a prefix the person gives.
 */
export type Target =
  | { kind: "core"; key: string; label: string }
  | { kind: "field"; spec: FieldSpec; key: string }
  | { kind: "ref"; key: "external_refs"; label: string };

/** The reference target: what another system calls the record, as `prefix + cell`. */
export const REF_TARGET: Target = { kind: "ref", key: "external_refs", label: "External reference" };

/** The server's rule for a reference: a lower-case scheme, a colon, and no whitespace. */
const REF_RULE = /^[a-z][a-z0-9+.\-]*:\S+$/;

/** Whether `prefix` makes a reference of a cell: `prefix + "123"` passes the server's rule. */
export function validRefPrefix(prefix: string): boolean {
  return REF_RULE.test(`${prefix.trim()}123`) && !prefix.trim().startsWith("people:");
}

/** The core columns each import tool takes, with the label a person reads. */
export const CORE_TARGETS: Record<ImportEntity, { key: string; label: string }[]> = {
  organization: [
    { key: "name", label: "Name" },
    { key: "domain", label: "Domain" },
    { key: "website", label: "Website" },
    { key: "industry", label: "Industry" },
    { key: "region", label: "Region" },
  ],
  contact: [
    { key: "name", label: "Name" },
    { key: "email", label: "Email" },
    { key: "phone", label: "Phone" },
    { key: "title", label: "Title" },
    { key: "organization_name", label: "Company name" },
    { key: "organization_domain", label: "Company domain" },
    { key: "lead_source", label: "Lead source" },
    { key: "status", label: "Status" },
    { key: "email_status", label: "Email status" },
  ],
};

/** What a row needs before it is worth sending: the tool skips one with neither. */
export const REQUIRED: Record<ImportEntity, [string, string]> = {
  organization: ["name", "domain"],
  contact: ["name", "email"],
};

/** Every place a column can go: the core columns, then the entity's own custom fields. */
export function importTargets(entity: ImportEntity, schema: FieldSpec[]): Target[] {
  const core: Target[] = [...CORE_TARGETS[entity].map((t) => ({ kind: "core" as const, ...t })), REF_TARGET];
  // Only the entity's own custom fields: a contact import cannot write its company's fields.
  const custom: Target[] = schema
    .filter((s) => s.custom && !s.path.includes("."))
    .map((spec) => ({ kind: "field", spec, key: spec.path }));
  return [...core, ...custom];
}

export function targetId(t: Target): string {
  return t.kind === "field" ? `fields.${t.key}` : t.key;
}

export function targetLabel(t: Target): string {
  return t.kind === "field" ? t.spec.label : t.label;
}

// ------------------------------------------------------------------------------------------
// Reading a file
// ------------------------------------------------------------------------------------------

/**
 * A CSV's cells (RFC 4180): quoted fields may hold commas, quotes (doubled) and line breaks;
 * rows end in LF or CRLF; a leading byte-order mark is dropped. Blank lines are skipped.
 */
export function parseCsv(text: string): string[][] {
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += ch;
      continue;
    }
    if (ch === '"' && field === "") quoted = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      if (row.some((c) => c !== "")) rows.push(row);
      row = [];
    } else field += ch;
  }
  row.push(field);
  if (row.some((c) => c !== "")) rows.push(row);
  return rows;
}

/** A spreadsheet cell as text: a date as YYYY-MM-DD, a number as written, an empty cell as "". */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? "" : value.toISOString().slice(0, 10);
  return String(value);
}

/**
 * A cell as a person typed it: the apostrophe a CSV this app writes puts before a value a
 * spreadsheet would run as a formula (see `csvCell`, and the list's export) is dropped.
 */
function unguard(text: string): string {
  return /^'[=+\-@\t\r]/.test(text) ? text.slice(1) : text;
}

/** The table from a file's rows: the first non-empty row is the headers, and every row is as wide as they are. */
export function toTable(cells: unknown[][]): Table {
  const texts = cells.map((r) => r.map((c) => unguard(cellText(c))));
  const start = texts.findIndex((r) => r.some((c) => c.trim() !== ""));
  const [head = [], ...body] = start < 0 ? [] : texts.slice(start);
  const headers = head.map((h, i) => h.trim() || `Column ${i + 1}`);
  const rows = body.map((r) => headers.map((_, i) => (r[i] ?? "").trim()));
  return { headers, rows };
}

// ------------------------------------------------------------------------------------------
// Mapping columns
// ------------------------------------------------------------------------------------------

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

/**
 * Header spellings a list commonly uses for each core column, beyond its own key and label.
 * By entity, because a "Company" column is the record's name in a list of companies and the
 * person's company in a list of people.
 */
const ALIASES: Record<ImportEntity, Record<string, string[]>> = {
  organization: {
    name: ["companyname", "company", "organization", "organisation", "account", "accountname"],
    domain: ["companydomain", "websitedomain", "url"],
    region: ["country", "location", "hq", "headquarters"],
  },
  contact: {
    name: ["fullname", "contactname", "personname"],
    email: ["emailaddress", "workemail", "e-mail"],
    title: ["jobtitle", "position", "role"],
    phone: ["phonenumber", "mobile"],
    organization_name: ["company", "companyname", "organization", "organisation", "account", "accountname"],
    organization_domain: ["companydomain", "domain", "website", "websitedomain"],
  },
};

/**
 * Each header's likeliest target, by its key, its label or a common spelling. A target is
 * given to one column at most, the first that matches it; an unmatched column is left out.
 */
export function guessMapping(headers: string[], targets: Target[], entity: ImportEntity): (string | null)[] {
  const taken = new Set<string>();
  const names = (t: Target) => {
    const aliases = t.kind === "core" ? (ALIASES[entity][t.key] ?? []) : [];
    return new Set([t.key, targetLabel(t), ...aliases].map(norm));
  };
  const pass = (h: string, exactOnly: boolean): string | null => {
    for (const t of targets) {
      const id = targetId(t);
      // A reference is never guessed: an id column is mapped to one only by the person,
      // who gives the prefix that says whose id it is.
      if (t.kind === "ref" || taken.has(id)) continue;
      const hit = exactOnly ? norm(targetLabel(t)) === norm(h) || norm(t.key) === norm(h) : names(t).has(norm(h));
      if (hit) {
        taken.add(id);
        return id;
      }
    }
    return null;
  };
  // Exact key or label first, so "Domain" goes to domain before an alias claims it.
  const first = headers.map((h) => pass(h, true));
  return first.map((m, i) => m ?? pass(headers[i], false));
}

// ------------------------------------------------------------------------------------------
// Filtering
// ------------------------------------------------------------------------------------------

export type FilterOp = "is" | "contains" | "is empty" | "is not empty";

export interface RowFilter {
  column: number;
  op: FilterOp;
  value: string;
}

/** Whether a row passes every filter; text compares without case. */
export function keeps(row: string[], filters: RowFilter[]): boolean {
  return filters.every(({ column, op, value }) => {
    const cell = (row[column] ?? "").toLowerCase();
    const want = value.trim().toLowerCase();
    if (op === "is empty") return cell === "";
    if (op === "is not empty") return cell !== "";
    if (op === "is") return cell === want;
    return cell.includes(want);
  });
}

// ------------------------------------------------------------------------------------------
// Building rows
// ------------------------------------------------------------------------------------------

const TRUE = new Set(["true", "yes", "y", "1"]);
const FALSE = new Set(["false", "no", "n", "0"]);

/**
 * A cell as the custom field's value: a boolean as true or false, a list or multi-select
 * split on semicolons, anything else as its text for the server to check. A boolean cell
 * that is neither stays text, so the server names it.
 */
export function fieldValue(spec: FieldSpec, text: string): unknown {
  if (spec.kind === "boolean") {
    const t = text.toLowerCase();
    if (TRUE.has(t)) return true;
    if (FALSE.has(t)) return false;
    return text;
  }
  if (spec.kind === "list" || spec.kind === "multi_select") {
    return text
      .split(";")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return text;
}

/** One import row from a file row, under the mapping; blank cells are left out. */
export function toImportRow(
  row: string[],
  mapping: (string | null)[],
  targets: Target[],
  refPrefix = "",
): Record<string, unknown> {
  const byId = new Map(targets.map((t) => [targetId(t), t]));
  const out: Record<string, unknown> = {};
  const fields: Record<string, unknown> = {};
  mapping.forEach((id, col) => {
    const t = id ? byId.get(id) : undefined;
    const text = row[col] ?? "";
    if (!t || text === "") return;
    if (t.kind === "core") out[t.key] = text;
    else if (t.kind === "ref") out.external_refs = [`${refPrefix.trim()}${text}`];
    else fields[t.key] = fieldValue(t.spec, text);
  });
  if (Object.keys(fields).length) out.fields = fields;
  return out;
}

/** The plan an import runs: the rows it sends (with each one's file row), and why others are left out. */
export interface Plan {
  rows: { line: number; row: Record<string, unknown> }[];
  filteredOut: number;
  missingRequired: number[];
}

/**
 * Which rows an import sends. A row the filters drop is not sent; one with neither of the
 * entity's required columns would only be skipped, so it is counted and kept back too.
 * `line` is the row's place in the file's body, so a failure names the row a person has.
 */
export function plan(
  table: Table,
  mapping: (string | null)[],
  targets: Target[],
  filters: RowFilter[],
  entity: ImportEntity,
  refPrefix = "",
): Plan {
  const [a, b] = REQUIRED[entity];
  const out: Plan = { rows: [], filteredOut: 0, missingRequired: [] };
  table.rows.forEach((cells, line) => {
    if (!keeps(cells, filters)) {
      out.filteredOut++;
      return;
    }
    const row = toImportRow(cells, mapping, targets, refPrefix);
    // A row with only a reference is sent: it matches a record that reference names, and
    // the server skips it, with its reason, when none does.
    if (row[a] === undefined && row[b] === undefined && row.external_refs === undefined) {
      out.missingRequired.push(line);
    } else {
      out.rows.push({ line, row });
    }
  });
  return out;
}

export function batches<T>(items: T[], size = BATCH_SIZE): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

// ------------------------------------------------------------------------------------------
// Results
// ------------------------------------------------------------------------------------------

/** One import call's answer (`import_organizations` / `import_contacts`). */
export interface ImportResult {
  received: number;
  created: number;
  matched: number;
  promoted: number;
  kept: { reason: string; rows: number[] }[];
  skipped: { reason: string; rows: number[] }[];
}

/** A file row the import did not save, or saved while keeping a value the record already held. */
export interface Outcome {
  line: number;
  reason: string;
}

/**
 * A batch's skipped and kept rows, named by the file row each came from. The tool indexes
 * into the batch it was sent; `sent[i].line` maps that back to the file.
 */
export function outcomes(result: ImportResult, sent: { line: number }[]): { skipped: Outcome[]; kept: Outcome[] } {
  const at = (groups: ImportResult["skipped"]) =>
    groups.flatMap((g) => g.rows.map((i) => ({ line: sent[i]?.line ?? -1, reason: g.reason })));
  return { skipped: at(result.skipped), kept: at(result.kept) };
}

function csvCell(text: string): string {
  // A spreadsheet runs a cell that opens with one of these as a formula.
  const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/**
 * The file rows named in `found`, as they were in the file, with a first column saying why:
 * a file a person can fix and import again. A row named twice keeps each reason.
 */
export function outcomesCsv(table: Table, found: Outcome[]): string {
  const reasons = new Map<number, string[]>();
  for (const { line, reason } of found) reasons.set(line, [...(reasons.get(line) ?? []), reason]);
  const lines = [["Reason", ...table.headers].map(csvCell).join(",")];
  for (const [line, why] of [...reasons].sort((x, y) => x[0] - y[0])) {
    lines.push([why.join("; "), ...(table.rows[line] ?? [])].map(csvCell).join(","));
  }
  return `﻿${lines.join("\r\n")}\r\n`;
}
