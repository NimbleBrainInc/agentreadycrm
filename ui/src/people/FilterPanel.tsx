import { useEffect, useState } from "react";
import { Text, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { type ApiError, type CountRecordsResult, isApiError } from "../types";
import { CustomMark } from "./cells";
import { type Clause, type FilterEntry, type FilterState, PRESETS, toFilter } from "./filters";
import { type Entity, type FieldSpec, GROUPS } from "./schema";

const inputStyle = {
  width: "100%",
  boxSizing: "border-box" as const,
  font: "inherit",
  fontSize: tokens.textSmSize,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusXs,
  padding: "0.2rem 0.45rem",
};

function Option({
  label,
  count,
  pressed,
  onToggle,
}: {
  label: string;
  count?: number;
  pressed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onToggle}
      style={{
        display: "inline-flex",
        alignItems: "baseline",
        gap: "0.4rem",
        font: "inherit",
        fontSize: tokens.textSmSize,
        padding: "0.1rem 0.5rem",
        borderRadius: tokens.radiusXs,
        cursor: "pointer",
        color: pressed ? tokens.accent : tokens.fg,
        background: pressed ? tokens.infoLight : tokens.bg,
        border: `1px solid ${pressed ? tokens.accent : tokens.border}`,
        opacity: count === 0 && !pressed ? 0.5 : 1,
      }}
    >
      {label}
      {count !== undefined ? (
        <span
          style={{
            fontSize: tokens.textXsSize,
            color: pressed ? tokens.accent : tokens.fgFaint,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {count.toLocaleString()}
        </span>
      ) : null}
    </button>
  );
}

/** A field's values as toggles, each with how many rows hold it under the other filters. */
function ValueFacet({
  entity,
  spec,
  entry,
  scope,
  onChange,
}: {
  entity: Entity;
  spec: FieldSpec;
  entry: FilterEntry | undefined;
  /** Every other active filter: what this field's counts are taken under. */
  scope: Clause | undefined;
  onChange: (entry: FilterEntry | undefined) => void;
}) {
  const call = useCall();
  const [counts, setCounts] = useState<Map<string, number> | null>(null);
  const scopeKey = JSON.stringify(scope ?? null);

  useEffect(() => {
    let stale = false;
    void call<CountRecordsResult | ApiError>("count_records", {
      entity,
      field: spec.path,
      filter: scope,
      limit: 12,
    }).then((res) => {
      if (stale || isApiError(res)) return;
      setCounts(new Map(res.values.filter((v) => v.value !== null).map((v) => [String(v.value), v.count])));
    });
    return () => {
      stale = true;
    };
    // `scopeKey` stands in for `scope`, which is a fresh object every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call, entity, spec.path, scopeKey]);

  const selected = entry?.t === "values" ? entry.values : [];
  const isBool = spec.kind === "boolean";
  const options: { value: string | boolean; label: string }[] = isBool
    ? [
        { value: true, label: "Yes" },
        { value: false, label: "No" },
      ]
    : (spec.options ?? [...(counts?.keys() ?? [])]).map((o) => ({
        value: o,
        label: o,
      }));

  const toggle = (value: string | boolean) => {
    const next = selected.includes(value)
      ? selected.filter((v) => v !== value)
      : isBool
        ? [value]
        : [...selected, value];
    onChange(next.length ? { t: "values", values: next } : undefined);
  };

  if (options.length === 0) {
    return (
      <Text size="xs" tone="faint">
        {counts ? "No values yet" : "…"}
      </Text>
    );
  }
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.25rem" }}>
      {options.map((o) => (
        <Option
          key={String(o.value)}
          label={o.label}
          count={counts ? (counts.get(String(o.value)) ?? 0) : undefined}
          pressed={selected.includes(o.value)}
          onToggle={() => toggle(o.value)}
        />
      ))}
    </div>
  );
}

function PresetFacet({
  spec,
  entry,
  onChange,
}: {
  spec: FieldSpec;
  entry: FilterEntry | undefined;
  onChange: (entry: FilterEntry | undefined) => void;
}) {
  const current = entry?.t === "preset" ? entry.id : null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.25rem" }}>
      {PRESETS[spec.path].map((p) => (
        <Option
          key={p.id}
          label={p.label}
          pressed={current === p.id}
          onToggle={() => onChange(current === p.id ? undefined : { t: "preset", id: p.id })}
        />
      ))}
    </div>
  );
}

/** An input that commits on Enter or blur, so a filter is not re-run per keystroke. */
function Committed({
  id,
  value,
  placeholder,
  type = "text",
  onCommit,
}: {
  id: string;
  value: string;
  placeholder?: string;
  type?: string;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      id={id}
      type={type}
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft !== value && onCommit(draft)}
      onKeyDown={(e) => e.key === "Enter" && onCommit(draft)}
      style={inputStyle}
    />
  );
}

function RangeFacet({
  spec,
  entry,
  onChange,
}: {
  spec: FieldSpec;
  entry: FilterEntry | undefined;
  onChange: (entry: FilterEntry | undefined) => void;
}) {
  const isNumber = spec.kind === "number";
  const low = entry?.t === "range" ? entry.min : entry?.t === "dates" ? entry.from : undefined;
  const high = entry?.t === "range" ? entry.max : entry?.t === "dates" ? entry.to : undefined;
  const commit = (side: "low" | "high", raw: string) => {
    const next = {
      low: low as number | string | undefined,
      high: high as number | string | undefined,
    };
    const parsed = raw.trim() === "" ? undefined : isNumber ? Number(raw) : raw;
    if (typeof parsed === "number" && Number.isNaN(parsed)) return;
    next[side] = parsed;
    if (next.low === undefined && next.high === undefined) return onChange(undefined);
    onChange(
      isNumber
        ? {
            t: "range",
            min: next.low as number | undefined,
            max: next.high as number | undefined,
          }
        : {
            t: "dates",
            from: next.low as string | undefined,
            to: next.high as string | undefined,
          },
    );
  };
  const type = isNumber ? "number" : "date";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr auto 1fr",
        gap: "0.35rem",
        alignItems: "center",
      }}
    >
      <Committed
        id={`f-${spec.path}-low`}
        type={type}
        placeholder="Min"
        value={low === undefined ? "" : String(low)}
        onCommit={(v) => commit("low", v)}
      />
      <Text size="xs" tone="faint">
        to
      </Text>
      <Committed
        id={`f-${spec.path}-high`}
        type={type}
        placeholder="Max"
        value={high === undefined ? "" : String(high)}
        onCommit={(v) => commit("high", v)}
      />
    </div>
  );
}

