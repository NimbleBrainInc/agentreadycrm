import { useCallback, useEffect, useMemo, useState } from "react";
import { useDataSync } from "@nimblebrain/synapse/react";
import { Badge, EmptyState, Inline, Spinner, Text, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { fmtMoney, outcomeTone } from "../format";
import { dealLines, DueChip, dueState } from "./cells";
import { type ApiError, isApiError, type ListRowsResult, type PipelineStage, type Row } from "../types";
import type { Clause } from "./filters";

// The paths a card draws with. One read per board, at the list tool's largest page.
const CARD_COLUMNS = [
  "title",
  "organization.name",
  "stage",
  "outcome",
  "value",
  "currency",
  "probability",
  "weighted_value",
  "close_date",
];
const PAGE = 200;
// Past this many deals the board stops reading and says so: a column of
// hundreds of cards is a list, and the list view pages.
const MOST = 1000;

function money(amount: number, currency: string | null): string {
  return fmtMoney(amount, currency);
}

/** The stripe over a column: the outcome's color, an open stage's strength from its probability. */
function stripe(stage: PipelineStage): { color: string; opacity: number } {
  if (stage.outcome === "won") return { color: tokens.success, opacity: 1 };
  if (stage.outcome === "lost") return { color: tokens.fgFaint, opacity: 0.6 };
  return { color: tokens.accent, opacity: 0.3 + (stage.probability / 100) * 0.7 };
}

/**
 * The pipeline as columns, one per stage, over the same filter as the list.
 * A card is a deal; dragging it to another column is `move_opportunity`.
 * Order inside a column is by close date, soonest first: there is no manual rank,
 * so the board and the agent agree on what a column holds.
 */
export function Board({
  stages,
  filter,
  showClosed,
  weighted,
  onOpen,
}: {
  stages: PipelineStage[];
  /** The list's filter, open-by-default already lifted by the caller when closed columns show. */
  filter: Clause | undefined;
  showClosed: boolean;
  /** Whether a column's header sums weighted value or value. */
  weighted: boolean;
  onOpen: (id: string) => void;
}) {
  const call = useCall();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [moving, setMoving] = useState<string | null>(null);
  const filterKey = JSON.stringify(filter ?? null);

  useEffect(() => {
    let stale = false;
    const read = async () => {
      const out: Row[] = [];
      let cursor: string | undefined;
      try {
        do {
          const res = await call<ListRowsResult | ApiError>("list_opportunities", {
            filter,
            columns: CARD_COLUMNS,
            sort: "close_date",
            limit: PAGE,
            cursor,
          });
          if (stale) return;
          if (isApiError(res)) return setError(res.message ?? res.error);
          out.push(...(res.opportunities ?? []));
          cursor = res.next_cursor ?? undefined;
        } while (cursor && out.length < MOST);
        setTruncated(!!cursor);
        setError(null);
        setRows(out);
      } catch (err: unknown) {
        if (!stale) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void read();
    return () => {
      stale = true;
    };
    // `filterKey` stands in for `filter`, a fresh object every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call, filterKey, refresh]);

  // People announces every write, this board's moves included, so re-read.
  useDataSync(() => setRefresh((n) => n + 1));

  const columns = useMemo(() => stages.filter((s) => showClosed || s.outcome === "open"), [stages, showClosed]);
  const byStage = useMemo(() => {
    const map = new Map<string, Row[]>();
    for (const row of rows ?? []) {
      const key = String(row.stage);
      map.set(key, [...(map.get(key) ?? []), row]);
    }
    return map;
  }, [rows]);

  const move = useCallback(
    async (id: string, stage: string) => {
      setMoving(id);
      // Show the move at once; the announce that follows the write re-reads the truth.
      setRows((current) => current?.map((row) => (row.id === id ? { ...row, stage } : row)) ?? null);
      try {
        const res = await call<ApiError | { opportunity: unknown }>("move_opportunity", {
          opportunity_id: id,
          stage,
        });
        if (isApiError(res)) {
          setError(res.message ?? res.error);
          setRefresh((n) => n + 1);
        }
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : String(err));
        setRefresh((n) => n + 1);
      } finally {
        setMoving(null);
      }
    },
    [call],
  );

  if (error) {
    return (
      <Text size="sm" tone="danger" style={{ display: "block", padding: "1rem" }}>
        {error}
      </Text>
    );
  }
  if (rows === null) {
    return (
      <Inline gap="0.5rem" style={{ padding: "1rem" }}>
        <Spinner />
        <Text size="sm" tone="muted">
          Loading…
        </Text>
      </Inline>
    );
  }
  if (columns.length === 0) {
    return <EmptyState title="No stages" description="A workspace admin defines the pipeline under Settings." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      {truncated ? (
        <Text size="xs" tone="muted" style={{ display: "block", padding: "0.4rem 1rem 0" }}>
          Showing the first {MOST.toLocaleString()} deals by close date. Narrow the filter, or use the list.
        </Text>
      ) : null}
      {/* The columns sit on the raised ground and the cards on the page color, so
          a card reads as a thing that can be picked up. */}
      <div
        style={{
          display: "grid",
          gridAutoFlow: "column",
          gridAutoColumns: "minmax(240px, 1fr)",
          gap: "0.6rem",
          overflowX: "auto",
          padding: "0.6rem 1rem 1rem",
          background: tokens.bgRaised,
          borderTop: `1px solid ${tokens.border}`,
          flex: 1,
          minHeight: 420,
        }}
      >
        {columns.map((stage) => {
          const cards = byStage.get(stage.key) ?? [];
          const currency = cards.find((c) => typeof c.currency === "string")?.currency;
          const sum = cards.reduce((n, c) => {
            const amount = weighted ? c.weighted_value : c.value;
            return n + (typeof amount === "number" ? amount : 0);
          }, 0);
          const closed = stage.outcome !== "open";
          const top = stripe(stage);
          return (
            <div
              key={stage.key}
              onDragOver={(e) => {
                e.preventDefault();
                if (over !== stage.key) setOver(stage.key);
              }}
              onDragLeave={() => setOver((current) => (current === stage.key ? null : current))}
              onDrop={(e) => {
                e.preventDefault();
                setOver(null);
                // The drop ends the drag here: moving a card remounts it under its new
                // column, so the dragend on the old element never reaches React.
                setDragging(null);
                const id = e.dataTransfer.getData("text/plain") || dragging;
                if (id && (byStage.get(stage.key) ?? []).every((c) => c.id !== id)) void move(id, stage.key);
              }}
              style={{
                display: "flex",
                flexDirection: "column",
                minWidth: 0,
                borderRadius: tokens.radiusSm,
                border: `1px solid ${over === stage.key ? tokens.accent : "transparent"}`,
                background: over === stage.key ? tokens.infoLight : "transparent",
                opacity: closed && over !== stage.key ? 0.85 : 1,
              }}
            >
              <div style={{ height: 3, borderRadius: 2, background: top.color, opacity: top.opacity, margin: "0 0.25rem 0.5rem" }} />
              <div style={{ padding: "0 0.5rem 0.5rem" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.5rem" }}>
                  <Badge tone={outcomeTone(stage.outcome)}>{stage.label}</Badge>
                  <Text size="sm" tone="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
                    {cards.length}
                  </Text>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: "0.5rem", marginTop: "0.35rem" }}>
                  <Text size="xs" tone="faint">
                    {stage.probability}% default
                  </Text>
                  <Text size="sm" weight="semibold" style={{ fontVariantNumeric: "tabular-nums" }}>
                    {sum ? money(sum, typeof currency === "string" ? currency : null) : "–"}
                  </Text>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", padding: "0 0.25rem 0.25rem" }}>
                {cards.map((card) => {
                  const lines = dealLines(card.title, card["organization.name"]);
                  const due = dueState(card.close_date, !closed);
                  const own = typeof card.probability === "number" && card.probability !== stage.probability;
                  return (
                    <div
                      key={card.id}
                      role="button"
                      tabIndex={0}
                      draggable
                      aria-label={`${[lines.account, lines.title].filter((part, i, all) => part && all.indexOf(part) === i).join(", ")}, ${stage.label}`}
                      onDragStart={(e) => {
                        e.dataTransfer.setData("text/plain", card.id);
                        e.dataTransfer.effectAllowed = "move";
                        setDragging(card.id);
                      }}
                      onDragEnd={() => setDragging(null)}
                      onClick={() => onOpen(card.id)}
                      onKeyDown={(e) => e.key === "Enter" && onOpen(card.id)}
                      style={{
                        border: `1px solid ${tokens.border}`,
                        // A late deal shows it from across the room, not only in its chip.
                        borderLeft: `3px solid ${due.kind === "overdue" ? tokens.danger : due.kind === "soon" ? tokens.warning : tokens.border}`,
                        borderRadius: tokens.radiusSm,
                        background: tokens.bg,
                        boxShadow: tokens.shadowSm,
                        padding: "0.55rem 0.7rem",
                        cursor: "pointer",
                        opacity: dragging === card.id || moving === card.id ? 0.5 : 1,
                      }}
                    >
                      <Text size="sm" weight="medium" truncate>
                        {lines.account ?? lines.title}
                      </Text>
                      {lines.account && lines.title !== lines.account ? (
                        <Text size="xs" tone="muted" truncate>
                          {lines.title}
                        </Text>
                      ) : null}
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.5rem", marginTop: "0.4rem" }}>
                        <Text size="sm" weight="semibold" tone={typeof card.value === "number" ? undefined : "faint"} style={{ fontVariantNumeric: "tabular-nums" }}>
                          {typeof card.value === "number" ? money(card.value, typeof card.currency === "string" ? card.currency : null) : "–"}
                        </Text>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: "0.5rem" }}>
                          {own ? (
                            <Text size="xs" tone="muted" title="This deal's own probability" style={{ fontVariantNumeric: "tabular-nums" }}>
                              {String(card.probability)}%
                            </Text>
                          ) : null}
                          {typeof card.close_date === "string" ? <DueChip closeDate={card.close_date} open={!closed} /> : null}
                        </span>
                      </div>
                    </div>
                  );
                })}
                {cards.length === 0 ? (
                  <Text size="xs" tone="faint" style={{ padding: "0.5rem 0.45rem" }}>
                    {over === stage.key ? "Drop to move here" : "Nothing here"}
                  </Text>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
