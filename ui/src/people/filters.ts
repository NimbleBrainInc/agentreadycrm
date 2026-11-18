import type { FieldSpec } from "./schema";

/** One field's filter, as the panel edits it. */
export type FilterEntry =
  | { t: "values"; values: (string | boolean)[] }
  | { t: "range"; min?: number; max?: number }
  | { t: "dates"; from?: string; to?: string }
  | { t: "contains"; text: string }
  | { t: "preset"; id: string };

export type FilterState = Record<string, FilterEntry>;

export type Clause = { field: string; op: string; value: unknown } | { and: Clause[] } | { or: Clause[] };

interface Preset {
  id: string;
  label: string;
  clause: (path: string) => Clause;
}

// Start of the UTC day, so the clause a preset builds is the same on every
// render: a filter that changed by the millisecond would re-count every facet
// on each keystroke.
function daysAgo(days: number): string {
  const day = new Date(Date.now() - days * 86_400_000);
  day.setUTCHours(0, 0, 0, 0);
  return day.toISOString();
}

/** The choices a rollup offers in place of a free-form comparison. */
export const PRESETS: Record<string, Preset[]> = {
  "signals.active": [
    {
      id: "some",
      label: "Has a live signal",
      clause: (f) => ({ field: f, op: "gte", value: 1 }),
    },
    {
      id: "none",
      label: "None",
      clause: (f) => ({ field: f, op: "eq", value: 0 }),
    },
  ],
  "interactions.count": [
    {
      id: "never",
      label: "Never",
      clause: (f) => ({ field: f, op: "eq", value: 0 }),
    },
    {
      id: "once",
      label: "Once",
      clause: (f) => ({ field: f, op: "eq", value: 1 }),
    },
    {
      id: "more",
      label: "Twice or more",
      clause: (f) => ({ field: f, op: "gte", value: 2 }),
    },
  ],
  "contacts.count": [
    {
      id: "none",
      label: "No one yet",
      clause: (f) => ({ field: f, op: "eq", value: 0 }),
    },
    {
      id: "some",
      label: "At least one",
      clause: (f) => ({ field: f, op: "gte", value: 1 }),
    },
    {
      id: "several",
      label: "Three or more",
      clause: (f) => ({ field: f, op: "gte", value: 3 }),
    },
  ],
  "interactions.last_at": [
    {
      id: "7d",
      label: "Last 7 days",
      clause: (f) => ({ field: f, op: "gte", value: daysAgo(7) }),
    },
    {
      id: "30d",
      label: "Last 30 days",
      clause: (f) => ({ field: f, op: "gte", value: daysAgo(30) }),
    },
    {
      id: "older",
      label: "Older",
      clause: (f) => ({ field: f, op: "lt", value: daysAgo(30) }),
    },
    {
      id: "never",
      label: "Never",
      clause: (f) => ({ field: f, op: "is_set", value: false }),
    },
  ],
};

function entryClause(spec: FieldSpec, entry: FilterEntry): Clause | null {
  const field = spec.path;
  if (entry.t === "preset") {
    return PRESETS[field]?.find((p) => p.id === entry.id)?.clause(field) ?? null;
  }
  if (entry.t === "contains") {
    const text = entry.text.trim();
    return text ? { field, op: "contains", value: text } : null;
  }
  if (entry.t === "range" || entry.t === "dates") {
    const [low, to] = entry.t === "range" ? [entry.min, entry.max] : [entry.from, entry.to];
    // A day picked as the end of a timestamp range means through the end of that day.
    const high = spec.kind === "timestamp" && typeof to === "string" ? `${to}T23:59:59.999Z` : to;
    if (low !== undefined && high !== undefined) return { field, op: "between", value: [low, high] };
    if (low !== undefined) return { field, op: "gte", value: low };
    if (high !== undefined) return { field, op: "lte", value: high };
    return null;
  }
  if (entry.values.length === 0) return null;
  if (spec.kind === "boolean") return { field, op: "eq", value: entry.values[0] };
  if (spec.kind === "multi_select" || spec.kind === "list") return { field, op: "has", value: entry.values };
  return { field, op: "in", value: entry.values };
}

/**
 * The filter the list tools take, from the panel's state and the search box.
 * `except` leaves one field out, which is how a facet counts its own options
 * under every other filter.
 */
export function toFilter(
  state: FilterState,
  schema: FieldSpec[],
  search: string,
  searchPaths: string[],
  except?: string,
): Clause | undefined {
  const clauses: Clause[] = [];
  for (const spec of schema) {
    const entry = state[spec.path];
    if (!entry || spec.path === except) continue;
    const clause = entryClause(spec, entry);
    if (clause) clauses.push(clause);
  }
  const q = search.trim();
  if (q) {
    clauses.push({
      or: searchPaths.map((field) => ({ field, op: "contains", value: q })),
    });
  }
  if (clauses.length === 0) return undefined;
  return clauses.length === 1 ? clauses[0] : { and: clauses };
}

/** The value side of a filter chip. */
export function describeEntry(spec: FieldSpec, entry: FilterEntry): string {
  if (entry.t === "preset") return PRESETS[spec.path]?.find((p) => p.id === entry.id)?.label ?? "";
  if (entry.t === "contains") return `contains “${entry.text.trim()}”`;
  if (entry.t === "range" || entry.t === "dates") {
    const [low, high] = entry.t === "range" ? [entry.min, entry.max] : [entry.from, entry.to];
    if (low !== undefined && high !== undefined) return `${low} to ${high}`;
    return low !== undefined ? `from ${low}` : `up to ${high}`;
  }
  return entry.values.map((v) => (typeof v === "boolean" ? (v ? "Yes" : "No") : v)).join(", ");
}

/** Whether an entry narrows the list at all. */
export function isActive(spec: FieldSpec, entry: FilterEntry | undefined): boolean {
  return !!entry && entryClause(spec, entry) !== null;
}
