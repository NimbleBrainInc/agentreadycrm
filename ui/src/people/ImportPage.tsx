import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { downloadFile } from "@nimblebrain/synapse";
import { useApp } from "@nimblebrain/synapse/react";
import { Button, Heading, Inline, Stack, Text, TextLink, tokens } from "@nimblebrain/synapse/ui";
import { readSheet } from "read-excel-file/browser";
import { useCall } from "../api";
import { type ApiError, type DescribeFieldsResult, isApiError } from "../types";
import {
  batches,
  CORE_TARGETS,
  type FilterOp,
  guessMapping,
  type ImportEntity,
  type ImportResult,
  importTargets,
  type Outcome,
  outcomes,
  outcomesCsv,
  parseCsv,
  plan,
  REF_TARGET,
  REQUIRED,
  type RowFilter,
  type Table,
  type Target,
  targetId,
  targetLabel,
  toTable,
  validRefPrefix,
} from "./importFile";
import { buildSchema, ENTITIES } from "./schema";

const TOOL: Record<ImportEntity, string> = {
  organization: "import_organizations",
  contact: "import_contacts",
};

const OPS: FilterOp[] = ["is", "contains", "is not empty", "is empty"];

const control: CSSProperties = {
  font: "inherit",
  fontSize: tokens.textSmSize,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusSm,
  padding: "0.35rem 0.5rem",
  minWidth: 0,
};

/** What a finished import did, over every batch it sent. */
interface Totals {
  sent: number;
  created: number;
  matched: number;
  skipped: Outcome[];
  kept: Outcome[];
}

const EMPTY: Totals = { sent: 0, created: 0, matched: 0, skipped: [], kept: [] };

/** A file's rows as a table: a spreadsheet's first sheet, or a CSV. */
async function readFile(file: File): Promise<Table> {
  if (/\.xlsx$/i.test(file.name)) return toTable(await readSheet(file));
  return toTable(parseCsv(await file.text()));
}

/** "a" or "an", by how the word sounds as written. */
const article = (word: string) => (/^[aeiou]/i.test(word) ? "an" : "a");

/**
 * Import a list from a file into people or organizations.
 *
 * The file is read here, in the browser: a CSV, or the first sheet of an Excel workbook. Its
 * columns are matched to the fields an import row takes (a guess from the headers, which the
 * reader can change), rows can be narrowed by column, and the count of what will be sent is
 * shown before anything is. The rows then go to the import tool in batches, one at a time, so
 * no row passes through the agent. Each batch is idempotent, so a stopped or failed import is
 * finished by importing the file again.
 *
 * What the import skipped, and the matched rows whose existing value it kept, are offered as a
 * CSV of those file rows with the reason first, to fix and import again.
 */
