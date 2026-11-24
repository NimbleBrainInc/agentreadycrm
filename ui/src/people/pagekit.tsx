import { type ReactNode, useMemo, useState } from "react";
import { SegmentedControl, Text, TextLink, tokens } from "@nimblebrain/synapse/ui";
import { FieldValueForm, type RecordKind } from "../components/forms";
import { fmtDate } from "../format";
import type { FieldDef, FieldDetails } from "../types";
import { CustomMark } from "./cells";
import { NewFieldForm } from "./FieldsView";

// A record page: a column of cards holding label/value lines, with a custom
// field's provenance one click away from its value, beside the record's timeline.

export const sectionTitle = {
  fontSize: "0.6875rem",
  fontWeight: 600,
  letterSpacing: "0.07em",
  textTransform: "uppercase" as const,
  color: tokens.fgFaint,
};

export function show(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function provenance(details: FieldDetails | undefined): string {
  if (!details) return "";
  return [details.by ? `Set by ${details.by}` : "Set", details.at ? fmtDate(details.at) : null]
    .filter(Boolean)
    .join(" · ");
}

/**
 * A label and its value. A value that carries provenance opens to show who set
 * it, when, and why: the reason is one click away from the value it explains,
 * and out of the way until asked for.
 */
export function FieldLine({
  label,
  custom,
  details,
  children,
}: {
  label: string;
  custom?: boolean;
  details?: FieldDetails;
  children: ReactNode;
}) {
  const row = (
    <span style={{ display: "grid", gridTemplateColumns: "110px minmax(0, 1fr)", gap: "0.6rem", padding: "0.2rem 0", fontSize: tokens.textSmSize }}>
      <span style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: tokens.fgMuted }}>
        {custom ? <CustomMark /> : null}
        {label}
      </span>
      <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{children}</span>
    </span>
  );
  if (!details) return row;
  return (
    <details>
      <summary style={{ listStyle: "none", cursor: "pointer" }}>{row}</summary>
      <div style={{ margin: "0 0 0.5rem 120px", paddingLeft: "0.5rem", borderLeft: `2px solid ${tokens.border}`, fontSize: tokens.textXsSize, color: tokens.fgMuted }}>
        {provenance(details)}
        {details.why ? <div style={{ color: tokens.fg, marginTop: "0.15rem" }}>{details.why}</div> : null}
      </div>
    </details>
  );
}

