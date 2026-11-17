import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The settings section reads the workspace's fields and retires or restores one through
// `update_field`. The tools are the app's `callTool`, answering from a small store kept
// here the way the server keeps it: a retired field is listed only under `include_retired`.
const { server, app, host } = vi.hoisted(() => {
  const now = "2026-10-20T00:00:00Z";
  const server = {
    fields: [] as Record<string, unknown>[],
    // The pipeline `get_pipeline` lists.
    stages: [] as Record<string, unknown>[],
    // Every `update_pipeline_stage` call's arguments, in order.
    stageCalls: [] as Record<string, unknown>[],
    // Set to have `update_field` refuse, as the server does a bad `replaced_by`.
    refuse: null as string | null,
    // Set to have `update_field` reject, as the bridge does when the transport fails.
    reject: null as string | null,
    // Set to have every call answer with a flagged error result carrying this message, as
    // the bridge hands over one under the service error contract: `data` is its text block.
    flagged: null as string | null,
  };
  // The tools the host lists for this viewer (`connector.allowedTools`).
  const host = { allowedTools: [] as string[] };
  const app = {
    callTool: async (name: string, args: Record<string, unknown>) => {
      if (server.flagged) {
        return { data: server.flagged, isError: true, content: [{ type: "text", text: server.flagged }] };
      }
      if (name === "get_pipeline") {
        const stages = server.stages.filter((s) => args.include_retired === true || !s.retired_at);
        return { data: { stages, open_count: 0, won_count: 0, lost_count: 0 } };
      }
      if (name === "update_pipeline_stage") {
        server.stageCalls.push(args);
        if (server.refuse) return { data: { error: "conflict", message: server.refuse } };
        const stage = server.stages.find((s) => s.key === args.key);
        if (!stage) return { data: { error: "not_found", message: `no stage ${String(args.key)}` } };
        if (args.retired === true) Object.assign(stage, { retired_at: now, replaced_by: args.replaced_by });
        if (args.retired === false) Object.assign(stage, { retired_at: undefined, replaced_by: undefined });
        return { data: { stage, action: args.retired ? "retired" : "restored" } };
      }
      if (name === "list_fields") {
        const custom = server.fields.filter((f) => args.include_retired === true || !f.retired_at);
        return {
          data: {
            fields: {
              organization: {
                core: [{ key: "name", type: "text", description: "" }],
                custom: custom.filter((f) => f.entity === "organization"),
                rollups: [],
              },
            },
          },
        };
      }
      if (name === "update_field") {
        if (server.reject) throw new Error(server.reject);
        if (server.refuse) return { data: { error: "invalid", message: server.refuse } };
        const field = server.fields.find((f) => f.entity === args.entity && f.key === args.key);
        if (!field) return { data: { error: "not_found", message: `no field ${String(args.key)}` } };
        if (args.retired === true) Object.assign(field, { retired_at: now, replaced_by: args.replaced_by ?? null });
        if (args.retired === false) Object.assign(field, { retired_at: null, replaced_by: null });
        if (typeof args.default_column === "boolean") field.default_column = args.default_column;
        if (typeof args.label === "string") field.label = args.label;
        if (typeof args.description === "string") field.description = args.description || undefined;
        if (args.clear_bounds === true) Object.assign(field, { minimum: null, maximum: null });
        if (typeof args.minimum === "number") field.minimum = args.minimum;
        if (typeof args.maximum === "number") field.maximum = args.maximum;
        if (Array.isArray(args.remove_options)) {
          const drop = new Set((args.remove_options as string[]).map((o) => o.toLowerCase()));
          field.options = (field.options as string[]).filter((o) => !drop.has(o.toLowerCase()));
        }
        return { data: { field, action: args.retired ? "retired" : "restored" } };
      }
      throw new Error(`unexpected tool ${name}`);
    },
  };
  return { server, app, host };
});

vi.mock("@nimblebrain/synapse/react", () => ({
  useApp: () => app,
  useDataSync: () => {},
  useToolAllowed: (tool: string) => host.allowedTools.includes(tool),
}));

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

/** The tools that change a workspace's vocabulary: a host may offer them to an admin only. */
const ADMIN_TOOLS: string[] = [
  "define_field",
  "define_pipeline_stage",
  "define_signal_type",
  "update_field",
  "update_pipeline_stage",
  "update_signal_type",
];

