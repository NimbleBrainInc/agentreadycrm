import { useEffect, useState } from "react";
import { ListRow, Text } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { fmtMoney } from "../format";
import { type ApiError, type GetPipelineResult, isApiError, type OpenDeal } from "../types";

/** A deal's value, which arrives as a number or a decimal string; `null` when unset or unreadable. */
function amount(value: OpenDeal["value"]): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * A record's open deals as its read carries them (`open_opportunities`, the newest few), each
 * opening in place, and how many more are open beyond them. Stage keys read as the pipeline's
 * labels once it answers, and as their keys until then or if it does not.
 */
export function OpenDeals({
  deals,
  total,
  onOpen,
}: {
  deals: OpenDeal[];
  total: number;
  onOpen: (id: string) => void;
}) {
  const call = useCall();
  const [labels, setLabels] = useState<Record<string, string>>({});
  useEffect(() => {
    if (deals.length === 0) return;
    let stale = false;
    void call<GetPipelineResult | ApiError>("get_pipeline", { include_retired: true }).then((res) => {
      if (stale || isApiError(res)) return;
      setLabels(Object.fromEntries(res.stages.map((s) => [s.key, s.label])));
    });
    return () => {
      stale = true;
    };
  }, [call, deals.length]);

  if (deals.length === 0) {
    return (
      <Text size="sm" tone="faint">
        No open deals.
      </Text>
    );
  }
  const more = total - deals.length;
  return (
    <div data-deals="open">
      {deals.map((deal) => {
        const value = amount(deal.value);
        return (
          <ListRow
            key={deal.id}
            interactive
            onClick={() => onOpen(deal.id)}
            title={deal.title}
            meta={labels[deal.stage] ?? deal.stage}
            trailing={value === null ? undefined : <Text size="sm">{fmtMoney(value, deal.currency)}</Text>}
          />
        );
      })}
      {more > 0 ? (
        <Text size="xs" tone="faint" style={{ display: "block", marginTop: "0.35rem" }}>
          {more} more open {more === 1 ? "deal" : "deals"}
        </Text>
      ) : null}
    </div>
  );
}