/** A card in a record page's column: a titled box, with an optional count and action. */
export function Section({
  title,
  count,
  action,
  children,
}: {
  title: string;
  count?: number;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section style={{ background: tokens.bg, border: `1px solid ${tokens.border}`, borderRadius: tokens.radiusMd }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", padding: "0.55rem 0.85rem", borderBottom: `1px solid ${tokens.border}` }}>
        <span style={sectionTitle}>{title}</span>
        {count !== undefined ? <span style={{ ...sectionTitle, color: tokens.fgMuted }}>{count}</span> : null}
        <span style={{ flex: 1 }} />
        {action}
      </div>
      <div style={{ padding: "0.55rem 0.85rem" }}>{children}</div>
    </section>
  );
}

/** How many set fields a card shows before "Show more". */
const FIELDS_SHOWN = 5;

/** A custom field's value, or a dash for none. */
function FieldValue({ value }: { value: unknown }) {
  if (value === undefined) {
    return (
      <Text size="sm" tone="faint">
        –
      </Text>
    );
  }
  return <>{show(value)}</>;
}

/**
 * Every custom field defined for an entity, set or not, so an empty one is visible
 * as empty. The first few set fields show; the rest, and the empty ones, are one
 * click away.
 *
 * Given `edit`, the record is the one the card belongs to: each value opens in place to be
 * set or cleared, and the card offers to define a new field for its kind. The server decides
 * both writes, and its refusal (a value the field does not take, an account at its field
 * limit, a viewer the host keeps from defining fields) shows as it words it.
 */
export function CustomFields({
  defs,
  values,
  details,
  edit,
}: {
  defs: FieldDef[];
  values: Record<string, unknown> | undefined;
  details: Record<string, FieldDetails> | undefined;
  edit?: { kind: RecordKind; id: string };
}) {
  const [showAll, setShowAll] = useState(false);
  const [showEmpty, setShowEmpty] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const set = defs.filter((d) => values?.[d.key] !== undefined);
  const empty = defs.filter((d) => values?.[d.key] === undefined);
  const shown = showAll ? set : set.slice(0, FIELDS_SHOWN);
  const hidden = set.length - shown.length;

  const line = (d: FieldDef, withDetails: boolean) => {
    if (edit && editing === d.key) {
      return (
        <FieldValueForm key={d.key} kind={edit.kind} id={edit.id} def={d} value={values?.[d.key]} onClose={() => setEditing(null)} />
      );
    }
    const value = <FieldValue value={values?.[d.key]} />;
    return (
      <FieldLine key={d.key} label={d.label} custom details={withDetails ? (details?.[d.key] ?? {}) : undefined}>
        {edit ? (
          <button
            type="button"
            aria-label={`Edit ${d.label}`}
            title={`Edit ${d.label}`}
            onClick={(e) => {
              // The value sits in the line that opens its provenance; editing opens neither.
              e.preventDefault();
              e.stopPropagation();
              setEditing(d.key);
            }}
            style={{ all: "unset", cursor: "text", display: "block", width: "100%", minHeight: "1.2em", overflowWrap: "anywhere" }}
          >
            {value}
          </button>
        ) : (
          value
        )}
      </FieldLine>
    );
  };

  return (
    <>
      {shown.map((d) => line(d, true))}
      {showEmpty ? empty.map((d) => line(d, false)) : null}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.9rem", marginTop: "0.2rem" }}>
        {set.length > FIELDS_SHOWN ? (
          <TextLink onClick={() => setShowAll((s) => !s)} style={{ fontSize: tokens.textXsSize }}>
            {showAll ? "Show fewer" : `Show ${hidden} more`}
          </TextLink>
        ) : null}
        {empty.length ? (
          <TextLink onClick={() => setShowEmpty((s) => !s)} style={{ fontSize: tokens.textXsSize }}>
            {showEmpty ? "Hide empty fields" : `${empty.length} empty ${empty.length === 1 ? "field" : "fields"}`}
          </TextLink>
        ) : null}
        {edit ? (
          <TextLink onClick={() => setAdding(true)} style={{ fontSize: tokens.textXsSize }}>
            Add field
          </TextLink>
        ) : null}
      </div>
      {defs.length === 0 && !edit ? (
        <Text size="sm" tone="faint">
          No custom fields defined.
        </Text>
      ) : null}
      {adding && edit ? <NewFieldForm entity={edit.kind} onClose={() => setAdding(false)} /> : null}
    </>
  );
}

/** One entry on a record's timeline. A field change names who made it, so a run's
 * changes read as one entry until opened, and why, which stays with that one field. */
export interface TimelineEvent {
  key: string;
  lane: string;
  at: string;
  glyph: string;
  title: string;
  summary?: string;
  detail?: ReactNode;
  change?: { by: string; why?: string };
}

/** The lane of field changes, shown one by one under History. */
export const HISTORY = "History";
const ACTIVITY = "Activity";

/**
 * Field changes, folded: the changes one writer made on one day become one entry
 * ("<writer> updated 10 fields") that opens to the list. A single change stays as it is.
 */
export function foldChanges(events: TimelineEvent[]): TimelineEvent[] {
  const runs = new Map<string, TimelineEvent[]>();
  const out: TimelineEvent[] = [];
  for (const e of events) {
    if (!e.change) {
      out.push(e);
      continue;
    }
    const run = `${e.change.by}\u0000${e.at.slice(0, 10)}`;
    const held = runs.get(run);
    if (held) held.push(e);
    else runs.set(run, [e]);
  }
  for (const [run, changes] of runs) {
    if (changes.length === 1) {
      out.push(changes[0]);
      continue;
    }
    const latest = changes.reduce((a, b) => (a.at > b.at ? a : b));
    out.push({
      key: `run:${run}`,
      lane: HISTORY,
      at: latest.at,
      glyph: "✎",
      title: `${changes[0].change?.by || "Someone"} updated ${changes.length} fields`,
      detail: (
        <ul style={{ margin: 0, paddingLeft: "1rem" }}>
          {changes.map((c) => (
            <li key={c.key}>
              {c.title}
              {c.change?.why ? ` · ${c.change.why}` : null}
            </li>
          ))}
        </ul>
      ),
    });
  }
  return out.sort((a, b) => b.at.localeCompare(a.at));
}

function TimelineRow({ event: e }: { event: TimelineEvent }) {
  const head = (
    <span style={{ display: "grid", gridTemplateColumns: "88px 18px minmax(0, 1fr)", gap: "0.5rem", alignItems: "baseline", padding: "0.6rem 0" }}>
      <Text size="xs" tone="faint" style={{ fontVariantNumeric: "tabular-nums" }}>
        {fmtDate(e.at)}
      </Text>
      <span aria-hidden style={{ color: tokens.fgFaint, fontSize: tokens.textXsSize, textAlign: "center" }}>
        {e.glyph}
      </span>
      <span style={{ minWidth: 0 }}>
        <Text size="sm" weight="medium">
          {e.title}
        </Text>
        {e.summary ? (
          <Text size="sm" tone="muted" style={{ display: "block" }}>
            {e.summary}
          </Text>
        ) : null}
      </span>
    </span>
  );
  return (
    <div style={{ borderBottom: `1px solid ${tokens.border}` }}>
      {e.detail ? (
        <details>
          <summary style={{ listStyle: "none", cursor: "pointer" }}>{head}</summary>
          <div style={{ padding: "0 0 0.9rem 114px", fontSize: tokens.textSmSize, color: tokens.fgMuted, overflowWrap: "anywhere" }}>{e.detail}</div>
        </details>
      ) : (
        head
      )}
    </div>
  );
}

/**
 * A record's history. Activity is everything, with field changes folded by run;
 * each of ``lanes`` filters to its own entries; History lists every field change.
 */
export function RecordTimeline({ events, lanes }: { events: TimelineEvent[]; lanes: string[] }) {
  const [lane, setLane] = useState(ACTIVITY);
  const activity = useMemo(() => foldChanges(events), [events]);
  const tabs = [ACTIVITY, ...lanes, HISTORY];
  const count = (l: string) => (l === ACTIVITY ? activity.length : events.filter((e) => e.lane === l).length);
  const shown = lane === ACTIVITY ? activity : events.filter((e) => e.lane === lane);
  return (
    <>
      <SegmentedControl options={tabs.map((l) => ({ label: `${l} ${count(l)}`, value: l }))} value={lane} onChange={setLane} />
      <div style={{ marginTop: "0.6rem" }}>
        {shown.length === 0 ? (
          <Text size="sm" tone="faint" style={{ display: "block", padding: "1.5rem 0" }}>
            Nothing here yet.
          </Text>
        ) : null}
        {shown.map((e) => (
          <TimelineRow key={e.key} event={e} />
        ))}
      </div>
    </>
  );
}

/** A record page's body: the cards in a column, the timeline beside them. */
export function RecordLayout({ narrow, cards, timeline }: { narrow: boolean; cards: ReactNode; timeline: ReactNode }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: narrow ? "1fr" : "360px minmax(0, 1fr)" }}>
      <aside
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          padding: "1rem",
          minWidth: 0,
          background: tokens.bgSubtle,
          borderRight: narrow ? undefined : `1px solid ${tokens.border}`,
          borderBottom: narrow ? `1px solid ${tokens.border}` : undefined,
        }}
      >
        {cards}
      </aside>
      <div style={{ padding: "1rem 1.25rem 2rem", minWidth: 0 }}>{timeline}</div>
    </div>
  );
}
