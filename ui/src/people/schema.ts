import type { DescribeFieldsResult } from "../types";

/** How a field compares, which decides the control its filter gets. */
export type Kind = "text" | "select" | "number" | "date" | "timestamp" | "boolean" | "multi_select" | "list";

export type Group = "Deal" | "Person" | "Company" | "Signals" | "Activity";

/** The record kinds a list can show. */
export type Entity = "contact" | "organization" | "opportunity";

/** One field path a list can show, filter, and sort on. */
export interface FieldSpec {
  path: string;
  label: string;
  kind: Kind;
  group: Group;
  options?: string[];
  /** A workspace-defined field, marked as such wherever it is listed. */
  custom: boolean;
  description?: string;
  /** Offered in the filter panel as a set of values with counts. */
  facet: boolean;
  /** An admin marked this field to open as a column. */
  defaultColumn?: boolean;
}

export const GROUPS: Group[] = ["Deal", "Person", "Company", "Signals", "Activity"];

/** What differs between a list of people and a list of organizations. */
export interface EntityConfig {
  entity: Entity;
  listTool: string;
  /** The key the list tool returns its rows under. */
  rowsKey: string;
  noun: string;
  plural: string;
  newLabel: string;
  searchPlaceholder: string;
  /** The text paths the search box matches against. */
  searchPaths: string[];
  /** Paths a row needs beyond its visible columns: what the name and email cells render with. */
  alwaysFetched: string[];
  /** A cell that draws a second value with its own; an export gives that value its own column. */
  companions: Record<string, string>;
  defaultColumns: (schema: FieldSpec[]) => string[];
}

export const ENTITIES: Record<Entity, EntityConfig> = {
  contact: {
    entity: "contact",
    listTool: "list_contacts",
    rowsKey: "contacts",
    noun: "person",
    plural: "people",
    newLabel: "New person",
    searchPlaceholder: "Search name, company, email",
    searchPaths: ["name", "email", "organization.name"],
    alwaysFetched: ["name", "title", "email", "email_status", "organization.domain"],
    companions: {
      name: "title",
      "organization.name": "organization.domain",
      email: "email_status",
    },
    defaultColumns: (schema) => defaultContactColumns(schema),
  },
  organization: {
    entity: "organization",
    listTool: "list_organizations",
    rowsKey: "organizations",
    noun: "organization",
    plural: "organizations",
    newLabel: "New organization",
    searchPlaceholder: "Search name, domain",
    searchPaths: ["name", "domain"],
    alwaysFetched: ["name", "domain"],
    companions: { name: "domain" },
    defaultColumns: (schema) => defaultOrganizationColumns(schema),
  },
  opportunity: {
    entity: "opportunity",
    listTool: "list_opportunities",
    rowsKey: "opportunities",
    noun: "deal",
    plural: "deals",
    newLabel: "New deal",
    searchPlaceholder: "Search deal, account, contact",
    searchPaths: ["title", "organization.name", "contact.name"],
    // What a card and the title cell draw with, and what a stage move re-reads.
    alwaysFetched: [
      "title",
      "organization.name",
      "stage",
      "outcome",
      "value",
      "currency",
      "probability",
      "weighted_value",
      "close_date",
    ],
    companions: { title: "organization.name" },
    defaultColumns: (schema) => defaultOpportunityColumns(schema),
  },
};

// Labels for the paths the server names. A path with no entry here still appears,
// under its key with the underscores opened up.
const LABELS: Record<string, string> = {
  name: "Person",
  title: "Title",
  email: "Email",
  email_status: "Email status",
  phone: "Phone",
  status: "Stage",
  lead_source: "Lead source",
  source: "Source",
  created_at: "Added",
  "organization.name": "Company",
  "organization.domain": "Domain",
  "organization.industry": "Industry",
  "organization.region": "Region",
  "organization.headquarters": "Headquarters",
  "organization.employee_range": "Employee range",
  "signals.active": "Live signals",
  "interactions.count": "Interactions",
  "interactions.last_at": "Last touch",
  "contacts.count": "People",
};

