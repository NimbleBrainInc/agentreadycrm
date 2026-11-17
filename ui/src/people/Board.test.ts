import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { call } = vi.hoisted(() => ({ call: vi.fn() }));

vi.mock("@nimblebrain/synapse/react", () => ({ useDataSync: () => {} }));
vi.mock("../api", () => ({ useCall: () => call }));

import { Board } from "./Board";
import type { PipelineStage } from "../types";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const stage = (key: string, label: string, probability: number): PipelineStage =>
  ({ key, label, probability, outcome: "open" }) as PipelineStage;
const STAGES = [stage("qualified", "Qualified", 25), stage("proposal", "Proposal", 50)];

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(async () => {
  call.mockImplementation(async (tool: string) =>
    tool === "list_opportunities"
      ? { opportunities: [{ id: "op_1", title: "Pilot", stage: "qualified", "organization.name": "Larkfield" }] }
      : { opportunity: {} },
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      createElement(Board, { stages: STAGES, filter: undefined, showClosed: false, weighted: false, onOpen: () => {} }),
    ),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

const card = () => container.querySelector<HTMLElement>('[draggable="true"]')!;
/** A column: the element whose stripe is followed by a header naming the stage. */
const column = (label: string) =>
  [...container.querySelectorAll<HTMLElement>("div")]
    .filter((el) => el.firstElementChild?.nextElementSibling?.textContent?.startsWith(label))
    .at(-1)!;

/** A drag event as a browser sends it; jsdom has no DragEvent or DataTransfer. */
function drag(type: string, target: Element, id = "op_1"): void {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", {
    value: { setData: () => {}, getData: () => id, effectAllowed: "" },
  });
  target.dispatchEvent(event);
}

describe("Board drag", () => {
  it("fades the card only while it is being dragged", async () => {
    await act(async () => drag("dragstart", card()));
    expect(card().style.opacity).toBe("0.5");
    await act(async () => drag("dragend", card()));
    expect(card().style.opacity).toBe("1");
  });

  it("restores a card moved to another column, whose dragend never reaches React", async () => {
    const source = card();
    await act(async () => drag("dragstart", source));
    await act(async () => drag("drop", column("Proposal")));

    // The optimistic move remounts the card under its new column, so the drag
    // source is detached by the time the browser fires dragend on it.
    expect(card()).not.toBe(source);
    await act(async () => drag("dragend", source));

    expect(column("Proposal").contains(card())).toBe(true);
    expect(card().style.opacity).toBe("1");
    expect(call).toHaveBeenCalledWith("move_opportunity", { opportunity_id: "op_1", stage: "proposal" });
  });
});
