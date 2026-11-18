import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

// The subject is the Deals section a contact or organization page draws from its read: each
// open deal it carries, named by its stage's label once the pipeline answers, with its value,
// opening in place; and how many more are open beyond the few carried.
const { call } = vi.hoisted(() => ({ call: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), useCall: () => call }));

import { OpenDeals } from "./OpenDeals";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const DEALS = [
  { id: "op_01A", uri: "people://opportunities/op_01A", title: "Larkfield pilot", stage: "proposal", value: "12000.00", currency: "USD" },
  { id: "op_01B", uri: "people://opportunities/op_01B", title: "Larkfield expansion", stage: "lead", value: null, currency: null },
];

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

async function render(deals: typeof DEALS, total: number, onOpen = vi.fn()) {
  call.mockResolvedValue({ stages: [{ key: "proposal", label: "Proposal sent" }, { key: "lead", label: "Lead" }] });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(createElement(OpenDeals, { deals, total, onOpen })));
  return onOpen;
}

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  call.mockReset();
});

describe("OpenDeals", () => {
  it("lists each deal with its stage's label and its value, and opens one in place", async () => {
    const onOpen = await render(DEALS, 2);
    const text = container.textContent ?? "";
    expect(text).toContain("Larkfield pilot");
    expect(text).toContain("Proposal sent");
    expect(text).toContain("12,000");
    expect(text).toContain("Larkfield expansion");
    expect(text).not.toContain("more open");

    const row = [...container.querySelectorAll<HTMLElement>("[data-deals] *")].find((el) =>
      el.textContent === "Larkfield pilot",
    );
    await act(async () => row?.click());
    expect(onOpen).toHaveBeenCalledWith("op_01A");
  });

  it("says how many more are open than the read carries", async () => {
    await render(DEALS, 7);
    expect(container.textContent).toContain("5 more open deals");
  });

  it("says there are none, without reading the pipeline", async () => {
    await render([], 0);
    expect(container.textContent).toContain("No open deals.");
    expect(call).not.toHaveBeenCalled();
  });
});
