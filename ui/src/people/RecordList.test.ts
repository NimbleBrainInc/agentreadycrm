import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Clause } from "./filters";

// The subject is the launch search: a list rendered for a tool call that searched opens with
// that query in the search box and in the filter it lists with, the board's included.
const { call, boardFilters } = vi.hoisted(() => ({ call: vi.fn(), boardFilters: [] as (Clause | undefined)[] }));

vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), useCall: () => call }));
vi.mock("@nimblebrain/synapse/react", () => ({ useApp: () => ({ hostCapabilities: {} }), useDataSync: () => {} }));
vi.mock("@nimblebrain/synapse", () => ({ downloadFile: vi.fn() }));
vi.mock("./Board", () => ({
  Board: ({ filter }: { filter?: Clause }) => {
    boardFilters.push(filter);
    return null;
  },
}));

import { RecordList } from "./RecordList";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const core = (keys: string[]) => keys.map((key) => ({ key, type: "text", description: "" }));
const FIELDS = {
  fields: {
    contact: { core: core(["name", "email", "title"]), custom: [], rollups: [] },
    organization: { core: core(["name", "domain"]), custom: [], rollups: [] },
    opportunity: { core: core(["title", "stage", "outcome"]), custom: [], rollups: [] },
  },
};

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  call.mockReset();
  boardFilters.length = 0;
});

const respond = (tool: string) => {
  if (tool === "list_fields") return Promise.resolve(FIELDS);
  if (tool === "get_pipeline") return Promise.resolve({ stages: [] });
  return Promise.resolve({ contacts: [], organizations: [], opportunities: [], total: 0, next_cursor: null });
};

const render = async (entity: "contact" | "opportunity", search?: string) => {
  call.mockImplementation(respond);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () =>
    root.render(createElement(RecordList, { entity, search, onOpen: () => {}, onState: () => {}, onNew: () => {} })),
  );
};

// The search is a contains-clause over the entity's search paths, somewhere in the filter.
const searchesFor = (filter: Clause | undefined, q: string) =>
  JSON.stringify(filter ?? null).includes(`"op":"contains","value":${JSON.stringify(q)}`);

describe("a list opened for a search", () => {
  it("puts the query in the search box and the list call's filter", async () => {
    await render("contact", "acme");

    const box = container.querySelector<HTMLInputElement>("input[type=search], input");
    expect(box?.value).toBe("acme");
    const listed = call.mock.calls.filter(([tool]) => tool === "list_contacts");
    expect(listed.length).toBeGreaterThan(0);
    expect(listed.every(([, args]) => searchesFor(args.filter, "acme"))).toBe(true);
  });

  it("filters the deal board by the query", async () => {
    await render("opportunity", "acme");

    expect(boardFilters.length).toBeGreaterThan(0);
    expect(searchesFor(boardFilters.at(-1), "acme")).toBe(true);
  });

  it("lists unfiltered without one", async () => {
    await render("contact");

    const listed = call.mock.calls.filter(([tool]) => tool === "list_contacts");
    expect(listed.length).toBeGreaterThan(0);
    expect(listed.every(([, args]) => args.filter === undefined)).toBe(true);
  });
});
