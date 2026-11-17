import { describe, expect, it } from "vitest";
import { describeEntry, isActive, toFilter } from "./filters";
import type { FieldSpec } from "./schema";

const spec = (path: string, kind: FieldSpec["kind"], extra: Partial<FieldSpec> = {}): FieldSpec => ({
  path,
  label: path,
  kind,
  group: "Person",
  custom: false,
  facet: false,
  ...extra,
});

const schema = [
  spec("fit", "select", { options: ["Strong", "Moderate"] }),
  spec("tags", "list"),
  spec("vip", "boolean"),
  spec("headcount", "number"),
  spec("created_at", "timestamp"),
  spec("title", "text"),
  spec("interactions.last_at", "timestamp"),
];
const SEARCH = ["name", "email"];

describe("toFilter", () => {
  it("is undefined with nothing applied", () => {
    expect(toFilter({}, schema, "", SEARCH)).toBeUndefined();
    expect(toFilter({ title: { t: "contains", text: "  " } }, schema, " ", SEARCH)).toBeUndefined();
  });

  it("compiles each entry kind to the clause its field takes", () => {
    const filter = toFilter(
      {
        fit: { t: "values", values: ["Strong"] },
        tags: { t: "values", values: ["warm", "event"] },
        vip: { t: "values", values: [true] },
        headcount: { t: "range", min: 10, max: 50 },
        created_at: { t: "dates", to: "2026-10-01" },
        title: { t: "contains", text: " CEO " },
      },
      schema,
      "",
      SEARCH,
    );
    expect(filter).toEqual({
      and: [
        { field: "fit", op: "in", value: ["Strong"] },
        { field: "tags", op: "has", value: ["warm", "event"] },
        { field: "vip", op: "eq", value: true },
        { field: "headcount", op: "between", value: [10, 50] },
        { field: "created_at", op: "lte", value: "2026-10-01T23:59:59.999Z" },
        { field: "title", op: "contains", value: "CEO" },
      ],
    });
  });

  it("turns the search box into one contains clause per search path", () => {
    expect(toFilter({}, schema, "dana", SEARCH)).toEqual({
      or: [
        { field: "name", op: "contains", value: "dana" },
        { field: "email", op: "contains", value: "dana" },
      ],
    });
  });

  it("leaves one field out so a facet counts its own options under the others", () => {
    const state = { fit: { t: "values" as const, values: ["Strong"] }, vip: { t: "values" as const, values: [false] } };
    expect(toFilter(state, schema, "", SEARCH, "fit")).toEqual({ field: "vip", op: "eq", value: false });
  });

  it("builds the same clause for a day preset on every call", () => {
    const state = { "interactions.last_at": { t: "preset" as const, id: "7d" } };
    const first = toFilter(state, schema, "", SEARCH);
    const second = toFilter(state, schema, "", SEARCH);
    expect(first).toEqual(second);
    expect(first).toMatchObject({ field: "interactions.last_at", op: "gte" });
    expect((first as { value: string }).value).toMatch(/T00:00:00\.000Z$/);
  });
});

describe("describeEntry and isActive", () => {
  it("names the value side of a chip", () => {
    expect(describeEntry(schema[0], { t: "values", values: ["Strong", "Moderate"] })).toBe("Strong, Moderate");
    expect(describeEntry(schema[2], { t: "values", values: [false] })).toBe("No");
    expect(describeEntry(schema[3], { t: "range", min: 10 })).toBe("from 10");
    expect(describeEntry(schema[5], { t: "contains", text: "CEO" })).toBe("contains “CEO”");
    expect(describeEntry(schema[6], { t: "preset", id: "never" })).toBe("Never");
  });

  it("treats an entry that narrows nothing as inactive", () => {
    expect(isActive(schema[0], { t: "values", values: [] })).toBe(false);
    expect(isActive(schema[3], { t: "range" })).toBe(false);
    expect(isActive(schema[5], { t: "contains", text: "x" })).toBe(true);
    expect(isActive(schema[5], undefined)).toBe(false);
  });
});
