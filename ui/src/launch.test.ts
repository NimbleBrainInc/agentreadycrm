import { describe, expect, it } from "vitest";
import { launchTarget } from "./launch";

const CONTACT = { contact: { id: "ct_01A", name: "Dana Reyes" }, counts: {}, recent_interactions: [] };
const ORGANIZATION = { organization: { id: "org_01A", name: "Larkfield" }, counts: {} };
const OPPORTUNITY = { opportunity: { id: "op_01A", title: "Larkfield pilot" }, counts: {} };
const FLAGGED = { error: { code: "not_found", message: "m", retryable: false }, next_step: "x" };

describe("launchTarget", () => {
  it.each([
    ["get_contact", { contact_id: "ct_01A" }, CONTACT, "ct_01A"],
    ["get_organization", { organization_id: "org_01A" }, ORGANIZATION, "org_01A"],
    ["get_opportunity", { opportunity_id: "op_01A" }, OPPORTUNITY, "op_01A"],
  ] as const)("opens the record a %s result carries", (tool, input, result, id) => {
    expect(launchTarget(tool, input, result)).toEqual({ recordId: id });
  });

  it("opens the record the input names until the result arrives, or when it is an error", () => {
    expect(launchTarget("get_contact", { contact_id: " ct_01A " }, null)).toEqual({ recordId: "ct_01A" });
    expect(launchTarget("get_organization", { organization_id: "org_01X" }, FLAGGED)).toEqual({
      recordId: "org_01X",
    });
    expect(launchTarget("get_opportunity", {}, null)).toBeNull();
  });

  it("opens a list searched for a list call's query", () => {
    expect(launchTarget("list_contacts", { query: " dana " }, { contacts: [] })).toEqual({ search: "dana" });
    expect(launchTarget("list_organizations", { query: "lark" }, null)).toEqual({ search: "lark" });
  });

  it("opens the plain list for a list call with no query, the pipeline, or no tool", () => {
    expect(launchTarget("list_contacts", { organization_id: "org_01A" }, { contacts: [] })).toBeNull();
    expect(launchTarget("get_pipeline", {}, { stages: [] })).toBeNull();
    // A deal lookup opens no view; were a host to render one for it, the board opens as is.
    expect(launchTarget("list_opportunities", { query: "pilot" }, null)).toBeNull();
    expect(launchTarget(undefined, null, null)).toBeNull();
    expect(launchTarget("get_contact", null, OPPORTUNITY)).toBeNull();
  });

  it("falls back to the result's shape when the host names no tool", () => {
    expect(launchTarget(undefined, null, ORGANIZATION)).toEqual({ recordId: "org_01A" });
    expect(launchTarget(undefined, null, { contacts: [{ id: "ct_01A" }] })).toBeNull();
    expect(launchTarget(undefined, null, FLAGGED)).toBeNull();
  });
});
