// Wire contracts mirroring the People server's Pydantic models and the
// documented tool return shapes. The UI never sees tenant_id/workspace_id.

export interface Organization {
  id: string;
  name: string;
  domain: string | null;
  website: string | null;
  industry: string | null;
  region: string | null;
  employee_range: string | null;
  revenue_range: string | null;
  headquarters: string | null;
  source: string | null;
  status: string;
  fields?: Record<string, unknown>;
  field_details?: Record<string, FieldDetails>;
  created_at: string;
  updated_at: string;
}

/** Who set a custom field, when, and why. Returned by the by-id reads only. */
export interface FieldDetails {
  by?: string;
  at?: string;
  why?: string;
}

export interface Contact {
  id: string;
  name: string;
  email: string | null;
  email_status?: string | null;
  email_checked_at?: string | null;
  phone: string | null;
  title: string | null;
  organization_id: string | null;
  lead_source: string | null;
  status: string;
  effective_status: string;
  /** When the contact was archived; absent when they are not. */
  archived_at?: string | null;
  source: string | null;
  fields?: Record<string, unknown>;
  field_details?: Record<string, FieldDetails>;
  created_at: string;
  updated_at: string;
}

export interface Interaction {
  id: string;
  contact_id: string;
  organization_id: string | null;
  kind: string;
  direction: string | null;
  summary: string;
  body: string | null;
  occurred_at: string;
  follow_up_at: string | null;
  created_at: string;
}

// A failed call as the views read it: `error` is the code. On the wire a failure is a tool
// result flagged `isError` whose body is `{error: {code, message, retryable, ...}, next_step}`;
// `useCall` (api.ts) turns it into this with `toApiError`, so a view checks `isApiError` and
// never reads the wire shape.
export interface ApiError {
  error: string;
  message?: string;
  next_step?: string;
  entity?: string;
  field?: string;
}

export interface ListContactsResult {
  contacts: Contact[];
  next_cursor: string | null;
  next_step?: string;
}

export interface ListOrganizationsResult {
  organizations: Organization[];
  next_cursor: string | null;
  next_step?: string;
}

// One page of a record's history, newest first, from `list_interactions`.
export interface ListInteractionsResult {
  interactions: Interaction[];
  next_cursor: string | null;
  next_step?: string;
}

// Opportunity — the pipeline entity. `stage` is a key of the
// workspace's pipeline; `outcome` (open / won / lost) is read from that stage.
export interface Opportunity {
  id: string;
  title: string;
  organization_id: string | null;
  primary_contact_id: string | null;
  stage: string;
  outcome: string;
  value: number | null;
  currency: string | null;
  /** The deal's own number when set, otherwise the stage's default. */
  probability: number | null;
  probability_is_own: boolean;
  /** value × probability / 100; null when either is missing. */
  weighted_value: number | null;
  close_date: string | null;
  closed_at: string | null;
  source: string | null;
  fields?: Record<string, unknown> | null;
  field_details?: Record<string, FieldDetails> | null;
  created_at: string;
  updated_at: string;
}

export interface GetOpportunityResult {
  opportunity: Opportunity;
  counts: { interactions: number };
  next_step?: string;
}

export interface ListOpportunitiesResult {
  opportunities: Opportunity[];
  /** Every row the filter matches. Returned with the first page only. */
  total?: number;
  next_cursor: string | null;
  next_step?: string;
}

// One stage of the workspace's pipeline, from `get_pipeline`, with the
// deals it holds and their value.
export interface PipelineStage {
  id: string;
  key: string;
  label: string;
  position: number;
  probability: number;
  outcome: string;
  deals: number;
  value_sum?: number;
  weighted_sum?: number;
  /** Set on a retired stage, which `get_pipeline` lists only under `include_retired`. */
  retired_at?: string;
  /** The live stage that takes a retired one's place. */
  replaced_by?: string;
}

export interface GetPipelineResult {
  stages: PipelineStage[];
  open_count: number;
  won_count: number;
  lost_count: number;
  total_value?: number;
  total_weighted?: number;
  next_step?: string;
}

// Similar Companies panel. `list_similar_organizations` / `promote_organization`
// return an `ApiError` on the structured unhappy path (guard with `isApiError`).
// `link_similar_organizations` is the agent's to call, not this app's — the UI only
// reads the graph and promotes a row.
export interface SimilarCompany {
  organization: Organization;
  similarity: number;
  discovered_at: string | null;
}

export interface ListSimilarOrganizationsResult {
  similar: SimilarCompany[];
  next_step?: string;
}


export interface PromoteOrganizationResult {
  ok: boolean;
  next_step?: string;
}

/** A failed result's body as an `ApiError`: the server's envelope, or the plain text a host or
 * a bridge hands over. */
