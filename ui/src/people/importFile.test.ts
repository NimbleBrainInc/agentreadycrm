import { describe, expect, it } from "vitest";
import {
  batches,
  cellText,
  fieldValue,
  guessMapping,
  importTargets,
  keeps,
  outcomes,
  outcomesCsv,
  parseCsv,
  plan,
  toImportRow,
  toTable,
} from "./importFile";
import type { FieldSpec } from "./schema";

const custom = (path: string, label: string, kind: FieldSpec["kind"] = "text"): FieldSpec => ({
  path,
  label,
  kind,
  group: "Company",
  custom: true,
  facet: false,
});

const schema: FieldSpec[] = [
  { path: "name", label: "Name", kind: "text", group: "Company", custom: false, facet: false },
  custom("vendor_id", "Vendor id"),
  custom("is_customer", "Customer", "boolean"),
  custom("tags", "Tags", "list"),
  // A related record's field, which an import of this entity cannot write.
  custom("organization.fit", "Fit"),
];

describe("parseCsv", () => {
  it("reads quoted commas, doubled quotes and line breaks, CRLF and a BOM", () => {
    const text = '﻿Name,Note\r\n"Acme, Inc","said ""hi""\nthen left"\r\nBeta,plain\n';
    expect(parseCsv(text)).toEqual([
      ["Name", "Note"],
      ["Acme, Inc", 'said "hi"\nthen left'],
      ["Beta", "plain"],
    ]);
  });

  it("skips blank lines and keeps a last row with no line break", () => {
    expect(parseCsv("a,b\n\n1,2\n,\n3,4")).toEqual([
      ["a", "b"],
      ["1", "2"],
      ["3", "4"],
    ]);
  });
});

describe("toTable", () => {
  it("names blank headers, pads short rows, trims cells, and writes dates and numbers as text", () => {
    const table = toTable([
      ["Name", "", "Founded"],
      [" Acme ", 42, new Date("2019-03-04T00:00:00Z")],
      ["Beta"],
    ]);
    expect(table.headers).toEqual(["Name", "Column 2", "Founded"]);
    expect(table.rows).toEqual([
      ["Acme", "42", "2019-03-04"],
      ["Beta", "", ""],
    ]);
    expect(cellText(null)).toBe("");
  });

  it("takes the first non-empty row as the headers", () => {
    const table = toTable([[], ["", null], ["Name", "Domain"], ["Acme", "acme.example"]]);
    expect(table.headers).toEqual(["Name", "Domain"]);
    expect(table.rows).toEqual([["Acme", "acme.example"]]);
  });

  it("reads back the values of an outcomes file as they were, without the formula guard", () => {
    const table = toTable([
      ["Name", "Phone", "Handle", "Note"],
      ["Acme", "+1 555 0100", "@acme", "'quoted'"],
    ]);
    const back = toTable(parseCsv(outcomesCsv(table, [{ line: 0, reason: "fix me" }])));
    expect(back.headers).toEqual(["Reason", ...table.headers]);
    expect(back.rows).toEqual([["fix me", ...table.rows[0]]]);
  });
});

describe("guessMapping", () => {
  it("maps by key, label or common spelling, and gives each target one column", () => {
    const targets = importTargets("organization", schema);
    const mapping = guessMapping(["Company", "Website Domain", "Vendor ID", "Domain", "Notes"], targets, "organization");
    expect(mapping).toEqual(["name", null, "fields.vendor_id", "domain", null]);
  });

  it("reads Company as the person's company in a list of people", () => {
    const targets = importTargets("contact", []);
    expect(guessMapping(["Full Name", "Work Email", "Company", "Website"], targets, "contact")).toEqual([
      "name",
      "email",
      "organization_name",
      "organization_domain",
    ]);
  });

  it("offers only the entity's own custom fields", () => {
    const ids = importTargets("organization", schema).map((t) => (t.kind === "core" ? t.key : t.key));
    expect(ids).toContain("vendor_id");
    expect(ids).not.toContain("organization.fit");
  });
});

