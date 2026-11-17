import { useCallback } from "react";
import { useApp } from "@nimblebrain/synapse/react";
import { type ApiError, isApiError, toApiError } from "./types";

/** A call's value: the tool's payload, or an `ApiError` for a result flagged `isError`. */
export function resultData<T>(res: { data: unknown; isError?: boolean }): T {
  return (res.isError === true ? toApiError(res.data) : res.data) as T;
}

/**
 * Returns a typed `call(toolName, args)` bound to the app's tool bridge.
 *
 * `app.callTool` returns a `ToolCallResult` whose `.data` is the tool's first text
 * content block, JSON-parsed when it is JSON. Every failure (not-found, conflict, a bad
 * argument, no verified identity, the database unreachable, a workspace at its limit) is
 * a result flagged `isError` whose body is `{error: {code, message, ...}, next_step}`; it
 * comes back here as an `ApiError` (`toApiError`), so callers inspect the returned value
 * (see `isApiError`) instead of catching. A failure of the transport itself still
 * rejects; callers may catch that.
 *
 * Every tool People's UI calls is People's own: an app reaches its own server and
 * nothing else. Work that needs another source is the agent's — the app asks for
 * it and the agent picks whatever provider is installed.
 */
export function useCall(): <T>(name: string, args?: Record<string, unknown>) => Promise<T> {
  const app = useApp();
  return useCallback(
    async <T>(name: string, args?: Record<string, unknown>): Promise<T> =>
      resultData<T>(await app.callTool(name, args)),
    [app],
  );
}

/**
 * A record's whole collection from a keyset listing (`list_contacts`, `list_interactions`):
 * every page under `key`, read in turn until `next_cursor` is null. A failed page returns its
 * `ApiError`, never the rows read before it, so a view cannot show part of a collection as
 * all of it.
 */
export async function readAll<T>(
  call: <R>(name: string, args?: Record<string, unknown>) => Promise<R>,
  name: string,
  args: Record<string, unknown>,
  key: string,
): Promise<T[] | ApiError> {
  const rows: T[] = [];
  let cursor: string | null = null;
  do {
    const page: Record<string, unknown> | ApiError = await call(name, {
      ...args,
      limit: 200,
      ...(cursor ? { cursor } : {}),
    });
    if (isApiError(page)) return page;
    rows.push(...((page[key] as T[] | undefined) ?? []));
    cursor = (page.next_cursor as string | null | undefined) ?? null;
  } while (cursor);
  return rows;
}
