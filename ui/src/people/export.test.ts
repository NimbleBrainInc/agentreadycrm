import { describe, expect, it } from "vitest";
import { exportColumns, toCsv } from "./export";
import type { FieldSpec } from "./schema";

const spec = (path: string, label = path, kind: FieldSpec["kind"] = "text"): FieldSpec => ({
  path,
  label,
  kind,
  group: "Person",
  custom: false,
  facet: false,
});

describe("toCsv", () => {
  const columns = [spec("name", "Person"), spec("note", "Note"), spec("n", "Count", "number")];

  it("writes the labels, then one row per record, CRLF-terminated with a BOM", () => {
    const csv = toCsv(columns, [{ id: "ct_1", name: "Dana", note: "fine", n: 3 }]);
    expect(csv).toBe("﻿Person,Note,Count\r\nDana,fine,3\r\n");
  });

  it("quotes a cell holding a comma, a quote or a line break", () => {
    const csv = toCsv(columns, [{ id: "ct_1", name: 'Dana "D" Hale, Jr', note: "two\nlines", n: null }]);
    expect(csv.split("\r\n")[1]).toBe('"Dana ""D"" Hale, Jr","two\nlines",');
  });

  it("prefixes a value a spreadsheet would run as a formula, and leaves a negative number alone", () => {
    const rows = [
      { id: "a", name: "=SUM(A1)", note: "+1 555", n: -5 },
      { id: "b", name: "-x", note: "@mention", n: 0 },
    ];
    const lines = toCsv(columns, rows).split("\r\n");
    expect(lines[1]).toBe("'=SUM(A1),'+1 555,-5");
    expect(lines[2]).toBe("'-x,'@mention,0");
  });

  it("joins a list with semicolons and guards the joined text", () => {
    const csv = toCsv([spec("tags", "Tags", "list")], [{ id: "a", tags: ["-x", "y"] }]);
    expect(csv.split("\r\n")[1]).toBe("'-x; y");
  });
});

describe("exportColumns", () => {
  const schema = [spec("name"), spec("title"), spec("email"), spec("email_status")];
  const companions = { name: "title", email: "email_status" };

  it("follows each visible column with the value its cell draws beside it", () => {
    const out = exportColumns([schema[0], schema[2]], schema, companions);
    expect(out.map((s) => s.path)).toEqual(["name", "title", "email", "email_status"]);
  });

  it("does not repeat a companion that is already visible", () => {
    const out = exportColumns([schema[1], schema[0]], schema, companions);
    expect(out.map((s) => s.path)).toEqual(["title", "name"]);
  });
});
