import { act, createElement, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The subject is `App`'s own routing: which page a record of each kind opens, what trail the
// app reports for it, and where it goes when the host hands it an address. The pages it
// composes each read their record through tools, and what they draw is theirs, so they stand
// in here as their props: an id, a label they report once "loaded", and their links.
// `vi.hoisted` because `vi.mock` is hoisted above the imports.
const { host } = vi.hoisted(() => ({
  host: {
    showsTrail: false,
    trail: [] as { id: string; label: string }[],
    navigate: null as ((id: string) => void) | null,
    sync: null as (() => void) | null,
    mounts: 0,
    // The tool call an MCP Apps host rendered the view for, as the bridge delivers it.
    toolName: undefined as string | undefined,
    toolInput: null as Record<string, unknown> | null,
    toolResult: null as { structuredContent?: unknown } | null,
  },
}));

vi.mock("@nimblebrain/synapse/react", () => ({
  useApp: () => ({ toolInfo: host.toolName ? { tool: { name: host.toolName } } : undefined }),
  useToolInput: () => host.toolInput,
  useToolResult: () => host.toolResult,
  useDataSync: (onSync: () => void) => {
    host.sync = onSync;
  },
  useModelContext: () => {},
  useTrail: (trail: { id: string; label: string }[], navigate: (id: string) => void) => {
    host.trail = trail;
    host.navigate = navigate;
    return host.showsTrail;
  },
}));

const NAMES: Record<string, string> = {
  ct_01A: "Dana Reyes",
  ct_01B: "Miles Okafor",
  org_01A: "Larkfield",
  op_01A: "Larkfield · pilot",
};

/**
 * A page standing in for a real one: names its record, and reports its label the way the real
 * pages do, from an effect on `onLoaded`, so it reports once per mount and not on every render.
 * A record with no name here is one whose read has not come back yet, so it reports none.
 */
function Page({
  kind,
  id,
  onLoaded,
  refreshKey,
}: {
  kind: string;
  id: string;
  onLoaded: (label: string) => void;
  refreshKey?: number;
}) {
  useEffect(() => {
    const name = NAMES[id];
    if (name) onLoaded(name);
  }, [onLoaded]);
  useEffect(() => {
    host.mounts += 1;
  }, []);
  return createElement("section", { "data-page": kind, "data-id": id, "data-refresh": refreshKey });
}

vi.mock("./people/RecordList", () => ({
  RecordList: ({ onOpen, search }: { onOpen: (id: string) => void; search?: string }) =>
    createElement(
      "ul",
      { "data-list": "records", "data-search": search ?? "" },
      createElement("li", null, createElement("button", { onClick: () => onOpen("ct_01A") }, "Dana")),
    ),
}));
vi.mock("./people/PersonPage", () => ({
  PersonPage: (p: { contactId: string; onLoaded: (l: string) => void; refreshKey?: number }) =>
    createElement(Page, { kind: "person", id: p.contactId, onLoaded: p.onLoaded, refreshKey: p.refreshKey }),
}));
vi.mock("./components/OrganizationDetail", () => ({
  OrganizationDetail: (p: { organizationId: string; onLoaded: (l: string) => void; refreshKey?: number }) =>
    createElement(Page, { kind: "organization", id: p.organizationId, onLoaded: p.onLoaded, refreshKey: p.refreshKey }),
}));
vi.mock("./people/OpportunityPage", () => ({
  OpportunityPage: (p: { opportunityId: string; onLoaded: (l: string) => void; refreshKey?: number }) =>
    createElement(Page, { kind: "opportunity", id: p.opportunityId, onLoaded: p.onLoaded, refreshKey: p.refreshKey }),
}));
vi.mock("./components/forms", () => ({
  NewContactForm: () => null,
  NewOrganizationForm: () => null,
  NewOpportunityForm: () => null,
}));

import App, { type View } from "./App";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

async function mount(view: View): Promise<void> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(createElement(App, { view })));
}

const shown = () => {
  const el = container.querySelector("section[data-page]");
  return el ? `${el.getAttribute("data-page")}:${el.getAttribute("data-id")}` : null;
};
const navigate = async (id: string) => {
  await act(async () => host.navigate?.(id));
};

