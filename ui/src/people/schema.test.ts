import { describe, expect, it } from "vitest";
import type { DescribeFieldsResult } from "../types";
import { buildSchema, ENTITIES, type Entity, MAX_REQUEST_COLUMNS } from "./schema";

const described: DescribeFieldsResult = {
  fields: {
    contact: {
      core: [
        { key: "name", type: "text", description: "" },
        { key: "email", type: "text", description: "" },
        { key: "email_status", type: "select", description: "" },
        { key: "title", type: "text", description: "" },
        { key: "effective_status", type: "text", description: "" },
      ],
      custom: [
        { entity: "contact", key: "audience", label: "Audience", type: "select", options: ["Founders"], created_at: "", updated_at: "" },
      ],
      rollups: [{ path: "signals.active", type: "number", description: "" }],
    },
    organization: {
      core: [
        { key: "name", type: "text", description: "" },
        { key: "domain", type: "text", description: "" },
        { key: "industry", type: "text", description: "" },
      ],
      custom: [
        { entity: "organization", key: "fit", label: "Fit", type: "select", options: ["Strong"], created_at: "", updated_at: "" },
      ],
      rollups: [{ path: "contacts.count", type: "number", description: "" }],
    },
    opportunity: {
      core: [
        { key: "title", type: "text", description: "" },
        { key: "organization_id", type: "text", description: "" },
        { key: "stage", type: "select", description: "" },
        { key: "outcome", type: "select", description: "" },
        { key: "value", type: "number", description: "" },
        { key: "probability", type: "number", description: "" },
        { key: "weighted_value", type: "number", description: "" },
        { key: "close_date", type: "date", description: "" },
      ],
      custom: [
        { entity: "opportunity", key: "champion", label: "Champion", type: "select", options: ["Yes", "No"], created_at: "", updated_at: "" },
      ],
      rollups: [{ path: "interactions.last_at", type: "timestamp", description: "" }],
    },
  },
};