// A deal's own keys, and its contact's seen from the deal.
const OPPORTUNITY_LABELS: Record<string, string> = {
  title: "Deal",
  stage: "Stage",
  outcome: "Outcome",
  value: "Value",
  currency: "Currency",
  probability: "Probability",
  weighted_value: "Weighted",
  close_date: "Close date",
  closed_at: "Closed",
  source: "Source",
  created_at: "Added",
  "contact.name": "Contact",
  "contact.title": "Contact title",
  "contact.email": "Contact email",
  "organization.name": "Account",
};

// The same core keys, seen from an organization's own list.
const ORGANIZATION_LABELS: Record<string, string> = {
  name: "Organization",
  domain: "Domain",
  website: "Website",
  industry: "Industry",
  region: "Region",
  headquarters: "Headquarters",
  employee_range: "Employee range",
  revenue_range: "Revenue range",
  status: "Status",
  source: "Source",
  created_at: "Added",
};

// Core paths left out of the list: ids, timestamps no one filters by, and the paths
// that restate another (`effective_status` is `status`).
const HIDDEN = new Set([
  "organization_id",
  "effective_status",
  "updated_at",
  "email_checked_at",
  "organization.status",
  "organization.source",
  "organization.website",
  "organization.revenue_range",
  "organization.created_at",
  "organization.updated_at",
]);

// Core paths an organization's own list leaves out.
const ORGANIZATION_HIDDEN = new Set(["updated_at"]);
// A deal's list leaves out its ids and the contact's own ids and stage.
const OPPORTUNITY_HIDDEN = new Set([
  "organization_id",
  "primary_contact_id",
  "updated_at",
  "contact.organization_id",
  "contact.status",
  "contact.effective_status",
  "contact.email_checked_at",
  "contact.phone",
  "contact.lead_source",
  "contact.source",
  "contact.created_at",
  "contact.updated_at",
  ...ORGANIZATION_HIDDEN,
  "organization.status",
  "organization.source",
  "organization.website",
  "organization.revenue_range",
  "organization.created_at",
  "organization.updated_at",
]);

// Open-string core fields that hold a handful of values in practice, so they
// filter better as a set of counted options than as a text box.
const TEXT_FACETS = new Set([
  "status",
  "lead_source",
  "source",
  "organization.industry",
  "industry",
  "region",
  "employee_range",
  "revenue_range",
  "currency",
]);

