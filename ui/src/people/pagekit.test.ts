import { act, createElement, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { FieldDef } from "../types";
import { buildTimeline as dealTimeline, type Loaded as DealLoaded } from "./OpportunityPage";
import { CustomFields, foldChanges, HISTORY, RecordTimeline, type TimelineEvent } from "./pagekit";
import { buildTimeline as personTimeline, type Loaded as PersonLoaded } from "./PersonPage";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const change = (key: string, by: string, at: string): TimelineEvent => ({
  key,
  lane: HISTORY,
  at,
  glyph: "✎",
  title: `${key} set`,
  change: { by },
});

describe("foldChanges", () => {
  it("folds one writer's changes on one day into one entry", () => {
    const folded = foldChanges([
      change("a", "source run", "2026-10-29T10:00:00Z"),
      change("b", "source run", "2026-10-29T10:00:01Z"),
      change("c", "source run", "2026-10-29T11:00:00Z"),
    ]);
    expect(folded).toHaveLength(1);
    expect(folded[0].title).toBe("source run updated 3 fields");
    expect(folded[0].at).toBe("2026-10-29T11:00:00Z");
  });

  it("keeps writers, days, and activity apart, newest first", () => {
    const added: TimelineEvent = { key: "added", lane: HISTORY, at: "2026-10-27T09:00:00Z", glyph: "+", title: "Added" };
    const folded = foldChanges([
      change("a", "source run", "2026-10-29T10:00:00Z"),
      change("b", "source run", "2026-10-29T10:00:01Z"),
      change("c", "mx check", "2026-10-29T12:00:00Z"),
      change("d", "source run", "2026-10-28T10:00:00Z"),
      added,
    ]);
    expect(folded.map((e) => e.title)).toEqual([
      "c set",
      "source run updated 2 fields",
      "d set",
      "Added",
    ]);
  });
});

describe("a page's field changes", () => {
  // One writer, one moment, a different reason for each field: the reason belongs to
  // the field, so the changes still fold into one entry under that writer.
  const at = "2026-10-29T10:00:00Z";
  const fields = { industry: "Software", size: 40 };
  const field_details = {
    industry: { by: "rubric-v2", at, why: "Sells procurement software." },
    size: { by: "rubric-v2", at, why: "Forty staff on the team page." },
  };
  const defs: FieldDef[] = [
    { key: "industry", label: "Industry" },
    { key: "size", label: "Size" },
  ] as FieldDef[];

  it("fold by writer on the person page, whatever each field's reason", () => {
    const loaded = {
      contact: { id: "c1", name: "Ada", fields, field_details, created_at: "2026-10-01T00:00:00Z" },
      interactions: [],
      organization: null,
      signals: [],
      defs: { contact: defs },
    } as unknown as PersonLoaded;
    const changes = foldChanges(personTimeline(loaded)).filter((e) => e.lane === HISTORY && e.key !== "created");
    expect(changes.map((e) => e.title)).toEqual(["rubric-v2 updated 2 fields"]);
  });

  it("fold by writer on the deal page, whatever each field's reason", () => {
    const loaded = {
      opportunity: { id: "o1", fields, field_details, created_at: "2026-10-01T00:00:00Z" },
      interactions: [],
      organization: null,
      contact: null,
      stages: [],
      defs,
      organizationDefs: [],
    } as unknown as DealLoaded;
    const changes = foldChanges(dealTimeline(loaded)).filter((e) => e.lane === HISTORY && e.key !== "added");
    expect(changes.map((e) => e.title)).toEqual(["rubric-v2 updated 2 fields"]);
  });
});

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const mount = (node: ReactNode) => act(() => root.render(node));
const button = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith(text));
const click = (text: string) => act(() => button(text)?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const labels = () => [...container.querySelectorAll("summary, span")].map((e) => e.textContent ?? "");

describe("CustomFields", () => {
  const defs = ["a", "b", "c", "d", "e", "f", "g", "h"].map((key) => ({ key, label: `Field ${key}` })) as FieldDef[];
  const values = { a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7 };
  const shown = (key: string) => labels().some((t) => t.startsWith(`Field ${key}`));

  it("shows five set fields, the rest and the empty ones a click away", async () => {
    await mount(createElement(CustomFields, { defs, values, details: undefined }));
    expect(["a", "b", "c", "d", "e"].every(shown)).toBe(true);
    expect(shown("f") || shown("h")).toBe(false);

    await click("Show 2 more");
    expect(shown("g")).toBe(true);
    await click("Show fewer");
    expect(shown("f")).toBe(false);

    await click("1 empty field");
    expect(shown("h")).toBe(true);
    await click("Hide empty fields");
    expect(shown("h")).toBe(false);
  });
});

describe("RecordTimeline", () => {
  const change = (key: string, at: string): TimelineEvent => ({ key, lane: HISTORY, at, glyph: "✎", title: `${key} set`, change: { by: "rubric-v2" } });
  const events: TimelineEvent[] = [
    { key: "e1", lane: "Emails", at: "2026-10-29T12:00:00Z", glyph: "↗", title: "Email sent" },
    change("a", "2026-10-29T10:00:00Z"),
    change("b", "2026-10-29T10:00:01Z"),
  ];
  const titles = () => [...container.querySelectorAll("[style*='grid'] > span:last-child > span:first-child")].map((e) => e.textContent);

  it("opens on Activity with changes folded; a lane filters; History lists each change", async () => {
    await mount(createElement(RecordTimeline, { events, lanes: ["Emails"] }));
    expect(button("Activity")?.textContent).toBe("Activity 2");
    expect(titles()).toEqual(["Email sent", "rubric-v2 updated 2 fields"]);

    await click("Emails");
    expect(titles()).toEqual(["Email sent"]);

    await click("History");
    expect(titles()).toEqual(["a set", "b set"]);
  });
});