describe("buildSchema", () => {
  it("gives a person its own fields, its organization's under organization., and its rollups", () => {
    const paths = buildSchema(described, "contact").map((s) => s.path);
    expect(paths).toEqual([
      "name",
      "email",
      "email_status",
      "title",
      "audience",
      "organization.name",
      "organization.domain",
      "organization.industry",
      "organization.fit",
      "signals.active",
    ]);
  });

  it("gives an organization only its own paths, labelled from its side", () => {
    const schema = buildSchema(described, "organization");
    expect(schema.map((s) => s.path)).toEqual(["name", "domain", "industry", "fit", "contacts.count"]);
    expect(schema.find((s) => s.path === "name")?.label).toBe("Organization");
    expect(schema.find((s) => s.path === "industry")?.facet).toBe(true);
  });

  it("marks a workspace-defined field and keeps its options", () => {
    const fit = buildSchema(described, "contact").find((s) => s.path === "organization.fit");
    expect(fit).toMatchObject({ custom: true, label: "Fit", options: ["Strong"], group: "Company", facet: true });
    expect(buildSchema(described, "contact").find((s) => s.path === "email_status")?.options).toEqual([
      "valid",
      "risky",
      "unknown",
      "invalid",
    ]);
  });

  it("opens each list on the columns its entity config names, skipping any the workspace lacks", () => {
    const contact = ENTITIES.contact.defaultColumns(buildSchema(described, "contact"));
    expect(contact).toEqual(["name", "organization.name", "organization.fit", "audience", "signals.active", "email"]);
    const organization = ENTITIES.organization.defaultColumns(buildSchema(described, "organization"));
    expect(organization).toEqual(["name", "industry", "fit", "contacts.count"]);
  });

  it("opens on a marked field ahead of older selects, whatever its definition order", () => {
    const select = (key: string, default_column = false) => ({
      entity: "organization",
      key,
      label: key,
      type: "select",
      options: ["a"],
      created_at: "",
      updated_at: "",
      default_column,
    });
    const withCustom = (custom: ReturnType<typeof select>[]) => ({
      ...described,
      fields: { ...described.fields, organization: { ...described.fields.organization, custom } },
    });
    const unmarked = withCustom([select("size_segment"), select("mail_provider"), select("lead_score")]);
    expect(ENTITIES.organization.defaultColumns(buildSchema(unmarked, "organization"))).toEqual(
      ["name", "industry", "size_segment", "mail_provider", "contacts.count"],
    );
    const marked = withCustom([select("size_segment"), select("mail_provider"), select("lead_score", true)]);
    expect(ENTITIES.organization.defaultColumns(buildSchema(marked, "organization"))).toEqual(
      ["name", "industry", "lead_score", "size_segment", "contacts.count"],
    );
    const contact = ENTITIES.contact.defaultColumns(buildSchema(marked, "contact"));
    expect(contact.slice(0, 4)).toEqual(["name", "organization.name", "organization.lead_score", "organization.size_segment"]);
  });

  describe("with fields marked", () => {
    const field = (entity: string, key: string, type: string, default_column: boolean) => ({
      entity,
      key,
      label: key,
      type,
      options: type === "select" ? ["a"] : undefined,
      created_at: "",
      updated_at: "",
      default_column,
    });
    const withCustom = (entity: Entity, custom: ReturnType<typeof field>[]): DescribeFieldsResult => ({
      ...described,
      fields: { ...described.fields, [entity]: { ...described.fields[entity], custom } },
    });

    it("shows every marked field past the usual slots, and no unmarked select", () => {
      const custom = ["a", "b", "c"].map((key) => field("organization", key, "select", true));
      custom.push(field("organization", "older", "select", false));
      const schema = buildSchema(withCustom("organization", custom), "organization");
      expect(ENTITIES.organization.defaultColumns(schema)).toEqual(["name", "industry", "a", "b", "c", "contacts.count"]);
    });

    it("opens a marked field that is not a select", () => {
      const custom = [field("organization", "older", "select", false), field("organization", "score", "number", true)];
      const schema = buildSchema(withCustom("organization", custom), "organization");
      expect(ENTITIES.organization.defaultColumns(schema)).toEqual(["name", "industry", "score", "older", "contacts.count"]);
    });

    it("opens a deal only on its own marked fields, not its account's or contact's", () => {
      const mark = withCustom("organization", [field("organization", "fit", "select", true)]);
      const deal = {
        ...mark,
        fields: {
          ...mark.fields,
          contact: { ...mark.fields.contact, custom: [field("contact", "audience", "select", true)] },
          opportunity: {
            ...mark.fields.opportunity,
            custom: [field("opportunity", "champion", "select", false), field("opportunity", "risk", "number", true)],
          },
        },
      };
      const columns = ENTITIES.opportunity.defaultColumns(buildSchema(deal, "opportunity"));
      expect(columns).toContain("risk");
      expect(columns).not.toContain("champion");
      expect(columns).not.toContain("organization.fit");
      expect(columns).not.toContain("contact.audience");
    });

    it.each<Entity>(["contact", "organization", "opportunity"])(
      "keeps a fresh %s list within the server's limit, dropping the last marked first",
      (entity) => {
        const request = (count: number) => {
          const custom = Array.from({ length: count }, (_, i) => field(entity, `f${i}`, "text", true));
          const schema = buildSchema(withCustom(entity, custom), entity);
          const columns = ENTITIES[entity].defaultColumns(schema);
          return { columns, paths: new Set([...columns, ...ENTITIES[entity].alwaysFetched]) };
        };
        const over = request(60);
        expect(over.paths.size).toBe(MAX_REQUEST_COLUMNS);
        const kept = over.columns.filter((path) => /^f\d+$/.test(path));
        expect(kept).toEqual(kept.map((_, i) => `f${i}`));
        // The count that lands exactly on the limit keeps every field.
        expect(request(kept.length).columns).toEqual(over.columns);
        expect(request(kept.length + 1).columns).toEqual(over.columns);
      },
    );
  });

  it("gives a deal its own fields, its account's and its contact's own fields, and its rollups", () => {
    const schema = buildSchema(described, "opportunity");
    expect(schema.map((s) => s.path)).toEqual([
      "title",
      "stage",
      "outcome",
      "value",
      "probability",
      "weighted_value",
      "close_date",
      "champion",
      "organization.name",
      "organization.domain",
      "organization.industry",
      "organization.fit",
      "contact.name",
      "contact.email",
      "contact.email_status",
      "contact.title",
      "contact.audience",
      "interactions.last_at",
    ]);
    expect(schema.find((s) => s.path === "champion")).toMatchObject({ custom: true, group: "Deal" });
    expect(schema.find((s) => s.path === "outcome")?.options).toEqual(["open", "won", "lost"]);
    expect(schema.find((s) => s.path === "contact.audience")?.group).toBe("Person");
    expect(ENTITIES.opportunity.defaultColumns(schema)).toEqual([
      "title",
      "stage",
      "value",
      "probability",
      "weighted_value",
      "champion",
      "close_date",
      "interactions.last_at",
    ]);
  });
});
