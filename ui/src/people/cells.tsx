import { Badge, StatusDot, Text, tokens } from "@nimblebrain/synapse/ui";
import { fmtDate, fmtMoney, localDay, outcomeTone, statusTone } from "../format";
import type { Row } from "../types";
import type { FieldSpec } from "./schema";

const EMAIL_COLOR: Record<string, string> = {
  valid: tokens.success,
  invalid: tokens.danger,
  risky: tokens.warning,
};

/** "3 days ago", for a column where recency is what is being read. */
export function ago(iso: unknown): string {
  if (typeof iso !== "string") return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  if (days < 730) return `${Math.round(days / 30)} months ago`;
  return `${Math.round(days / 365)} years ago`;
}

const Empty = () => (
  <Text size="sm" tone="faint">
    –
  </Text>
);

/** The address with a dot for whether it can receive mail; hollow when unchecked. */
export function EmailValue({ email, status }: { email: unknown; status: unknown }) {
  if (typeof email !== "string" || !email) return <Empty />;
  const color = typeof status === "string" ? EMAIL_COLOR[status] : undefined;
  return (
    <span
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.45rem",
        minWidth: 0,
      }}
    >
      <StatusDot
        size={7}
        color={color ?? "transparent"}
        title={typeof status === "string" ? `Email ${status}` : "Email not checked"}
        style={color ? undefined : { boxShadow: `inset 0 0 0 1.5px ${tokens.fgFaint}` }}
      />
      <Text size="sm" truncate>
        {email}
      </Text>
    </span>
  );
}

function TwoLine({ primary, secondary }: { primary: unknown; secondary: unknown }) {
  if (typeof primary !== "string" || !primary) return <Empty />;
  return (
    <span style={{ display: "block", minWidth: 0 }}>
      <Text size="sm" weight="medium" truncate>
        {primary}
      </Text>
      {typeof secondary === "string" && secondary ? (
        <Text size="xs" tone="muted" truncate>
          {secondary}
        </Text>
      ) : null}
    </span>
  );
}

/**
 * A deal's two lines without saying the account twice. A title that opens with
 * the account's name ("Larkfield · pilot") loses that prefix, so the account is
 * read once from its own line; a title that is only the account's name leaves
 * the second line empty.
 */
export function dealLines(title: unknown, account: unknown): { title: string; account: string | null } {
  const full = typeof title === "string" ? title : "";
  const name = typeof account === "string" && account ? account : null;
  if (!name) return { title: full, account: null };
  const prefixed = full.toLowerCase().startsWith(name.toLowerCase());
  const after = full.slice(name.length);
  // Only a whole word: "Acmeville renewal" under "Acme" keeps its name.
  const bounded = after === "" || /^[\s·:\-–—|,]/.test(after);
  const rest = prefixed && bounded ? after.replace(/^[\s·:\-–—|,]+/, "") : full;
  return { title: rest || full, account: name };
}

/**
 * How a close date reads against today for a deal that is still open: past is
 * overdue, within a week is soon, the rest is just a date. A closed deal's date
 * is history and gets no urgency.
 */
export function dueState(
  closeDate: unknown,
  open: boolean,
): { kind: "overdue" | "soon" | "later" | "none"; label: string } {
  if (typeof closeDate !== "string" || !closeDate) return { kind: "none", label: "" };
  const date = fmtDate(closeDate);
  if (!open) return { kind: "later", label: date };
  // Both ends are local calendar days: the deal closes on a day, and the
  // viewer is in a day, wherever each of them is.
  const day = 86_400_000;
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const close = localDay(closeDate);
  if (!close) return { kind: "later", label: date };
  const days = Math.round((close.getTime() - start) / day);
  if (days < 0) {
    const late = -days;
    const span = late >= 30 ? `${Math.round(late / 30)} mo` : late >= 7 ? `${Math.round(late / 7)} wk` : `${late} d`;
    return { kind: "overdue", label: `Overdue ${span}` };
  }
  if (days === 0) return { kind: "soon", label: "Due today" };
  if (days <= 7) return { kind: "soon", label: `Due in ${days} d` };
  return { kind: "later", label: date };
}