const { FieldsSettings } = await import("./FieldsView");
const { PipelineSettings } = await import("./PipelineSettings");

function field(key: string, label: string, extra: Record<string, unknown> = {}) {
  return { entity: "organization", key, label, type: "select", options: ["a", "b"], created_at: "", updated_at: "", ...extra };
}

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLElement | null = null;

beforeEach(() => {
  server.fields = [field("fit", "Fit"), field("fit_grade", "Fit grade")];
  server.refuse = null;
  server.reject = null;
  server.flagged = null;
  server.stages = [];
  server.stageCalls = [];
  host.allowedTools = ADMIN_TOOLS;
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
});

async function mount(component: () => unknown = FieldsSettings) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root?.render(createElement(component as () => null)));
  return container;
}

function buttons(el: HTMLElement, text: RegExp): HTMLElement[] {
  return [...el.querySelectorAll("button, a, [role=button]")].filter((b) => text.test(b.textContent ?? "")) as HTMLElement[];
}

function button(el: HTMLElement, text: RegExp): HTMLElement {
  const [found] = buttons(el, text);
  if (!found) throw new Error(`no button ${text}`);
  return found;
}

/** The text of each body row of the table at `index` in the section, or none when it is not drawn. */
function keysIn(el: HTMLElement, index: number): string[] {
  const table = el.querySelectorAll("table")[index];
  return table ? [...table.querySelectorAll("tbody tr")].map((tr) => tr.textContent ?? "") : [];
}

async function click(target: HTMLElement) {
  await act(async () => target.click());
  await act(async () => {});
}

// The rows a table draws, after the pipeline's own table (always first in the section).
const LIVE = 1;
const RETIRED = 2;