beforeEach(() => {
  host.showsTrail = false;
  host.mounts = 0;
  host.toolName = undefined;
  host.toolInput = null;
  host.toolResult = null;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("the trail the app reports", () => {
  it("is the view's list, then the open record's address and name", async () => {
    await mount("contact");
    expect(host.trail).toEqual([{ id: "ui://people/contacts", label: "Contacts" }]);

    const open = container.querySelector("button");
    await act(async () => open?.dispatchEvent(new MouseEvent("click", { bubbles: true })));

    expect(shown()).toBe("person:ct_01A");
    // The address is the record's own `uri`, the value the agent reads off a tool result.
    expect(host.trail).toEqual([
      { id: "ui://people/contacts", label: "Contacts" },
      { id: "people://contacts/ct_01A", label: "Dana Reyes" },
    ]);
  });
});

describe("an address the host hands the app", () => {
  it("opens a company from the contacts view, in place", async () => {
    await mount("contact");
    await navigate("people://organizations/org_01A");

    expect(shown()).toBe("organization:org_01A");
    expect(host.trail.at(-1)).toEqual({
      id: "people://organizations/org_01A",
      label: "Larkfield",
    });
  });

  it("opens a person from the organizations view, in place", async () => {
    await mount("organization");
    await navigate("people://contacts/ct_01B");

    expect(shown()).toBe("person:ct_01B");
    expect(host.trail).toEqual([
      { id: "ui://people/organizations", label: "Organizations" },
      { id: "people://contacts/ct_01B", label: "Miles Okafor" },
    ]);
  });

  it("moves from one open record to another, relabelling the step", async () => {
    await mount("contact");
    await navigate("people://contacts/ct_01A");
    await navigate("people://contacts/ct_01B");

    expect(shown()).toBe("person:ct_01B");
    expect(host.trail.at(-1)?.label).toBe("Miles Okafor");
  });

  it("keeps the open record's name when handed its own address", async () => {
    await mount("contact");
    await navigate("people://contacts/ct_01A");
    await navigate("people://contacts/ct_01A");

    expect(shown()).toBe("person:ct_01A");
    expect(host.trail.at(-1)?.label).toBe("Dana Reyes");
  });

  it("never labels a record with the previous one's name while it loads", async () => {
    await mount("contact");
    await navigate("people://contacts/ct_01A");
    await navigate("people://contacts/ct_01SLOW");

    expect(shown()).toBe("person:ct_01SLOW");
    // Unnamed until its read lands (the host shows a placeholder), never "Dana Reyes".
    expect(host.trail.at(-1)).toEqual({ id: "people://contacts/ct_01SLOW", label: "" });
  });

  it("goes back to the list from an open record", async () => {
    await mount("contact");
    await navigate("people://contacts/ct_01A");
    await navigate("ui://people/contacts");

    expect(shown()).toBeNull();
    expect(host.trail).toEqual([{ id: "ui://people/contacts", label: "Contacts" }]);
  });

  it("opens a deal from the opportunities view, in place", async () => {
    await mount("opportunity");
    await navigate("people://opportunities/op_01A");

    expect(shown()).toBe("opportunity:op_01A");
    expect(host.trail).toEqual([
      { id: "ui://people/opportunities", label: "Opportunities" },
      { id: "people://opportunities/op_01A", label: "Larkfield · pilot" },
    ]);
  });

  it("ignores an address that is not one of People's, leaving the open record open", async () => {
    await mount("contact");
    await navigate("people://contacts/ct_01A");
    await navigate("db-query://saved/sq_1");
    await navigate("people://contacts/not-an-id");

    expect(shown()).toBe("person:ct_01A");
    expect(host.trail.at(-1)?.id).toBe("people://contacts/ct_01A");
  });
});

describe("beside a host that shows the trail", () => {
  it("draws no back link over a company it opened", async () => {
    host.showsTrail = true;
    await mount("contact");
    await navigate("people://organizations/org_01A");
    expect(container.textContent).not.toContain("← Contacts");
  });

  it("keeps the back link where the host does not show the trail", async () => {
    await mount("contact");
    await navigate("people://organizations/org_01A");
    expect(container.textContent).toContain("← Contacts");
  });
});

describe("a data sync while a record is open", () => {
  // A sync runs on every write People announces and when the view comes back after a while
  // hidden. It re-reads the open record through `refreshKey`; a remount would close a form open
  // on the page and lose what the reader typed.
  it.each([
    ["contact", "people://contacts/ct_01A", "person:ct_01A"],
    ["contact", "people://organizations/org_01A", "organization:org_01A"],
    ["opportunity", "people://opportunities/op_01A", "opportunity:op_01A"],
  ] as const)("re-reads the %s view's open %s in place", async (view, address, page) => {
    await mount(view);
    await navigate(address);
    expect(shown()).toBe(page);
    const mounted = host.mounts;
    const refresh = () => container.querySelector("section[data-page]")?.getAttribute("data-refresh");
    const before = refresh();

    await act(async () => host.sync?.());

    expect(shown()).toBe(page);
    expect(refresh()).not.toBe(before);
    expect(host.mounts).toBe(mounted);
  });
});

describe("a view an MCP Apps host renders for a tool call", () => {
  const listShown = () => !container.querySelector<HTMLElement>("[data-list]")?.closest("[hidden]");
  const searched = () => container.querySelector("[data-list]")?.getAttribute("data-search");
  const rendered = async (view: View, toolName: string, input: Record<string, unknown>, result: unknown) => {
    host.toolName = toolName;
    host.toolInput = input;
    host.toolResult = result === null ? null : { structuredContent: result };
    await mount(view);
  };

  it.each([
    ["contact", "get_contact", { contact_id: "ct_01A" }, { contact: { id: "ct_01A" } }, "person:ct_01A", "people://contacts/ct_01A"],
    ["organization", "get_organization", { organization_id: "org_01A" }, { organization: { id: "org_01A" } }, "organization:org_01A", "people://organizations/org_01A"],
    ["opportunity", "get_opportunity", { opportunity_id: "op_01A" }, { opportunity: { id: "op_01A" } }, "opportunity:op_01A", "people://opportunities/op_01A"],
  ] as const)("the %s view opens on the record %s read", async (view, tool, input, result, page, address) => {
    await rendered(view, tool, input, result);
    expect(shown()).toBe(page);
    expect(listShown()).toBe(false);
    expect(host.trail.at(-1)?.id).toBe(address);
  });

  it("opens the record the input names before the result arrives", async () => {
    await rendered("contact", "get_contact", { contact_id: "ct_01B" }, null);
    expect(shown()).toBe("person:ct_01B");
  });

  it.each([
    ["contact", "list_contacts"],
    ["organization", "list_organizations"],
  ] as const)("the %s view opens its list searched for the %s query", async (view, tool) => {
    await rendered(view, tool, { query: "lark" }, { next_step: "x" });
    expect(shown()).toBeNull();
    expect(listShown()).toBe(true);
    expect(searched()).toBe("lark");
  });

  it("the opportunities view opens its list for the pipeline", async () => {
    await rendered("opportunity", "get_pipeline", {}, { stages: [] });
    expect(shown()).toBeNull();
    expect(listShown()).toBe(true);
    expect(searched()).toBe("");
  });

  it("opened from a placement, with no tool call, shows the list", async () => {
    await mount("organization");
    expect(shown()).toBeNull();
    expect(listShown()).toBe(true);
  });

  it("stays on the list after going back, though the host delivers the same result again", async () => {
    await rendered("contact", "get_contact", { contact_id: "ct_01A" }, { contact: { id: "ct_01A" } });
    await navigate("ui://people/contacts");
    expect(shown()).toBeNull();

    host.toolResult = { structuredContent: { contact: { id: "ct_01A" } } };
    await act(async () => root.render(createElement(App, { view: "contact" })));
    expect(shown()).toBeNull();
  });
});

describe("the scroll position of the view", () => {
  // jsdom lays nothing out, so the container's offset is a plain value the test sets.
  const scroller = () => {
    const el = container.querySelector<HTMLElement>("[data-scroller]")!;
    if (!Object.getOwnPropertyDescriptor(el, "scrollTop")) {
      let top = 0;
      Object.defineProperty(el, "scrollTop", { get: () => top, set: (v: number) => { top = v; }, configurable: true });
    }
    return el;
  };
  const scrollListTo = async (top: number) => {
    const el = scroller();
    el.scrollTop = top;
    await act(async () => el.dispatchEvent(new Event("scroll")));
  };

  it("opens a record at its top, though the list was read partway down", async () => {
    await mount("contact");
    await scrollListTo(480);
    const open = container.querySelector("button");
    await act(async () => open?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(shown()).toBe("person:ct_01A");
    expect(scroller().scrollTop).toBe(0);
  });

  it("puts the list back where the reader left it, however far they read the record", async () => {
    await mount("contact");
    await scrollListTo(480);
    await navigate("people://contacts/ct_01A");
    await scrollListTo(120);
    await navigate("ui://people/contacts");
    expect(shown()).toBeNull();
    expect(scroller().scrollTop).toBe(480);
  });
});
