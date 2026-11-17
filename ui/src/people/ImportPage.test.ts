import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

// The page reads the workspace's fields, then sends the file's rows to the import tool in
// batches. The tool is the app's `callTool`, captured here, answering as the server would.
const { calls, app, refuse } = vi.hoisted(() => {
  const calls: { name: string; args: Record<string, unknown> }[] = [];
  // Set to have the import tool refuse every batch, or keep a value on each batch's first row.
  const refuse = { on: false, keep: false };
  // One app for the life of the page, as a host gives: a new one per render would re-run
  // every effect keyed on the app's call.
  const app = {
    hostCapabilities: { downloadFile: false },
    callTool: async (name: string, args: Record<string, unknown>) => {
      calls.push({ name, args });
      if (name === "list_fields") {
        return {
          data: {
            fields: {
              organization: {
                core: [{ key: "name", type: "text" }],
                custom: [{ key: "vendor_id", label: "Vendor id", type: "text", options: [] }],
              },
            },
          },
        };
      }
      if (refuse.on) return { data: { error: "unavailable", message: "the database is unavailable" } };
      const rows = args.rows as Record<string, unknown>[];
      // The second row of every batch is refused, to see it named by its file row.
      return {
        data: {
          received: rows.length,
          created: rows.length - 1,
          matched: 0,
          promoted: 0,
          kept: refuse.keep ? [{ reason: "kept the domain already held", rows: [0] }] : [],
          skipped: [{ reason: "field 'vendor_id' takes a non-empty string", rows: [1] }],
        },
      };
    },
  };
  return { calls, app, refuse };
});

vi.mock("@nimblebrain/synapse/react", () => ({ useApp: () => app }));
vi.mock("@nimblebrain/synapse", () => ({ downloadFile: vi.fn() }));

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const { ImportPage } = await import("./ImportPage");

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  calls.length = 0;
  refuse.on = false;
  refuse.keep = false;
});

async function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root?.render(createElement(ImportPage, { entity: "organization", onBack: () => {} })));
  return container;
}

async function choose(el: HTMLElement, csv: string) {
  const input = el.querySelector('input[type="file"]') as HTMLInputElement;
  // jsdom's File has no text(), which every browser's does.
  const file = Object.assign(new File([csv], "orgs.csv", { type: "text/csv" }), { text: async () => csv });
  Object.defineProperty(input, "files", { value: [file] });
  await act(async () => {
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  // Reading the file resolves after the event; let it land.
  await act(async () => {});
}

function button(el: HTMLElement, text: RegExp): HTMLButtonElement {
  const found = [...el.querySelectorAll("button")].find((b) => text.test(b.textContent ?? ""));
  if (!found) throw new Error(`no button ${text}`);
  return found as HTMLButtonElement;
}

describe("ImportPage", () => {
  it("guesses the columns, counts what it will send, and sends it in batches of 250, one at a time", async () => {
    const el = await mount();
    const lines = ["Company,Domain,Vendor ID,Notes"];
    for (let i = 0; i < 300; i++) lines.push(`Org ${i},org${i}.example,v-${i},`);
    lines.push(",,v-orphan,no name or domain");
    await choose(el, lines.join("\n"));

    const selects = [...el.querySelectorAll("select")] as HTMLSelectElement[];
    expect(selects.slice(0, 4).map((s) => s.value)).toEqual(["name", "domain", "fields.vendor_id", ""]);
    expect(el.textContent).toContain("300 of 301 rows will be imported.");
    expect(el.textContent).toContain("1 has no name or domain and is left out.");
    expect(el.textContent).toContain("An organization already here is matched");

    await act(async () => button(el, /^Import 300 organizations$/).click());
    await act(async () => {});

    const sent = calls.filter((c) => c.name === "import_organizations");
    expect(sent.map((c) => (c.args.rows as unknown[]).length)).toEqual([250, 50]);
    expect((sent[0].args.rows as unknown[])[0]).toEqual({
      name: "Org 0",
      domain: "org0.example",
      fields: { vendor_id: "v-0" },
    });
    expect(el.textContent).toContain("300 rows sent: 298 added, 0 already here, 2 skipped.");
    // The skipped rows are named by the file rows they came from (header is row 1).
    expect(el.textContent).toContain("Row 3: field 'vendor_id' takes a non-empty string");
    expect(el.textContent).toContain("Row 253: field 'vendor_id' takes a non-empty string");
  });

  it("stops at a refused batch and names its rows as skipped", async () => {
    refuse.on = true;
    const el = await mount();
    const lines = ["Company,Domain"];
    for (let i = 0; i < 300; i++) lines.push(`Org ${i},org${i}.example`);
    await choose(el, lines.join("\n"));

    await act(async () => button(el, /^Import 300 organizations$/).click());
    await act(async () => {});

    expect(calls.filter((c) => c.name === "import_organizations")).toHaveLength(1);
    expect(el.textContent).toContain("The import stopped: the database is unavailable");
    expect(el.textContent).toContain("250 rows sent: 0 added, 0 already here, 250 skipped.");
    expect(el.textContent).toContain("Row 2: the database is unavailable");
  });

  it("sends an id column as a reference under the prefix given, and holds back a bad prefix", async () => {
    const el = await mount();
    await choose(el, "Company,Source ID\nAcme,123\n,456\n");
    const selects = [...el.querySelectorAll("select")] as HTMLSelectElement[];
    // A reference is never guessed: the id column starts unmapped.
    expect(selects[1].value).toBe("");
    await act(async () => {
      selects[1].value = "external_refs";
      selects[1].dispatchEvent(new Event("change", { bubbles: true }));
    });
    const prefix = el.querySelector('input[aria-label="Reference prefix"]') as HTMLInputElement;
    expect(el.textContent).toContain("Give the prefix to import.");
    const type = async (text: string) =>
      act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        setter?.call(prefix, text);
        prefix.dispatchEvent(new Event("input", { bubbles: true }));
      });
    await type("Vendor companies/");
    expect(button(el, /^Import/).disabled).toBe(true);
    await type("vendor://companies/");
    // The row with only an id is sent too: the server matches it or names why not.
    expect(el.textContent).toContain("2 of 2 rows will be imported.");
    await act(async () => button(el, /^Import 2/).click());
    await act(async () => {});
    const [sent] = calls.filter((c) => c.name === "import_organizations");
    expect(sent.args.rows).toEqual([
      { name: "Acme", external_refs: ["vendor://companies/123"] },
      { external_refs: ["vendor://companies/456"] },
    ]);
  });

  it("says when one matched organization kept a value of its own", async () => {
    refuse.keep = true;
    const el = await mount();
    await choose(el, "Company,Domain\nAcme,acme.example\nBeta,beta.example\n");
    await act(async () => button(el, /^Import 2 organizations$/).click());
    await act(async () => {});
    expect(el.textContent).toContain("1 matched organization kept a value of its own in place of the file's.");
  });

  it("draws no back link of its own under a host trail", async () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () =>
      root?.render(createElement(ImportPage, { entity: "organization", onBack: () => {}, hostShowsTrail: true })),
    );
    expect(container.textContent).not.toContain("← Organizations");
  });

  it("will not import until a name or a domain column is chosen", async () => {
    const el = await mount();
    await choose(el, "Notes,Size\nfine,12\n");
    expect(el.textContent).toContain(
      "Choose the column that holds each organization's name or domain, or map an id column to External reference.",
    );
    expect(button(el, /^Import/).disabled).toBe(true);
  });
});