describe("rows", () => {
  const targets = importTargets("organization", schema);
  const table = toTable([
    ["Name", "Domain", "Vendor", "Customer", "Tags", "Region"],
    ["Acme", "acme.example", "v-1", "Yes", "saas; b2b", "EMEA"],
    ["", "", "v-2", "", "", "US"],
    ["Beta", "", "", "maybe", "", "US"],
  ]);
  const mapping = ["name", "domain", "fields.vendor_id", "fields.is_customer", "fields.tags", "region"];

  it("puts core columns on the row and custom fields under fields, leaving blank cells out", () => {
    expect(toImportRow(table.rows[0], mapping, targets)).toEqual({
      name: "Acme",
      domain: "acme.example",
      region: "EMEA",
      fields: { vendor_id: "v-1", is_customer: true, tags: ["saas", "b2b"] },
    });
    expect(toImportRow(table.rows[2], mapping, targets)).toEqual({
      name: "Beta",
      region: "US",
      fields: { is_customer: "maybe" },
    });
  });

  it("coerces a boolean it recognises and leaves one it does not for the server to name", () => {
    expect(fieldValue(custom("x", "X", "boolean"), "N")).toBe(false);
    expect(fieldValue(custom("x", "X", "boolean"), "maybe")).toBe("maybe");
  });

  it("plans the rows to send, keeping back filtered rows and rows with neither required column", () => {
    const filters = [{ column: 5, op: "is" as const, value: "us" }];
    const result = plan(table, mapping, targets, filters, "organization");
    expect(result.filteredOut).toBe(1);
    expect(result.missingRequired).toEqual([1]);
    expect(result.rows.map((r) => r.line)).toEqual([2]);
  });

  it("filters without case, on emptiness, and by containment", () => {
    expect(keeps(["Acme"], [{ column: 0, op: "contains", value: "CM" }])).toBe(true);
    expect(keeps([""], [{ column: 0, op: "is not empty", value: "" }])).toBe(false);
    expect(keeps([""], [{ column: 0, op: "is empty", value: "" }])).toBe(true);
  });
});

describe("batches and outcomes", () => {
  it("cuts a list into batches of the size given", () => {
    expect(batches([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
  });

  it("names a skipped or kept row by its file row, and writes them back with the reason first", () => {
    const sent = [{ line: 4 }, { line: 9 }, { line: 12 }];
    const found = outcomes(
      {
        received: 3,
        created: 1,
        matched: 1,
        promoted: 0,
        skipped: [{ reason: "field 'tags' takes text", rows: [2] }],
        kept: [{ reason: "matched an organization that keeps its own vendor_id", rows: [0] }],
      },
      sent,
    );
    expect(found.skipped).toEqual([{ line: 12, reason: "field 'tags' takes text" }]);
    expect(found.kept).toEqual([{ line: 4, reason: "matched an organization that keeps its own vendor_id" }]);

    const table = { headers: ["Name", "Note"], rows: Array.from({ length: 13 }, (_, i) => [`Org ${i}`, i === 12 ? "=bad" : ""]) };
    expect(outcomesCsv(table, found.skipped)).toBe(
      "﻿Reason,Name,Note\r\nfield 'tags' takes text,Org 12,'=bad\r\n",
    );
  });
});

describe("validRefPrefix", () => {
  it("accepts a prefix that makes a reference the server takes, and refuses one it would not", async () => {
    const { validRefPrefix } = await import("./importFile");
    for (const ok of ["vendor://companies/", "acme-data:company:", "x+y.z:"]) expect(validRefPrefix(ok)).toBe(true);
    for (const bad of ["", "Vendor://x/", "vendor companies/", "people://organizations/", "://x/"]) {
      expect(validRefPrefix(bad)).toBe(false);
    }
  });
});
