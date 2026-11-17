import { describe, expect, it } from "vitest";
import { resultData } from "./api";
import { isApiError } from "./types";

// Every failure is a result flagged `isError`. The bridge hands `resultData` its first text
// block as `data`: the server's envelope `{error: {code, message, retryable}, next_step}`,
// JSON-parsed, or plain text when a host or a schema rejection wrote it.
describe("resultData", () => {
  it("turns the server's envelope into an ApiError carrying its code and next step", () => {
    const body = {
      error: { code: "not_found", message: "No contact.", retryable: false, entity: "contact" },
      next_step: "Verify the contact id.",
    };
    const value = resultData<unknown>({ data: body, isError: true });
    expect(isApiError(value)).toBe(true);
    expect(value).toEqual({
      error: "not_found",
      message: "No contact.",
      next_step: "Verify the contact id.",
      entity: "contact",
      field: undefined,
    });
  });

  it.each([
    ["unauthenticated", "This request carries no verified identity, so the server did not run it."],
    ["internal_error", "The People database is temporarily unreachable. Retry shortly; nothing was read or written."],
  ])("reads a %s failure the same way", (code, message) => {
    const body = { error: { code, message, retryable: false }, next_step: "Call again." };
    expect(resultData<{ error: string; message: string }>({ data: body, isError: true })).toMatchObject({
      error: code,
      message,
    });
  });

  it("turns flagged plain text into an ApiError carrying it", () => {
    const value = resultData<unknown>({ data: "Unexpected keyword argument", isError: true });
    expect(value).toEqual({ error: "error", message: "Unexpected keyword argument" });
  });

  it("passes a success through", () => {
    expect(resultData<unknown>({ data: { contacts: [] }, isError: false })).toEqual({ contacts: [] });
  });
});