export function ImportPage({
  entity,
  onBack,
  hostShowsTrail = false,
}: {
  entity: ImportEntity;
  onBack: () => void;
  /** The host draws the trail, whose first step goes back; the page then draws no back link. */
  hostShowsTrail?: boolean;
}) {
  const app = useApp();
  const call = useCall();
  const config = ENTITIES[entity];
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [table, setTable] = useState<Table | null>(null);
  const [mapping, setMapping] = useState<(string | null)[]>([]);
  const [filters, setFilters] = useState<RowFilter[]>([]);
  // What the reference column's values are under: whose ids they are, e.g. vendor://companies/.
  const [refPrefix, setRefPrefix] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const stop = useRef(false);
  // Leaving the page stops a running import after its current batch.
  useEffect(
    () => () => {
      stop.current = true;
    },
    [],
  );

  // The custom fields a column can go to are the workspace's own, read once.
  useEffect(() => {
    void call<DescribeFieldsResult | ApiError>("list_fields", {}).then((res) => {
      if (isApiError(res)) setError(res.message ?? res.error);
      else setTargets(importTargets(entity, buildSchema(res, entity)));
    });
  }, [call, entity]);

  const planned = useMemo(
    () => (table && targets ? plan(table, mapping, targets, filters, entity, refPrefix) : null),
    [table, targets, mapping, filters, entity, refPrefix],
  );
  const [first, second] = REQUIRED[entity];
  const refMapped = mapping.includes(REF_TARGET.key);
  const refPrefixOk = !refMapped || validRefPrefix(refPrefix);
  // A reference alone identifies a row too: the server matches it to the record it names.
  const requiredMapped = mapping.includes(first) || mapping.includes(second) || refMapped;
  const requiredLabels = CORE_TARGETS[entity]
    .filter((t) => t.key === first || t.key === second)
    .map((t) => t.label.toLowerCase())
    .join(" or ");

  const pick = async (file: File | undefined) => {
    if (!file || !targets) return;
    setError(null);
    setTotals(null);
    try {
      const read = await readFile(file);
      if (read.rows.length === 0) return setError("That file has no rows under its headers.");
      setFileName(file.name);
      setTable(read);
      setMapping(guessMapping(read.headers, targets, entity));
      setFilters([]);
    } catch (err: unknown) {
      setError(`That file could not be read: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const run = async () => {
    if (!planned || !table) return;
    stop.current = false;
    const done: Totals = { ...EMPTY, skipped: [], kept: [] };
    setProgress(0);
    for (const batch of batches(planned.rows)) {
      if (stop.current) break;
      let res: ImportResult | ApiError;
      try {
        res = await call<ImportResult | ApiError>(TOOL[entity], {
          rows: batch.map((r) => r.row),
          fields_by: `import of ${fileName}`.slice(0, 200),
        });
      } catch (err: unknown) {
        res = { error: "unavailable", message: err instanceof Error ? err.message : String(err) } as ApiError;
      }
      if (isApiError(res)) {
        // The batch saved nothing; its rows are named with the reason, and the import stops,
        // since the next batch would meet the same refusal.
        const reason = res.message ?? res.error;
        done.skipped.push(...batch.map((r) => ({ line: r.line, reason })));
        done.sent += batch.length;
        setError(`The import stopped: ${reason}`);
        break;
      }
      const found = outcomes(res, batch);
      done.sent += batch.length;
      done.created += res.created;
      done.matched += res.matched;
      done.skipped.push(...found.skipped);
      done.kept.push(...found.kept);
      setProgress(done.sent);
    }
    setProgress(null);
    setTotals(done);
  };

  const save = (found: Outcome[], what: string) => {
    if (!table) return;
    const base = (fileName ?? "import").replace(/\.[^.]+$/, "");
    void downloadFile(app, `${base}-${what}.csv`, outcomesCsv(table, found), "text/csv");
  };

  const pad = { padding: "1rem 1.25rem", maxWidth: 960 };

  return (
    <div style={pad}>
      <Stack gap="1rem">
        {hostShowsTrail ? null : (
          <Inline gap="0.75rem" align="center">
            <TextLink onClick={onBack}>← {config.plural[0].toUpperCase() + config.plural.slice(1)}</TextLink>
          </Inline>
        )}
        <Heading size="md">Import {config.plural}</Heading>
        <Text size="sm" tone="muted">
          {`A CSV or an Excel file, one ${config.noun} per row. ${article(config.noun) === "an" ? "An" : "A"} ${config.noun} already here is matched by its external reference, then ${entity === "organization" ? "its domain, else its name" : "email, else name and company"}, and gains only the values it lacks; importing the same file again changes nothing.`}
        </Text>

        {error ? (
          <Text size="sm" tone="danger" role="alert">
            {error}
          </Text>
        ) : null}

        <label style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
          <Text size="sm" weight="medium">
            File
          </Text>
          <input
            type="file"
            accept=".csv,.xlsx,text/csv"
            disabled={!targets || progress !== null}
            onChange={(e) => void pick(e.target.files?.[0])}
            style={control}
          />
        </label>

        {table && targets ? (
          <>
            <Section title={`Columns in ${fileName}`}>
              <Stack gap="0.4rem">
                {table.headers.map((header, col) => (
                  <Inline key={`${header}-${col}`} gap="0.75rem" align="center" style={{ flexWrap: "wrap" }}>
                    <div style={{ width: 200, minWidth: 0 }}>
                      <Text size="sm" weight="medium" truncate>
                        {header}
                      </Text>
                      <Text size="xs" tone="faint" truncate>
                        {table.rows.find((r) => r[col])?.[col] ?? "empty"}
                      </Text>
                    </div>
                    <select
                      aria-label={`Field for ${header}`}
                      value={mapping[col] ?? ""}
                      disabled={progress !== null}
                      onChange={(e) =>
                        setMapping((m) => {
                          const next = [...m];
                          const id = e.target.value || null;
                          // A field takes one column: choosing it here frees it elsewhere.
                          for (let i = 0; i < next.length; i++) if (id && next[i] === id) next[i] = null;
                          next[col] = id;
                          return next;
                        })
                      }
                      style={{ ...control, width: 260 }}
                    >
                      <option value="">Don't import</option>
                      {targets.map((t) => (
                        <option key={targetId(t)} value={targetId(t)}>
                          {t.kind === "field" ? `${targetLabel(t)} (custom)` : targetLabel(t)}
                        </option>
                      ))}
                    </select>
                  </Inline>
                ))}
              </Stack>
            </Section>

            {refMapped ? (
              <Section title="Whose ids are these?">
                <Stack gap="0.35rem">
                  <Text size="sm" tone="muted">
                    The reference column holds another system's id for each {config.noun}. Give the prefix that
                    makes it a reference, the same one every time you import from that source, so a later pull
                    matches the same {config.plural}.
                  </Text>
                  <input
                    aria-label="Reference prefix"
                    value={refPrefix}
                    placeholder="vendor://companies/"
                    disabled={progress !== null}
                    onChange={(e) => setRefPrefix(e.target.value)}
                    style={{ ...control, maxWidth: 360 }}
                  />
                  {refPrefixOk ? null : (
                    <Text size="sm" tone="danger">
                      {refPrefix
                        ? "A prefix starts with a lower-case name, then a colon, with no spaces: vendor://companies/"
                        : "Give the prefix to import."}
                    </Text>
                  )}
                </Stack>
              </Section>
            ) : null}

            <Section title="Only rows where">
              <Stack gap="0.4rem">
                {filters.map((f, i) => (
                  <Inline key={i} gap="0.5rem" align="center" style={{ flexWrap: "wrap" }}>
                    <select
                      aria-label="Column"
                      value={f.column}
                      onChange={(e) => setFilters((fs) => fs.map((x, j) => (j === i ? { ...x, column: Number(e.target.value) } : x)))}
                      style={control}
                    >
                      {table.headers.map((h, col) => (
                        <option key={col} value={col}>
                          {h}
                        </option>
                      ))}
                    </select>
                    <select
                      aria-label="Condition"
                      value={f.op}
                      onChange={(e) => setFilters((fs) => fs.map((x, j) => (j === i ? { ...x, op: e.target.value as FilterOp } : x)))}
                      style={control}
                    >
                      {OPS.map((op) => (
                        <option key={op} value={op}>
                          {op}
                        </option>
                      ))}
                    </select>
                    {f.op === "is" || f.op === "contains" ? (
                      <input
                        aria-label="Value"
                        value={f.value}
                        onChange={(e) => setFilters((fs) => fs.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                        style={control}
                      />
                    ) : null}
                    <TextLink onClick={() => setFilters((fs) => fs.filter((_, j) => j !== i))}>Remove</TextLink>
                  </Inline>
                ))}
                <div>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={progress !== null}
                    onClick={() => setFilters((fs) => [...fs, { column: 0, op: "is not empty", value: "" }])}
                  >
                    Add a condition
                  </Button>
                </div>
              </Stack>
            </Section>

            {planned ? (
              <Stack gap="0.5rem">
                <Text size="sm">
                  {`${planned.rows.length.toLocaleString()} of ${table.rows.length.toLocaleString()} rows will be imported.`}
                  {planned.filteredOut ? ` ${planned.filteredOut.toLocaleString()} left out by the conditions.` : ""}
                  {planned.missingRequired.length
                    ? ` ${planned.missingRequired.length.toLocaleString()} ${planned.missingRequired.length === 1 ? "has" : "have"} no ${requiredLabels}${refMapped ? " or reference" : ""} and ${planned.missingRequired.length === 1 ? "is" : "are"} left out.`
                    : ""}
                </Text>
                {requiredMapped ? null : (
                  <Text size="sm" tone="danger">
                    Choose the column that holds each {config.noun}'s {requiredLabels}, or map an id column to External reference.
                  </Text>
                )}
                <Inline gap="0.5rem" align="center">
                  {progress === null ? (
                    <Button
                      size="sm"
                      disabled={!requiredMapped || !refPrefixOk || planned.rows.length === 0}
                      onClick={() => void run()}
                    >
                      {`Import ${planned.rows.length.toLocaleString()} ${planned.rows.length === 1 ? config.noun : config.plural}`}
                    </Button>
                  ) : (
                    <>
                      <Text size="sm" role="status">
                        {`Imported ${progress.toLocaleString()} of ${planned.rows.length.toLocaleString()}…`}
                      </Text>
                      <Button variant="secondary" size="sm" onClick={() => (stop.current = true)}>
                        Stop
                      </Button>
                    </>
                  )}
                </Inline>
              </Stack>
            ) : null}
          </>
        ) : null}

        {totals ? (
          <Section title="Done">
            <Stack gap="0.4rem">
              <Text size="sm" role="status">
                {`${totals.sent.toLocaleString()} rows sent: ${totals.created.toLocaleString()} added, ${totals.matched.toLocaleString()} already here, ${totals.skipped.length.toLocaleString()} skipped.`}
              </Text>
              {totals.kept.length ? (
                <Text size="sm" tone="muted">
                  {(() => {
                    const n = new Set(totals.kept.map((k) => k.line)).size;
                    return n === 1
                      ? `1 matched ${config.noun} kept a value of its own in place of the file's.`
                      : `${n.toLocaleString()} matched ${config.plural} kept a value of their own in place of the file's.`;
                  })()}
                </Text>
              ) : null}
              {app.hostCapabilities.downloadFile ? (
                <Inline gap="0.5rem">
                  {totals.skipped.length ? (
                    <Button variant="secondary" size="sm" onClick={() => save(totals.skipped, "skipped")}>
                      Download skipped rows
                    </Button>
                  ) : null}
                  {totals.kept.length ? (
                    <Button variant="secondary" size="sm" onClick={() => save(totals.kept, "kept")}>
                      Download kept values
                    </Button>
                  ) : null}
                </Inline>
              ) : totals.skipped.length ? (
                <Stack gap="0.2rem">
                  {totals.skipped.slice(0, 20).map((s) => (
                    <Text key={`${s.line}-${s.reason}`} size="xs" tone="muted">
                      {`Row ${s.line + 2}: ${s.reason}`}
                    </Text>
                  ))}
                </Stack>
              ) : null}
            </Stack>
          </Section>
        ) : null}
      </Stack>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ borderTop: `1px solid ${tokens.border}`, paddingTop: "0.75rem" }}>
      <Stack gap="0.5rem">
        <Text size="sm" weight="medium">
          {title}
        </Text>
        {children}
      </Stack>
    </section>
  );
}