/** The close date as a chip when it needs attention, plain text when it does not. */
export function DueChip({ closeDate, open }: { closeDate: unknown; open: boolean }) {
  const due = dueState(closeDate, open);
  if (due.kind === "none") return <Empty />;
  if (due.kind === "overdue") return <Badge tone="danger">⚠ {due.label}</Badge>;
  if (due.kind === "soon") return <Badge tone="warning">● {due.label}</Badge>;
  return (
    <Text size="xs" tone="faint" style={{ fontVariantNumeric: "tabular-nums" }}>
      {due.label}
    </Text>
  );
}

/** How the stage keys read, from `get_pipeline`; a key with no entry shows as itself. */
export type StageLabels = Record<string, { label: string; outcome: string }>;

/**
 * One cell of a list. `companions` names the value a cell draws beside its own
 * (the entity config's, which the export also reads). Values are untrusted
 * strings and render as text only.
 */
export function Cell({
  spec,
  row,
  companions,
  stages,
}: {
  spec: FieldSpec;
  row: Row;
  companions: Record<string, string>;
  /** How a deal's stage keys read, from `get_pipeline`. */
  stages?: StageLabels;
}) {
  const value = row[spec.path];
  const companion = spec.path in companions ? row[companions[spec.path]] : undefined;
  if (spec.path === "email") return <EmailValue email={value} status={companion} />;
  if (spec.path === "contact.email") return <EmailValue email={value} status={row["contact.email_status"]} />;
  if (spec.path === "title" && "stage" in row) {
    const lines = dealLines(value, companion);
    return <TwoLine primary={lines.title} secondary={lines.account} />;
  }
  if (spec.path in companions) return <TwoLine primary={value} secondary={companion} />;
  if (value === null || value === undefined || value === "") return <Empty />;
  if (spec.path === "status") return <Badge tone={statusTone(String(value))}>{String(value)}</Badge>;
  if (spec.path === "stage") {
    const stage = stages?.[String(value)];
    return <Badge tone={outcomeTone(stage?.outcome ?? "open")}>{stage?.label ?? String(value)}</Badge>;
  }
  if (spec.path === "outcome") return <Badge tone={outcomeTone(String(value))}>{String(value)}</Badge>;
  if (spec.path === "value" || spec.path === "weighted_value") {
    const currency = typeof row.currency === "string" ? row.currency : null;
    return (
      <Text size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>
        {fmtMoney(Number(value), currency)}
      </Text>
    );
  }
  if (spec.path === "probability") {
    return (
      <Text size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>
        {String(value)}%
      </Text>
    );
  }
  if (spec.path === "signals.active" || spec.path === "contacts.count") {
    if (value === 0) return <Empty />;
    if (spec.path === "contacts.count") return <Text size="sm">{String(value)}</Text>;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "0.45rem" }}>
        <StatusDot size={7} color={tokens.processing} />
        <Text size="sm">{String(value)}</Text>
      </span>
    );
  }
  if (spec.kind === "timestamp") {
    return (
      <Text size="sm" title={String(value)}>
        {ago(value)}
      </Text>
    );
  }
  if (spec.path === "close_date") return <DueChip closeDate={value} open={row.outcome === "open"} />;
  if (spec.kind === "date") return <Text size="sm">{fmtDate(String(value))}</Text>;
  if (spec.kind === "boolean") return <Text size="sm">{value ? "Yes" : "No"}</Text>;
  if (Array.isArray(value)) {
    return (
      <Text size="sm" truncate>
        {value.join(", ")}
      </Text>
    );
  }
  return (
    <Text size="sm" truncate style={spec.kind === "number" ? { fontVariantNumeric: "tabular-nums" } : undefined}>
      {String(value)}
    </Text>
  );
}

/** The small diamond that marks a workspace-defined field wherever fields are listed. */
export function CustomMark() {
  return (
    <span
      title="Custom field"
      aria-label="Custom field"
      style={{
        display: "inline-block",
        width: 5,
        height: 5,
        background: tokens.processing,
        transform: "rotate(45deg)",
        flex: "none",
      }}
    />
  );
}
