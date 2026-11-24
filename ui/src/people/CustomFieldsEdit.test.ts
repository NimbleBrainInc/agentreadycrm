import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FieldDef } from "../types";

// The subject is the Fields card of a record page: each value set or cleared in place with the
// record's own update tool, and a new field defined from the card. The server and the host
// decide both writes, so a refusal shows as they word it. Tool calls go through `useCall`,
// which stands in here as a mock answering each call.
const { call } = vi.hoisted(() => ({ call: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), useCall: () => call }));

import { CustomFields } from "./pagekit";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

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
  call.mockReset();
});

const def = (key: string, type: string, extra: Partial<FieldDef> = {}) =>
  ({ entity: "organization", key, label: key.toUpperCase(), type, created_at: "", updated_at: "", ...extra }) as FieldDef;
const DEFS = [
  def("fit", "select", { options: ["Strong", "Weak"] }),
  def("note", "text"),
  def("seats", "number"),
  def("tags", "list"),
];

const mount = (defs: FieldDef[], values: Record<string, unknown> = {}) =>
  act(async () =>
    root.render(createElement(CustomFields, { defs, values, details: undefined, edit: { kind: "organization", id: "org_01A" } })),
  );
const find = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === text);
const press = (el: Element | null | undefined) => act(async () => el?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const edit = (label: string) => press(container.querySelector(`button[aria-label="Edit ${label}"]`));
// React tracks a control's value through its own setter; set it the way input does.
async function type(el: HTMLInputElement | HTMLSelectElement | null, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, value);
    el?.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

describe("a value in the Fields card", () => {
  it("is set in place with the record's update tool, attributed to a person", async () => {
    call.mockResolvedValue({ organization: {} });
    await mount(DEFS, { fit: "Weak" });
    await edit("FIT");
    await type(container.querySelector("select"), "Strong");
    await press(find("Save"));
    expect(call).toHaveBeenCalledWith("update_organization", {
      organization_id: "org_01A",
      fields: { fit: "Strong" },
      fields_by: "a person, in the People app",
    });
    expect(container.querySelector("form")).toBeNull();
  });

  it("is cleared when emptied, and a list and a number go as their types", async () => {
    call.mockResolvedValue({ organization: {} });
    await mount(DEFS, { note: "Met at a conference", seats: 3, tags: ["a"] });
    await edit("NOTE");
    await type(container.querySelector("input"), "  ");
    await press(find("Save"));
    expect(call).toHaveBeenLastCalledWith("update_organization", expect.objectContaining({ fields: { note: null } }));

    await edit("SEATS");
    await type(container.querySelector("input"), "12");
    await press(find("Save"));
    expect(call).toHaveBeenLastCalledWith("update_organization", expect.objectContaining({ fields: { seats: 12 } }));

    await edit("TAGS");
    await type(container.querySelector("input"), "east, , west");
    await press(find("Save"));
    expect(call).toHaveBeenLastCalledWith("update_organization", expect.objectContaining({ fields: { tags: ["east", "west"] } }));
  });

  it("shows the server's refusal and keeps the form open with what was typed", async () => {
    call.mockResolvedValue({ error: "invalid_request", message: "SEATS takes at most 10." });
    await mount(DEFS, { seats: 3 });
    await edit("SEATS");
    await type(container.querySelector("input"), "12");
    await press(find("Save"));
    expect(container.textContent).toContain("SEATS takes at most 10.");
    expect(container.querySelector<HTMLInputElement>("input")?.value).toBe("12");
  });

  it("is not edited where the card shows another record's fields", async () => {
    await act(async () => root.render(createElement(CustomFields, { defs: DEFS, values: { fit: "Weak" }, details: undefined })));
    expect(container.querySelector('button[aria-label="Edit FIT"]')).toBeNull();
    expect(find("Add field")).toBeUndefined();
  });
});

describe("adding a field from the card", () => {
  it("is offered in place of the empty message when no field is defined", async () => {
    await mount([]);
    expect(container.textContent).not.toContain("No custom fields defined.");
    expect(find("Add field")).toBeDefined();
  });

  it("defines the field for the card's record kind, without asking which", async () => {
    call.mockResolvedValue({ field: {} });
    await mount([]);
    await press(find("Add field"));
    expect(container.textContent).not.toContain("Describes");
    await type(container.querySelector<HTMLInputElement>('input[placeholder="e.g. Funding stage"]'), "Funding stage");
    await type(container.querySelector<HTMLSelectElement>("#new-field-type"), "text");
    await press([...container.querySelectorAll("button")].filter((b) => b.textContent === "Add field").at(-1));
    expect(call).toHaveBeenCalledWith(
      "define_field",
      expect.objectContaining({ entity: "organization", key: "funding_stage", label: "Funding stage", type: "text" }),
    );
  });

  it("shows the account's field limit and a host's refusal as they are worded", async () => {
    await mount([]);
    await press(find("Add field"));
    await type(container.querySelector<HTMLInputElement>('input[placeholder="e.g. Funding stage"]'), "Region");
    await type(container.querySelector<HTMLSelectElement>("#new-field-type"), "text");
    const submit = () => press([...container.querySelectorAll("button")].filter((b) => b.textContent === "Add field").at(-1));

    call.mockResolvedValueOnce({ error: "invalid_request", message: "This account is at its limit of 3 custom fields." });
    await submit();
    expect(container.textContent).toContain("This account is at its limit of 3 custom fields.");

    call.mockRejectedValueOnce(new Error("define_field is for workspace admins."));
    await submit();
    expect(container.textContent).toContain("define_field is for workspace admins.");
  });
});