function control(spec: FieldSpec): "values" | "preset" | "range" | "contains" | null {
  if (PRESETS[spec.path]) return "preset";
  if (spec.facet) return "values";
  if (spec.kind === "number" || spec.kind === "date" || spec.kind === "timestamp") return "range";
  // Free text earns a box where people segment by it: the title, and custom text.
  if (spec.kind === "text" && (spec.path === "title" || spec.custom)) return "contains";
  return null;
}

// How many fields a group shows before the rest fold behind "more". The fields a
// workspace defined come first: they are the ones it segments by.
const SHOWN_PER_GROUP = 4;
const RANK: Record<string, number> = {
  values: 0,
  preset: 0,
  contains: 1,
  range: 2,
};

function ordered(specs: FieldSpec[]): FieldSpec[] {
  const rank = (s: FieldSpec) => (s.custom && s.facet ? -1 : (RANK[control(s) ?? "range"] ?? 2));
  return [...specs].sort((a, b) => rank(a) - rank(b));
}

/** Whether the panel offers a control for this field. */
export function filterable(spec: FieldSpec): boolean {
  return control(spec) !== null;
}

/**
 * Every filterable field, grouped by what it describes. Opens above the list
 * and stays collapsed otherwise; the chips beside the button say what is applied.
 */
export function FilterPanel({
  entity,
  schema,
  state,
  search,
  searchPaths,
  onChange,
}: {
  entity: Entity;
  schema: FieldSpec[];
  state: FilterState;
  search: string;
  searchPaths: string[];
  onChange: (path: string, entry: FilterEntry | undefined) => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
        gap: "1rem 1.75rem",
        padding: "0.9rem 1rem 1rem",
        background: tokens.bgRaised,
        borderTop: `1px solid ${tokens.border}`,
        borderBottom: `1px solid ${tokens.border}`,
      }}
    >
      {GROUPS.map((group) => {
        const all = ordered(schema.filter((s) => s.group === group && filterable(s)));
        if (all.length === 0) return null;
        // A folded field with a filter applied stays in view: hiding it would hide
        // why the list is short.
        const specs = expanded[group]
          ? all
          : all.filter((s, index) => index < SHOWN_PER_GROUP || state[s.path] !== undefined);
        const folded = all.length - specs.length;
        return (
          <section key={group} style={{ minWidth: 0 }}>
            <div
              style={{
                fontSize: "0.6875rem",
                fontWeight: 600,
                letterSpacing: "0.07em",
                textTransform: "uppercase",
                color: tokens.fgFaint,
                marginBottom: "0.5rem",
              }}
            >
              {group}
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.65rem",
              }}
            >
              {specs.map((spec) => {
                const kind = control(spec);
                const entry = state[spec.path];
                const set = (next: FilterEntry | undefined) => onChange(spec.path, next);
                return (
                  <div key={spec.path}>
                    <label
                      htmlFor={kind === "contains" ? `f-${spec.path}` : undefined}
                      title={spec.description}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "0.4rem",
                        marginBottom: "0.25rem",
                        fontSize: tokens.textSmSize,
                        color: tokens.fgMuted,
                      }}
                    >
                      {spec.custom ? <CustomMark /> : null}
                      {spec.label}
                    </label>
                    {kind === "values" ? (
                      <ValueFacet
                        entity={entity}
                        spec={spec}
                        entry={entry}
                        scope={toFilter(state, schema, search, searchPaths, spec.path)}
                        onChange={set}
                      />
                    ) : kind === "preset" ? (
                      <PresetFacet spec={spec} entry={entry} onChange={set} />
                    ) : kind === "range" ? (
                      <RangeFacet spec={spec} entry={entry} onChange={set} />
                    ) : (
                      <Committed
                        id={`f-${spec.path}`}
                        placeholder="contains…"
                        value={entry?.t === "contains" ? entry.text : ""}
                        onCommit={(text) => set(text.trim() ? { t: "contains", text } : undefined)}
                      />
                    )}
                  </div>
                );
              })}
              {folded > 0 || expanded[group] ? (
                <button
                  type="button"
                  onClick={() =>
                    setExpanded((current) => ({
                      ...current,
                      [group]: !current[group],
                    }))
                  }
                  style={{
                    all: "unset",
                    cursor: "pointer",
                    fontSize: tokens.textXsSize,
                    color: tokens.fgMuted,
                  }}
                >
                  {expanded[group] ? "Fewer" : `${folded} more`}
                </button>
              ) : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}
