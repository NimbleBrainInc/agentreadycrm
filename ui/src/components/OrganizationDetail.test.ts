import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

// The subject is how the page answers a re-read: a `refreshKey` change reads the organization
// again in place, so a form the reader has open on it stays open with what they typed. The
// form stands in as an input holding its own state, which a remount would discard.
const { call } = vi.hoisted(() => ({ call: vi.fn() }));

vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), useCall: () => call }));
vi.mock("./SimilarCompanies", () => ({ SimilarCompanies: () => null }));
vi.mock("./forms", () => ({
  EditOrganizationForm: () => {
    const [name, setName] = useState("");
    return createElement("input", {
      "data-form": "edit",
      value: name,
      onChange: (e: { target: { value: string } }) => setName(e.target.value),
    });
  },
  DossierForm: () => null,
}));

import { OrganizationDetail } from "./OrganizationDetail";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const ORG = {
  organization: { id: "org_01A", name: "Larkfield", domain: null, created_at: "2026-01-01T00:00:00Z" },
  contacts: [],
  interactions: [],
};

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  call.mockReset();
});

const render = (refreshKey: number) =>
  act(async () =>
    root.render(
      createElement(OrganizationDetail, { key: "org_01A", organizationId: "org_01A", onNavigate: () => {}, refreshKey }),
    ),
  );

describe("a re-read of the open organization", () => {
  it("reads it again in place, leaving an open form and its text", async () => {
    call.mockResolvedValue(ORG);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await render(0);
    // One read is the organization, its people and its history, called together.
    expect(call).toHaveBeenCalledTimes(3);

    const edit = [...container.querySelectorAll("button")].find((b) => b.textContent === "Edit");
    await act(async () => edit?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    const input = container.querySelector<HTMLInputElement>("input[data-form=edit]");
    expect(input).not.toBeNull();
    // React tracks an input's value through its own setter; set it the way a keystroke does.
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setValue?.call(input, "Larkfield Ltd");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await render(1);

    expect(call).toHaveBeenCalledTimes(6);
    expect(call).toHaveBeenCalledWith("get_organization", { organization_id: "org_01A" });
    expect(call).toHaveBeenLastCalledWith("list_interactions", { organization_id: "org_01A", limit: 200 });
    expect(container.querySelector<HTMLInputElement>("input[data-form=edit]")?.value).toBe("Larkfield Ltd");
  });

  it("shows the newer read when two overlapping reads resolve out of order", async () => {
    const pending: ((value: unknown) => void)[] = [];
    call.mockResolvedValueOnce(ORG).mockResolvedValueOnce(ORG).mockResolvedValueOnce(ORG);
    call.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await render(0);

    await render(1);
    await render(2);
    // Each read is three calls; settle the second read's three, then the first's.
    expect(pending).toHaveLength(6);
    const named = (name: string) => ({ ...ORG, organization: { ...ORG.organization, name } });
    await act(async () => pending.slice(3).forEach((resolve) => resolve(named("Larkfield Newer"))));
    await act(async () => pending.slice(0, 3).forEach((resolve) => resolve(named("Larkfield Older"))));

    expect(container.querySelector("h1, h2, h3, h4")?.textContent).toBe("Larkfield Newer");
  });
});

// The page shows the organization's whole roster and history, and a collection that did not
// load as an error: the listings page by cursor, and each page is its own call that can fail.
describe("the organization's collections", () => {
  const person = (id: string) => ({ id, name: `Person ${id}`, title: null, effective_status: "active" });
  const mount = async () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await render(0);
  };

  it("reads every page of the roster", async () => {
    call.mockImplementation(async (name: string, args: { cursor?: string }) => {
      if (name === "list_contacts") {
        return args.cursor === "page2"
          ? { contacts: [person("c2")], next_cursor: null }
          : { contacts: [person("c1")], next_cursor: "page2" };
      }
      return ORG;
    });
    await mount();

    expect(call).toHaveBeenCalledWith("list_contacts", { organization_id: "org_01A", limit: 200, cursor: "page2" });
    expect(container.textContent).toContain("People (2)");
    expect(container.textContent).toContain("Person c1");
    expect(container.textContent).toContain("Person c2");
  });

  it("shows a roster that failed to load as an error, not as no contacts", async () => {
    call.mockImplementation(async (name: string) =>
      name === "list_contacts" ? { error: "rate_limited", message: "Too many requests." } : ORG,
    );
    await mount();

    expect(container.textContent).toContain("Too many requests.");
    expect(container.textContent).not.toContain("No contacts at this organization yet.");
  });

  it("shows a history page that failed to load as an error", async () => {
    call.mockImplementation(async (name: string, args: { cursor?: string }) => {
      if (name === "list_interactions") {
        return args.cursor
          ? { error: "internal_error", message: "History is unavailable." }
          : { interactions: [], next_cursor: "page2" };
      }
      return ORG;
    });
    await mount();

    expect(container.textContent).toContain("History is unavailable.");
  });
});