function humanize(key: string): string {
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function groupOf(path: string, entity: Entity): Group {
  if (path.startsWith("signals.")) return "Signals";
  if (path.startsWith("interactions.") || path.startsWith("contacts.")) return "Activity";
  if (path.startsWith("organization.") || entity === "organization") return "Company";
  if (path.startsWith("contact.")) return "Person";
  return entity === "opportunity" ? "Deal" : "Person";
}

function spec(entity: Entity, path: string, kind: string, extra: Partial<FieldSpec> = {}): FieldSpec {
  const k = kind as Kind;
  const labels =
    entity === "organization"
      ? { ...LABELS, ...ORGANIZATION_LABELS }
      : entity === "opportunity"
        ? { ...LABELS, ...OPPORTUNITY_LABELS }
        : LABELS;
  return {
    path,
    label: labels[path] ?? humanize(path.split(".").pop() ?? path),
    kind: k,
    group: groupOf(path, entity),
    custom: false,
    facet: k === "select" || k === "multi_select" || k === "list" || k === "boolean" || TEXT_FACETS.has(path),
    ...extra,
  };
}

/**
 * Every path a list offers, from one `list_fields` read. For people: the
 * contact's core and custom fields, its organization's under `organization.`,
 * and the rollups. For organizations: their own core, custom and rollup paths.
 */
export function buildSchema(described: DescribeFieldsResult, entity: Entity): FieldSpec[] {
  const contact = described.fields.contact;
  const organization = described.fields.organization;
  const specs: FieldSpec[] = [];
  const own = described.fields[entity];
  for (const core of own?.core ?? []) specs.push(spec(entity, core.key, core.type));
  for (const field of own?.custom ?? []) {
    specs.push(
      spec(entity, field.key, field.type, {
        label: field.label,
        options: field.options,
        custom: true,
        description: field.description,
        defaultColumn: field.default_column,
      }),
    );
  }
  if (entity !== "organization") {
    for (const core of organization?.core ?? []) specs.push(spec(entity, `organization.${core.key}`, core.type));
    for (const field of organization?.custom ?? []) {
      specs.push(
        spec(entity, `organization.${field.key}`, field.type, {
          label: field.label,
          options: field.options,
          custom: true,
          description: field.description,
          defaultColumn: field.default_column,
        }),
      );
    }
  }
  if (entity === "opportunity") {
    // The primary contact's own fields; the server offers no contact rollups here.
    for (const core of contact?.core ?? []) specs.push(spec(entity, `contact.${core.key}`, core.type));
    for (const field of contact?.custom ?? []) {
      specs.push(
        spec(entity, `contact.${field.key}`, field.type, {
          label: field.label,
          options: field.options,
          custom: true,
          description: field.description,
          defaultColumn: field.default_column,
        }),
      );
    }
  }
  for (const rollup of own?.rollups ?? []) {
    specs.push(spec(entity, rollup.path, rollup.type, { description: rollup.description }));
  }
  const hidden = { contact: HIDDEN, organization: ORGANIZATION_HIDDEN, opportunity: OPPORTUNITY_HIDDEN }[entity];
  const byPath = new Map(specs.filter((s) => !hidden.has(s.path)).map((s) => [s.path, s]));
  for (const path of ["email_status", "contact.email_status"]) {
    const emailStatus = byPath.get(path);
    if (emailStatus) emailStatus.options = ["valid", "risky", "unknown", "invalid"];
  }
  const outcome = byPath.get("outcome");
  if (outcome) outcome.options = ["open", "won", "lost"];
  return [...byPath.values()];
}

/** The most paths a listing request may name; the server refuses more (`query.MAX_COLUMNS`). */
export const MAX_REQUEST_COLUMNS = 30;

/**
 * The custom columns a list opens with: every field an admin marked, then the
 * first select fields defined until there are `slots`.
 */
function customColumns(fields: FieldSpec[], slots: number): string[] {
  const marked = fields.filter((s) => s.custom && s.defaultColumn);
  const selects = fields.filter((s) => s.custom && !s.defaultColumn && s.kind === "select");
  return [...marked, ...selects.slice(0, Math.max(0, slots - marked.length))].map((s) => s.path);
}

/** A list's opening columns, less the last custom ones when the request they make would pass the server's limit. */
function withinRequestLimit(columns: string[], custom: string[], alwaysFetched: string[]): string[] {
  const excess = new Set([...columns, ...alwaysFetched]).size - MAX_REQUEST_COLUMNS;
  if (excess <= 0) return columns;
  const dropped = new Set(custom.slice(-excess));
  return columns.filter((path) => !dropped.has(path));
}

/**
 * The columns a fresh people list opens with: who, where, the custom columns
 * (company fields first, since they are what a list is usually segmented by),
 * then signals, email, and last touch.
 */
function defaultContactColumns(schema: FieldSpec[]): string[] {
  const has = (path: string) => schema.some((s) => s.path === path);
  const selects = customColumns(
    [...schema].sort((a, b) => Number(b.group === "Company") - Number(a.group === "Company")),
    2,
  );
  return withinRequestLimit(
    ["name", "organization.name", ...selects, "signals.active", "email", "interactions.last_at"].filter(has),
    selects,
    ENTITIES.contact.alwaysFetched,
  );
}

/** A deal list opens on the deal, where it is, what it is worth, and when it closes. */
function defaultOpportunityColumns(schema: FieldSpec[]): string[] {
  const has = (path: string) => schema.some((s) => s.path === path);
  const selects = customColumns(
    schema.filter((s) => !s.path.includes(".")),
    1,
  );
  return withinRequestLimit(
    [
      "title",
      "stage",
      "value",
      "probability",
      "weighted_value",
      ...selects,
      "close_date",
      "interactions.last_at",
    ].filter(has),
    selects,
    ENTITIES.opportunity.alwaysFetched,
  );
}

/** An organization list opens on who, what they do, its custom columns, signals and headcount of people. */
function defaultOrganizationColumns(schema: FieldSpec[]): string[] {
  const has = (path: string) => schema.some((s) => s.path === path);
  const selects = customColumns(schema, 2);
  return withinRequestLimit(
    ["name", "industry", ...selects, "signals.active", "contacts.count", "created_at"].filter(has),
    selects,
    ENTITIES.organization.alwaysFetched,
  );
}
