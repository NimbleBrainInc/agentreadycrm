// What the host opened this view for. An MCP Apps host renders a view for the read tool bound to
// it: `list_contacts` and `get_contact` open the contacts view, `list_organizations` and
// `get_organization` the organizations view, `list_opportunities`, `get_pipeline` and
// `get_opportunity` the deals. The host names the tool in `toolInfo` and delivers the call's
// input and result; any of the three may be missing. A host that opens the view from its own
// placement delivers none, and the view opens on its list.

/** What the view opens on: one record, a list searched for a query, or neither. */
export interface LaunchTarget {
  /** The record a `get_*` call read; the view shows it in place of the list. */
  recordId?: string;
  /** The `query` a `list_*` call searched; the list opens searching for it. */
  search?: string;
}

/** Each record read: the key its result holds the record under, and its id argument. */
const RECORD_READS: Record<string, { key: string; arg: string }> = {
  get_contact: { key: "contact", arg: "contact_id" },
  get_organization: { key: "organization", arg: "organization_id" },
  get_opportunity: { key: "opportunity", arg: "opportunity_id" },
};

/** The list reads whose `query` the view's list searches for. */
const LIST_READS = new Set(["list_contacts", "list_organizations", "list_opportunities"]);

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

/** The id of the record a `get_*` result carries under `key`, or none for an error. */
function fromResult(result: unknown, key: string): string | undefined {
  const body = record(result);
  if (!body || body.error !== undefined) return undefined;
  return text(record(body[key])?.id);
}

/**
 * Where to open the view, from the tool the host rendered it for. `null` opens the list as
 * usual: no tool, a tool that names no record or query, or one this view is not bound to.
 * A tool name the host leaves out falls back to the shape of the result.
 */
export function launchTarget(
  toolName: string | undefined,
  input: Record<string, unknown> | null,
  structuredResult: unknown,
): LaunchTarget | null {
  if (toolName === undefined) {
    for (const { key } of Object.values(RECORD_READS)) {
      const recordId = fromResult(structuredResult, key);
      if (recordId) return { recordId };
    }
    return null;
  }
  const read = RECORD_READS[toolName];
  if (read) {
    // Until the result arrives, or when it is an error, the input names the record, and its
    // page reads it (and reports a record that is not found) itself.
    const recordId = fromResult(structuredResult, read.key) ?? text(input?.[read.arg]);
    return recordId ? { recordId } : null;
  }
  if (LIST_READS.has(toolName)) {
    const search = text(input?.query);
    return search ? { search } : null;
  }
  return null;
}