export function toApiError(body: unknown): ApiError {
  if (typeof body === "string") return { error: "error", message: body || undefined };
  if (typeof body === "object" && body !== null) {
    const b = body as { error?: unknown; message?: unknown; next_step?: unknown };
    const nextStep = typeof b.next_step === "string" ? b.next_step : undefined;
    if (typeof b.error === "object" && b.error !== null) {
      const e = b.error as { code?: unknown; message?: unknown; entity?: unknown; field?: unknown };
      return {
        error: typeof e.code === "string" ? e.code : "error",
        message: typeof e.message === "string" ? e.message : undefined,
        next_step: nextStep,
        entity: typeof e.entity === "string" ? e.entity : undefined,
        field: typeof e.field === "string" ? e.field : undefined,
      };
    }
    if (typeof b.error === "string") {
      return { error: b.error, message: typeof b.message === "string" ? b.message : undefined, next_step: nextStep };
    }
  }
  return { error: "error" };
}

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { error?: unknown }).error === "string"
  );
}

/** Route by opaque id prefix: `ct_…`/`org_…`/`op_…`. */
export function kindOfId(id: string): "contact" | "organization" | "opportunity" | null {
  if (id.startsWith("ct_")) return "contact";
  if (id.startsWith("org_")) return "organization";
  if (id.startsWith("op_")) return "opportunity";
  return null;
}

// --- Custom fields and the filtered listings -------------

export interface FieldDef {
  entity: string;
  key: string;
  label: string;
  type: string;
  options?: string[];
  description?: string;
  defined_by?: string;
  created_at: string;
  updated_at: string;
  /** Set on a retired field, which only `list_fields(include_retired=true)` lists. */
  retired_at?: string | null;
  /** The live field that replaces a retired one: a bare key, else `organization.<key>` / `contact.<key>`. */
  replaced_by?: string | null;
  /** An admin's choice that the field opens as a column in its lists. */
  default_column?: boolean;
  /** A number field's inclusive range; absent is unbounded on that side. */
  minimum?: number | null;
  maximum?: number | null;
}

export interface DescribeFieldsResult {
  fields: Record<
    string,
    {
      core: { key: string; type: string; description: string }[];
      custom: FieldDef[];
      rollups: { path: string; type: string; description: string }[];
    }
  >;
  next_step?: string;
}

export interface UpdateFieldResult {
  field: FieldDef;
  action: "retired" | "restored" | "updated" | "unchanged";
  next_step?: string;
}

export interface DefineFieldResult {
  field: FieldDef;
  action: "created" | "matched";
  options_added?: string[];
  next_step?: string;
}

/** A row of a `columns=[…]` listing: the id, then one value per path asked for. */
export type Row = { id: string } & Record<string, unknown>;

/** A page of `list_contacts` or `list_organizations` under `columns`: rows under the entity's plural key. */
export interface ListRowsResult {
  contacts?: Row[];
  organizations?: Row[];
  opportunities?: Row[];
  /** Every row the filter matches. Returned with the first page only. */
  total?: number;
  next_cursor: string | null;
  next_step?: string;
}

export interface CountRecordsResult {
  field: string;
  values: { value: string | number | boolean | null; count: number }[];
  total: number;
  truncated: boolean;
  next_step?: string;
}

/** One of a record's open deals as a contact or organization read carries it. */
export interface OpenDeal {
  id: string;
  uri?: string;
  title: string;
  stage: string;
  /** A decimal, which the wire may carry as a string. */
  value?: number | string | null;
  currency?: string | null;
}

export interface GetContactResult {
  contact: Contact;
  counts: Record<string, number>;
  /** The newest open deals naming this person as their contact. */
  open_opportunities?: OpenDeal[];
  /** How many deals name them and are open, the ones carried included. */
  open_opportunities_total?: number;
  next_step?: string;
}

export interface GetOrganizationResult {
  organization: Organization;
  counts: Record<string, number>;
  /** The newest open deals on this account. */
  open_opportunities?: OpenDeal[];
  /** How many deals on the account are open, the ones carried included. */
  open_opportunities_total?: number;
  next_step?: string;
}

export interface Signal {
  id: string;
  /** The signal's address (`people://signals/…`): how another tool cites it. */
  uri: string;
  organization_id: string;
  contact_id?: string | null;
  claim: string;
  evidence_quote: string;
  source_url: string;
  /** The source's own date: for an article, when it was published. */
  source_date?: string | null;
  /** When the event itself happened, as the evidence states it; null when it states none. */
  event_date?: string | null;
  /** A key of the workspace's signal vocabulary. */
  signal_type: string;
  status: string;
  withdrawn_at?: string | null;
  withdrawn_reason?: string | null;
  created_at: string;
}

export interface ListSignalsResult {
  signals: Signal[];
  next_cursor?: string | null;
  next_step?: string;
}