describe("FieldsSettings", () => {
  it("retires a field with its replacement, moving it behind the retired disclosure, and restores it", async () => {
    const el = await mount();
    expect(keysIn(el, LIVE).some((row) => row.includes("Fit") && !row.includes("Fit grade"))).toBe(true);
    expect(buttons(el, /retired field/)).toHaveLength(0);

    // The first Retire belongs to the first custom field, Fit.
    await click(buttons(el, /^Retire$/)[0]);
    const picker = el.querySelector("#retire-field-replacement") as HTMLSelectElement;
    // The field cannot replace itself; the other live field is offered by its bare key.
    expect([...picker.options].map((o) => o.value)).toEqual(["", "fit_grade"]);
    await act(async () => {
      picker.value = "fit_grade";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(button(el, /^Retire field$/));

    expect(el.querySelector("#retire-field-replacement")).toBeNull();
    expect(keysIn(el, LIVE).map((row) => row.includes("Fit grade"))).toEqual([true]);
    expect(keysIn(el, RETIRED)).toEqual([]);

    await click(button(el, /^Show 1 retired field$/));
    const [retiredRow] = keysIn(el, RETIRED);
    expect(retiredRow).toContain("fit");
    expect(retiredRow).toContain("Company · Fit grade");

    await click(button(el, /^Restore$/));
    expect(keysIn(el, LIVE)).toHaveLength(2);
    expect(buttons(el, /retired field/)).toHaveLength(0);
  });

  it("shows the server's refusal in the retire form and keeps the field live", async () => {
    server.refuse = "replaced_by 'fit_grade' is retired";
    const el = await mount();
    await click(buttons(el, /^Retire$/)[0]);
    await click(button(el, /^Retire field$/));

    expect(el.textContent).toContain("replaced_by 'fit_grade' is retired");
    expect(el.querySelector("#retire-field-replacement")).not.toBeNull();
    expect(server.fields.every((f) => !f.retired_at)).toBe(true);
  });

  it("shows a call refused for no verified identity as its message, never a code", async () => {
    const el = await mount();
    server.flagged = "This request carries no verified identity, so the server did not run it.";
    await click(buttons(el, /^Retire$/)[0]);
    await click(button(el, /^Retire field$/));

    expect(el.textContent).toContain(server.flagged);
    expect(el.textContent).not.toContain("service_error");
    expect(el.textContent).not.toContain("auth_required");
    expect(el.textContent).not.toContain("unauthenticated");
  });

  it("lets an admin mark a field as a default column, and hides the control from a member", async () => {
    const el = await mount();
    const box = el.querySelector<HTMLInputElement>('input[aria-label="Open Fit grade as a default column"]');
    expect(box?.checked).toBe(false);
    await act(async () => box?.click());
    expect(server.fields.find((f) => f.key === "fit_grade")?.default_column).toBe(true);
    expect(el.querySelector<HTMLInputElement>('input[aria-label="Open Fit grade as a default column"]')?.checked).toBe(true);
    expect(server.fields.find((f) => f.key === "fit")?.default_column).toBeUndefined();

    act(() => root?.unmount());
    host.allowedTools = [];
    const member = await mount();
    expect(member.querySelector('input[aria-label="Open Fit grade as a default column"]')).toBeNull();
  });

  it("lets an admin rename a field, showing a refusal, and hides Edit from a member", async () => {
    const el = await mount();
    await click(buttons(el, /^Edit$/)[1]);
    const input = el.ownerDocument.querySelector<HTMLInputElement>('input[value="Fit grade"]');
    expect(input?.value).toBe("Fit grade");
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setValue?.call(input, "Fit score");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    server.refuse = "label is too long";
    await click(button(el, /^Save$/));
    expect(el.ownerDocument.body.textContent).toContain("label is too long");
    expect(server.fields.find((f) => f.key === "fit_grade")?.label).toBe("Fit grade");

    server.refuse = null;
    await click(button(el, /^Save$/));
    expect(server.fields.find((f) => f.key === "fit_grade")?.label).toBe("Fit score");
    expect(keysIn(el, LIVE).some((row) => row.includes("Fit score"))).toBe(true);

    act(() => root?.unmount());
    host.allowedTools = [];
    const member = await mount();
    expect(buttons(member, /^Edit$/)).toHaveLength(0);
  });

  it("shows a rejected edit and lets the admin retry or cancel", async () => {
    const el = await mount();
    await click(buttons(el, /^Edit$/)[1]);

    server.reject = "connection lost";
    await click(button(el, /^Save$/));
    expect(el.ownerDocument.body.textContent).toContain("connection lost");
    expect((button(el, /^Save$/) as HTMLButtonElement).disabled).toBe(false);
    expect((button(el, /^Cancel$/) as HTMLButtonElement).disabled).toBe(false);

    server.reject = null;
    await click(button(el, /^Save$/));
    expect(el.ownerDocument.body.textContent).not.toContain("connection lost");
    expect(buttons(el, /^Save$/)).toHaveLength(0);
  });

  it("edits a field's description and removes an unused option in one save", async () => {
    server.fields[1] = field("fit_grade", "Fit grade", { options: ["A", "B", "C"], description: "Old words." });
    const el = await mount();
    await click(buttons(el, /^Edit$/)[1]);
    const area = el.ownerDocument.querySelector<HTMLTextAreaElement>("textarea");
    expect(area?.value).toBe("Old words.");
    const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      setValue?.call(area, "New words.");
      area?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(el.ownerDocument.querySelector('input[aria-label="Remove option C"]') as HTMLElement);
    await click(button(el, /^Save$/));
    const saved = server.fields.find((f) => f.key === "fit_grade");
    expect(saved?.description).toBe("New words.");
    expect(saved?.options).toEqual(["A", "B"]);
  });

  it("shows a number field's range and edits it whole", async () => {
    server.fields[1] = field("score", "Score", { type: "number", options: [], minimum: 1, maximum: 5 });
    const el = await mount();
    expect(keysIn(el, LIVE).some((row) => row.includes("number · 1–5"))).toBe(true);
    await click(buttons(el, /^Edit$/)[1]);
    const max = el.ownerDocument.querySelector<HTMLInputElement>('input[value="5"]');
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setValue?.call(max, "");
      max?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(button(el, /^Save$/));
    const saved = server.fields.find((f) => f.key === "score");
    expect([saved?.minimum, saved?.maximum]).toEqual([1, null]);
  });

  it("lists retired fields to a member without Retire or Restore", async () => {
    host.allowedTools = [];
    server.fields[0] = field("fit", "Fit", { retired_at: "2026-10-20T00:00:00Z", replaced_by: "fit_grade" });
    const el = await mount();

    expect(keysIn(el, LIVE).map((row) => row.includes("Fit grade"))).toEqual([true]);
    expect(buttons(el, /^Retire$/)).toHaveLength(0);
    await click(button(el, /^Show 1 retired field$/));
    expect(keysIn(el, RETIRED)[0]).toContain("Company · Fit grade");
    expect(buttons(el, /^Restore$/)).toHaveLength(0);
  });

  it("offers each control for its own tool: update_field gives Retire, and no New field", async () => {
    host.allowedTools = ["update_field"];
    const el = await mount();

    expect(buttons(el, /^Retire$/).length).toBeGreaterThan(0);
    expect(buttons(el, /^New field$/)).toHaveLength(0);
    expect(el.textContent).toContain("A workspace admin defines them.");
  });
});

describe("PipelineSettings", () => {
  const stage = (key: string, position: number) => ({ id: key, key, label: key, position, probability: 50, outcome: "open", deals: 0 });

  async function mountPipeline(tools: string[]) {
    host.allowedTools = tools;
    server.stages = [stage("lead", 0), stage("won", 1)];
    const el = await mount(PipelineSettings);
    return {
      newStage: buttons(el, /^New stage$/).length > 0,
      editable: el.querySelector('[aria-label="Label of lead"]') !== null,
      reorder: el.querySelector('[aria-label="Move down"]') !== null,
      remove: buttons(el, /^Remove$/).length,
      retire: buttons(el, /^Retire$/).length,
    };
  }

  it("gives define_pipeline_stage New stage, and nothing else", async () => {
    expect(await mountPipeline(["define_pipeline_stage"])).toEqual({ newStage: true, editable: false, reorder: false, remove: 0, retire: 0 });
  });

  it("gives update_pipeline_stage editing, reordering and retiring, and no New stage", async () => {
    expect(await mountPipeline(["update_pipeline_stage"])).toEqual({ newStage: false, editable: true, reorder: true, remove: 0, retire: 2 });
  });

  it("gives a viewer with neither tool the stages to read", async () => {
    expect(await mountPipeline([])).toEqual({ newStage: false, editable: false, reorder: false, remove: 0, retire: 0 });
  });

  it("retires a stage through update_pipeline_stage, naming its replacement, and lists it under Retired", async () => {
    host.allowedTools = ["update_pipeline_stage"];
    server.stages = [stage("lead", 0), stage("proposal", 1), stage("negotiation", 2)];
    const el = await mount(PipelineSettings);
    await act(async () => buttons(el, /^Retire$/)[1].click());
    const select = el.querySelector('select[aria-label="Replaced by"]') as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["", "lead", "negotiation"]);
    await act(async () => {
      select.value = "negotiation";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => buttons(el, /^Retire stage$/)[0].click());
    expect(server.stageCalls).toEqual([{ key: "proposal", retired: true, replaced_by: "negotiation" }]);
    expect(el.querySelector('[aria-label="Label of proposal"]')).toBeNull();
    expect(el.textContent).toContain("Retired");
    expect(el.textContent).toContain("proposal → negotiation");
    expect(buttons(el, /^Restore$/)).toHaveLength(1);
  });

  it("shows the server's refusal and keeps the stage live", async () => {
    host.allowedTools = ["update_pipeline_stage"];
    server.stages = [stage("lead", 0), stage("proposal", 1)];
    server.refuse = "stage 'proposal' holds 2 open deals, which a retired stage cannot";
    const el = await mount(PipelineSettings);
    await act(async () => buttons(el, /^Retire$/)[1].click());
    await act(async () => buttons(el, /^Retire stage$/)[0].click());
    expect(el.textContent).toContain("holds 2 open deals");
    expect(el.querySelector('[aria-label="Label of proposal"]')).not.toBeNull();
  });

  it("restores a retired stage through update_pipeline_stage", async () => {
    host.allowedTools = ["update_pipeline_stage"];
    server.stages = [stage("lead", 0), { ...stage("old", 1), outcome: "lost", retired_at: "2026-11-01T00:00:00Z" }];
    const el = await mount(PipelineSettings);
    expect(el.querySelector('[aria-label="Label of old"]')).toBeNull();
    await act(async () => buttons(el, /^Restore$/)[0].click());
    expect(server.stageCalls).toEqual([{ key: "old", retired: false }]);
    expect(el.querySelector('[aria-label="Label of old"]')).not.toBeNull();
    expect(buttons(el, /^Restore$/)).toHaveLength(0);
  });
});
