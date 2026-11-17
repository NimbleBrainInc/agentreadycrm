import type { Row } from "../types";
import type { FieldSpec } from "./schema";

/**
 * The visible columns in order, each followed by the value its cell draws
 * beside it (the title under a name, the status dot on an address), so the
 * file holds what the screen showed.
 */
export function exportColumns(
  visible: FieldSpec[],
  schema: FieldSpec[],
  companions: Record<string, string>,
): FieldSpec[] {
  const byPath = new Map(schema.map((s) => [s.path, s]));
  const shown = new Set(visible.map((s) => s.path));
  const out: FieldSpec[] = [];
  for (const spec of visible) {
    out.push(spec);
    const companion = byPath.get(companions[spec.path] ?? "");
    if (companion && !shown.has(companion.path)) {
      out.push(companion);
      shown.add(companion.path);
    }
  }
  return out;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "";
  let text = Array.isArray(value) ? value.join("; ") : String(value);
  // A spreadsheet runs a cell that opens with one of these as a formula, and a
  // field value is a string an import or an enrichment run wrote. A number is
  // left alone so a negative stays a number.
  if (typeof value !== "number" && /^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The rows as CSV under the columns' labels, with a byte-order mark so a spreadsheet reads it as UTF-8. */
export function toCsv(columns: FieldSpec[], rows: Row[]): string {
  const lines = [columns.map((spec) => cell(spec.label)).join(",")];
  for (const row of rows) lines.push(columns.map((spec) => cell(row[spec.path])).join(","));
  return `﻿${lines.join("\r\n")}\r\n`;
}
