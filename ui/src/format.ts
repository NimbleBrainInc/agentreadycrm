import DOMPurify from "dompurify";
import { marked } from "marked";
import type { BadgeTone } from "@nimblebrain/synapse/ui";

/**
 * Render an interaction body's markdown to sanitized HTML.
 *
 * Bodies carry agent-written text over prospect-controlled sources (an email
 * received, a page read), so the markdown→HTML result is run through DOMPurify
 * before it reaches `dangerouslySetInnerHTML` — stripping `<script>`, inline
 * event handlers, and other injection vectors. The host iframe's CSP allows
 * `unsafe-inline` (required by the single-file bundle), so sanitizing here is
 * the load-bearing defense, not the CSP.
 */
export function markdownToHtml(md: string): string {
  const html = marked.parse(md, { async: false }) as string;
  return DOMPurify.sanitize(html);
}

/**
 * A `YYYY-MM-DD` value is a calendar day with no zone, so it is read as the
 * viewer's local day; `new Date("2026-10-12")` would be UTC midnight, which is
 * the evening before west of Greenwich.
 */
export function localDay(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

/** "Jun 19, 2026" — date only. A date-only value is the viewer's local day. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = localDay(iso) ?? new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** "Jun 19, 2026, 2:30 PM" — date + time, for interaction timestamps. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** A `Badge` tone for an opportunity outcome (won / lost / open). */
export function outcomeTone(outcome: string): "success" | "neutral" | "accent" {
  switch (outcome) {
    case "won":
      return "success";
    case "lost":
      return "neutral";
    default:
      return "accent"; // open
  }
}

/** "$5,000" — a deal value with its currency (defaults to USD). */
export function fmtMoney(
  value: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (value === null || value === undefined) return "";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    return String(value);
  }
}

/** A `Badge` tone for a contact lifecycle status. */
export function statusTone(status: string): BadgeTone {
  switch (status) {
    case "engaged":
      return "success";
    case "contacted":
      return "accent";
    case "enriched":
      return "processing";
    default:
      // "sourced" (the default first stage) and anything else: nothing has happened to
      // the contact yet, and the badge renders the status word itself, so the tone
      // carries emphasis, not identity. The alarm tones are the wrong read for a contact
      // that is merely untouched.
      return "neutral";
  }
}
