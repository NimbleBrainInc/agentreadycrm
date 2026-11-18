import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fmtDate } from "../format";
import { dealLines, dueState } from "./cells";
import { stageKey } from "./PipelineSettings";

describe("dealLines", () => {
  it("drops the account's name from the front of a title", () => {
    expect(dealLines("Larkfield · signal scoring pilot", "Larkfield")).toEqual({ title: "signal scoring pilot", account: "Larkfield" });
    expect(dealLines("larkfield: renewal", "Larkfield")).toEqual({ title: "renewal", account: "Larkfield" });
  });
  it("only drops the name as a whole word", () => {
    expect(dealLines("Acmeville renewal", "Acme")).toEqual({ title: "Acmeville renewal", account: "Acme" });
    expect(dealLines("Acme renewal", "Acme")).toEqual({ title: "renewal", account: "Acme" });
  });
  it("keeps a title that is only the account's name, and one that does not start with it", () => {
    expect(dealLines("Larkfield", "Larkfield")).toEqual({ title: "Larkfield", account: "Larkfield" });
    expect(dealLines("Q4 expansion", "Larkfield")).toEqual({ title: "Q4 expansion", account: "Larkfield" });
    expect(dealLines("Q4 expansion", null)).toEqual({ title: "Q4 expansion", account: null });
  });
});

describe("dueState", () => {
  // 6pm in Hawaii on Oct 3, which is already Oct 4 in UTC: a close date is a
  // calendar day where the viewer is, not a UTC instant.
  beforeEach(() => {
    vi.stubEnv("TZ", "Pacific/Honolulu");
    vi.useFakeTimers({ now: new Date("2026-10-04T04:00:00Z") });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it("reads a date-only value as the viewer's day, west of Greenwich too", () => {
    expect(fmtDate("2026-10-12")).toBe("Oct 12, 2026");
    expect(dueState("2026-10-03", true)).toEqual({ kind: "soon", label: "Due today" });
  });

  it("calls a past close date on an open deal overdue, in days, weeks or months", () => {
    expect(dueState("2026-09-30", true)).toEqual({ kind: "overdue", label: "Overdue 3 d" });
    expect(dueState("2026-09-13", true)).toEqual({ kind: "overdue", label: "Overdue 3 wk" });
    expect(dueState("2026-07-01", true)).toEqual({ kind: "overdue", label: "Overdue 3 mo" });
  });
  it("calls the coming week soon and anything later a date", () => {
    expect(dueState("2026-10-03", true)).toEqual({ kind: "soon", label: "Due today" });
    expect(dueState("2026-10-06", true)).toEqual({ kind: "soon", label: "Due in 3 d" });
    expect(dueState("2026-11-09", true).kind).toBe("later");
  });
  it("gives a closed deal's date no urgency, and no date nothing", () => {
    expect(dueState("2026-09-13", false).kind).toBe("later");
    expect(dueState(null, true)).toEqual({ kind: "none", label: "" });
  });
});

describe("stageKey", () => {
  it("is the key the server will store, whatever was typed", () => {
    expect(stageKey("Closed_Won", "x")).toBe("closed_won");
    expect(stageKey(" proposal ", "x")).toBe("proposal");
    expect(stageKey("", "Contract sent!")).toBe("contract_sent");
  });
});
