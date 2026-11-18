import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { downloadFile } from "@nimblebrain/synapse";
import { useApp, useDataSync } from "@nimblebrain/synapse/react";
import {
  Button,
  type Column,
  EmptyState,
  Inline,
  SearchField,
  SegmentedControl,
  Spinner,
  Table,
  Text,
  tokens,
} from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import {
  type ApiError,
  type DescribeFieldsResult,
  type GetPipelineResult,
  isApiError,
  type ListRowsResult,
  type PipelineStage,
  type Row,
} from "../types";
import { Board } from "./Board";
import { Cell, CustomMark, type StageLabels } from "./cells";
import { exportColumns, toCsv } from "./export";
import { FilterPanel, filterable } from "./FilterPanel";
import { type Clause, describeEntry, type FilterEntry, type FilterState, isActive, toFilter } from "./filters";
import { buildSchema, type Entity, ENTITIES, type FieldSpec, GROUPS } from "./schema";

const PAGE_SIZES = [25, 50, 100];
// Rows an export reads per call: the most a list tool returns in one page.
const EXPORT_PAGE = 200;
const SORTABLE = new Set(["text", "select", "number", "date", "timestamp"]);
// A column whose reader wants the biggest or the newest first.
const DESCENDING_FIRST = new Set(["number", "timestamp", "date"]);
// Column content widths. The table is fixed-layout so a long value truncates in
// its cell instead of widening the column and pushing the rest off.
const WIDTHS: Record<string, number> = {
  name: 200,
  title: 240,
  "organization.name": 170,
  email: 220,
};
const DEFAULT_WIDTH = 120;
// A cell's horizontal padding, which a fixed-layout column adds to its width.
const CELL_PADDING = 24;
const widthOf = (path: string) => WIDTHS[path] ?? DEFAULT_WIDTH;
const SELECT_WIDTH = 16;

interface Sort {
  path: string;
  descending: boolean;
}

/** The arguments of the list call on screen, and how many rows it matches. */
export interface ListState {
  filter: Clause | null;
  sort: string | null;
  columns: string[];
  total: number | null;
}

interface Paging {
  key: string;
  page: number;
  cursors: (string | null)[];
}

function SortHeader({ spec, sort, onSort }: { spec: FieldSpec; sort: Sort | null; onSort: () => void }) {
  const active = sort?.path === spec.path;
  const label = (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
      {spec.custom ? <CustomMark /> : null}
      {spec.label}
      {active ? <span aria-hidden>{sort.descending ? "↓" : "↑"}</span> : null}
    </span>
  );
  if (!SORTABLE.has(spec.kind)) return label;
  return (
    <button
      type="button"
      onClick={onSort}
      aria-label={`Sort by ${spec.label}`}
      style={{
        all: "unset",
        cursor: "pointer",
        color: active ? tokens.fg : "inherit",
      }}
    >
      {label}
    </button>
  );
}

/**
 * A row's checkbox, or the header's for the page. The label covers the cell's
 * padding and keeps the click, so a near miss selects the row rather than opening it.
 */
function SelectBox({
  checked,
  indeterminate = false,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate?: boolean;
  label: string;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <label
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
      style={{
        display: "flex",
        margin: "-0.6rem -12px",
        padding: "0.6rem 12px",
        cursor: "pointer",
      }}
    >
      <input
        ref={ref}
        type="checkbox"
        aria-label={label}
        checked={checked}
        onChange={onChange}
        style={{ margin: 0, cursor: "pointer", accentColor: tokens.accent }}
      />
    </label>
  );
}

function ColumnPicker({
  schema,
  columns,
  onToggle,
  onClose,
}: {
  schema: FieldSpec[];
  columns: string[];
  onToggle: (path: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const escape = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [onClose]);
  return (
    <div
      ref={ref}
      role="dialog"
      aria-label="Columns"
      style={{
        position: "absolute",
        right: 0,
        top: "calc(100% + 6px)",
        zIndex: 20,
        width: 250,
        maxHeight: 380,
        overflowY: "auto",
        background: tokens.bg,
        border: `1px solid ${tokens.border}`,
        borderRadius: tokens.radiusSm,
        boxShadow: tokens.shadowLg,
        padding: "0.6rem 0.75rem",
      }}
    >
      {GROUPS.map((group) => {
        const specs = schema.filter((s) => s.group === group);
        if (specs.length === 0) return null;
        return (
          <div key={group} style={{ marginBottom: "0.5rem" }}>
            <div
              style={{
                fontSize: "0.6875rem",
                fontWeight: 600,
                letterSpacing: "0.07em",
                textTransform: "uppercase",
                color: tokens.fgFaint,
                margin: "0.25rem 0",
              }}
            >
              {group}
            </div>
            {specs.map((s) => (
              <label
                key={s.path}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.15rem 0",
                  fontSize: tokens.textSmSize,
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={columns.includes(s.path)}
                  disabled={s.path === "name"}
                  onChange={() => onToggle(s.path)}
                />
                {s.label}
                {s.custom ? <CustomMark /> : null}
              </label>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/**
 * A list of people or of organizations: one table over the entity's list tool,
 * with the filter, sort, and columns the reader chose. Every control maps to an
 * argument of that tool, so what is on screen is a call an agent could have made.
 */
export function RecordList({
  entity,
  search: launchSearch,
  onOpen,
  onState,
  onNew,
  onImport,
}: {
  entity: Entity;
  /** A query the list opens searching for: the one the tool call it was rendered for searched. */
  search?: string;
  onOpen: (id: string) => void;
  /** What the list is showing, for the app to publish as the agent's context. */
  onState: (state: ListState) => void;
  onNew: () => void;
  /** Open the import of a file into this list; absent where the list cannot be imported. */
  onImport?: () => void;
}) {
  const call = useCall();
  const app = useApp();
  const config = ENTITIES[entity];

  const [schema, setSchema] = useState<FieldSpec[] | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [filters, setFilters] = useState<FilterState>({});
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<Sort | null>(null);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [panelOpen, setPanelOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);

  // Keyset paging has no page numbers: `cursors[n]` is where page n starts. The
  // position is only meaningful for the listing it was taken under, so it is kept
  // with that listing's key and read as the first page under any other.
  const [paging, setPaging] = useState<Paging>({
    key: "",
    page: 0,
    cursors: [null],
  });
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  // A deal list has two shapes over one filter: the table, or the pipeline's
  // columns. The board shows open stages unless asked for the closed ones too.
  const [board, setBoard] = useState(entity === "opportunity");
  const [showClosed, setShowClosed] = useState(false);
  const [weighted, setWeighted] = useState(true);
  const [stages, setStages] = useState<PipelineStage[]>([]);
  // Selection is of the rows on screen: a page change or a new listing starts it empty.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Rows read so far by a running export of the whole listing.
  const [exporting, setExporting] = useState<number | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const cancelExport = useRef(false);

  useEffect(() => {
    void call<DescribeFieldsResult | ApiError>("list_fields", {}).then((res) => {
      if (isApiError(res)) return setError(res.message ?? res.error);
      const built = buildSchema(res, entity);
      setSchema(built);
      setColumns((current) => (current.length ? current : config.defaultColumns(built)));
    });
  }, [call, config, entity, refresh]);

  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput), 250);
    return () => clearTimeout(timer);
  }, [searchInput]);

  // A new launch query replaces the search box's text; the reader's own typing stays theirs.
  useEffect(() => {
    if (!launchSearch) return;
    setSearchInput(launchSearch);
    setSearch(launchSearch);
  }, [launchSearch]);

  // The pipeline names the stage columns and how a stage key reads in a cell. A deal can sit
  // in a retired stage, so the labels read every stage; the board's columns are the live ones.
  useEffect(() => {
    if (entity !== "opportunity") return;
    void call<GetPipelineResult | ApiError>("get_pipeline", { include_retired: true }).then((res) => {
      if (!isApiError(res)) setStages(res.stages);
    });
  }, [call, entity, refresh]);
  const liveStages = useMemo(() => stages.filter((s) => !s.retired_at), [stages]);
  const stageLabels = useMemo<StageLabels>(
    () => Object.fromEntries(stages.map((s) => [s.key, { label: s.label, outcome: s.outcome }])),
    [stages],
  );

  const filter = useMemo(
    () => (schema ? toFilter(filters, schema, search, config.searchPaths) : undefined),
    [filters, schema, search, config],
  );
  const fetched = useMemo(() => [...new Set([...columns, ...config.alwaysFetched])], [columns, config]);
  // The board with closed columns asks for every outcome; the list tool would
  // otherwise keep its open-only default under a filter that names neither path.
  // A filter that names the outcome or the stage has chosen its columns; the
  // closed ones show for it without the checkbox.
  const filterSpeaksToOpenness = useMemo(() => {
    const names = (clause: Clause | undefined): boolean =>
      !clause
        ? false
        : "and" in clause
          ? clause.and.some(names)
          : "or" in clause
            ? clause.or.some(names)
            : clause.field === "outcome" || clause.field === "stage";
    return names(filter);
  }, [filter]);
  const closedShown = showClosed || filterSpeaksToOpenness;
  const boardFilter = useMemo<Clause | undefined>(() => {
    if (!showClosed) return filter;
    const all: Clause = { field: "outcome", op: "is_set", value: true };
    return filter ? { and: [filter, all] } : all;
  }, [filter, showClosed]);
  // What the listing is: a change to any of these starts again from the first page.
  const queryKey = JSON.stringify([filter ?? null, sort, fetched, pageSize]);

  const current = paging.key === queryKey ? paging : { key: queryKey, page: 0, cursors: [null] };
  const { page, cursors } = current;

  useEffect(() => {
    if (!schema || columns.length === 0 || board) return;
    let stale = false;
    setLoading(true);
    void call<ListRowsResult | ApiError>(config.listTool, {
      filter,
      sort: sort ? `${sort.descending ? "-" : ""}${sort.path}` : undefined,
      columns: fetched,
      limit: pageSize,
      cursor: cursors[page] ?? undefined,
    })
      .then((res) => {
        if (stale) return;
        if (isApiError(res)) {
          setError(res.message ?? res.error);
          setRows([]);
          return;
        }
        setError(null);
        setRows(res[config.rowsKey as "contacts" | "organizations" | "opportunities"] ?? []);
        if (res.total !== undefined) setTotal(res.total);
        setPaging((previous) => {
          const base = previous.key === queryKey ? previous : { key: queryKey, page: 0, cursors: [null] };
          const next = base.cursors.slice(0, page + 1);
          next[page + 1] = res.next_cursor;
          return { ...base, cursors: next };
        });
      })
      .catch((err: unknown) => !stale && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !stale && setLoading(false));
    return () => {
      stale = true;
    };
    // `queryKey` covers filter / sort / fetched / pageSize; `cursors` is read at `page`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call, schema, queryKey, page, refresh, board]);

  // People announces every write — the agent's, another view's, this one's — so re-read.
  useDataSync(() => setRefresh((n) => n + 1));

  useEffect(() => {
    onState({
      filter: filter ?? null,
      sort: sort ? `${sort.descending ? "-" : ""}${sort.path}` : null,
      columns,
      total,
    });
    // `queryKey` covers filter / sort / columns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onState, queryKey, total]);

  const setFilter = useCallback((path: string, entry: FilterEntry | undefined) => {
    setFilters((current) => {
      const next = { ...current };
      if (entry) next[path] = entry;
      else delete next[path];
      return next;
    });
  }, []);

  const toggleSort = (spec: FieldSpec) =>
    setSort((current) =>
      current?.path === spec.path
        ? { path: spec.path, descending: !current.descending }
        : { path: spec.path, descending: DESCENDING_FIRST.has(spec.kind) },
    );

  useEffect(() => {
    setSelected(new Set());
  }, [queryKey, page]);
  useEffect(
    () => () => {
      cancelExport.current = true;
    },
    [],
  );

  const toggleColumn = (path: string) =>
    setColumns((current) => (current.includes(path) ? current.filter((c) => c !== path) : [...current, path]));

  if (!schema) {
    return error ? (
      <Text size="sm" tone="danger" style={{ padding: "1rem" }}>
        {error}
      </Text>
    ) : (
      <Inline gap="0.5rem" style={{ padding: "1rem" }}>
        <Spinner />
        <Text size="sm" tone="muted">
          Loading…
        </Text>
      </Inline>
    );
  }

  const byPath = new Map(schema.map((s) => [s.path, s]));
  const visible = columns.map((path) => byPath.get(path)).filter((s): s is FieldSpec => !!s);
  const active = schema.filter((s) => isActive(s, filters[s.path]));
  const chosen = rows.filter((row) => selected.has(row.id));
  const toggleRow = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const tableColumns: Column<Row>[] = [
    {
      key: "__select",
      header: (
        <SelectBox
          label="Select every row on this page"
          checked={rows.length > 0 && chosen.length === rows.length}
          indeterminate={chosen.length > 0 && chosen.length < rows.length}
          onChange={() => setSelected(chosen.length === rows.length ? new Set() : new Set(rows.map((row) => row.id)))}
        />
      ),
      render: (row) => (
        <SelectBox
          label={`Select ${typeof row.name === "string" ? row.name : "this row"}`}
          checked={selected.has(row.id)}
          onChange={() => toggleRow(row.id)}
        />
      ),
      width: SELECT_WIDTH,
    },
    ...visible.map((spec): Column<Row> => ({
      key: spec.path,
      header: <SortHeader spec={spec} sort={sort} onSort={() => toggleSort(spec)} />,
      render: (row) => <Cell spec={spec} row={row} companions={config.companions} stages={stageLabels} />,
      align: spec.kind === "number" && spec.path !== "signals.active" ? "right" : "left",
      width: widthOf(spec.path),
    })),
  ];
  // Below this the table scrolls sideways inside its own container rather than
  // crushing its columns; above it the columns share the extra in proportion.
  const tableMinWidth = visible.reduce(
    (sum, spec) => sum + widthOf(spec.path) + CELL_PADDING,
    SELECT_WIDTH + CELL_PADDING,
  );
  const first = page * pageSize + 1;
  const hasNext = !!cursors[page + 1];
  const anyFilterable = schema.some(filterable);
  const sortArg = sort ? `${sort.descending ? "-" : ""}${sort.path}` : undefined;

  // The selected rows, or with none selected every row the listing matches, as a
  // CSV of the visible columns. The whole listing is read page by page with the
  // list's own filter and sort, so the file is the view, not a second query.
  const runExport = async () => {
    const specs = exportColumns(visible, schema, config.companions);
    setExportError(null);
    let out: Row[] = chosen;
    try {
      if (chosen.length === 0) {
        out = [];
        cancelExport.current = false;
        setExporting(0);
        let cursor: string | undefined;
        do {
          const res = await call<ListRowsResult | ApiError>(config.listTool, {
            filter,
            sort: sortArg,
            columns: specs.map((spec) => spec.path),
            limit: EXPORT_PAGE,
            cursor,
          });
          if (cancelExport.current) return;
          if (isApiError(res)) return setExportError(res.message ?? res.error);
          out.push(...(res[config.rowsKey as "contacts" | "organizations" | "opportunities"] ?? []));
          cursor = res.next_cursor ?? undefined;
          setExporting(out.length);
        } while (cursor);
      }
      await downloadFile(
        app,
        `${config.plural}-${new Date().toISOString().slice(0, 10)}.csv`,
        toCsv(specs, out),
        "text/csv",
      );
    } catch (err: unknown) {
      setExportError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      {/* The count and the pager sit here, not under the table, and the bar stays
          in view while the rows scroll. A host that sizes the frame to its content
          scrolls the frame itself, where nothing can pin; there the bar is simply
          first, which a bar under the table could not be. */}
      <div
        style={{
          position: "sticky",
          top: 0,
          zIndex: 10,
          background: tokens.bg,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            flexWrap: "wrap",
            padding: "0.6rem 1rem",
          }}
        >
          {anyFilterable ? (
            <Button
              variant="secondary"
              size="sm"
              aria-expanded={panelOpen}
              onClick={() => setPanelOpen((open) => !open)}
            >
              Filters{active.length ? ` · ${active.length}` : ""}
            </Button>
          ) : null}
          <div style={{ flex: "1 1 200px", maxWidth: 320 }}>
            <SearchField
              variant="boxed"
              placeholder={config.searchPlaceholder}
              aria-label={`Search ${config.plural}`}
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          <span style={{ flex: 1 }} />
          {entity === "opportunity" ? (
            <SegmentedControl
              options={[
                { label: "Board", value: "board" },
                { label: "List", value: "list" },
              ]}
              value={board ? "board" : "list"}
              onChange={(value) => setBoard(value === "board")}
            />
          ) : null}
          {board ? (
            <>
              <SegmentedControl
                options={[
                  { label: "Value", value: "value" },
                  { label: "Weighted", value: "weighted" },
                ]}
                value={weighted ? "weighted" : "value"}
                onChange={(value) => setWeighted(value === "weighted")}
              />
              <label
                title={filterSpeaksToOpenness ? "The filter names a stage or outcome, so the closed columns show" : undefined}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.35rem",
                  fontSize: tokens.textSmSize,
                  color: tokens.fgMuted,
                }}
              >
                <input
                  type="checkbox"
                  checked={closedShown}
                  disabled={filterSpeaksToOpenness}
                  onChange={(e) => setShowClosed(e.target.checked)}
                />
                Closed
              </label>
            </>
          ) : null}
          {board ? null : (
            <Text size="sm" tone="muted" style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>
              {total === null
                ? ""
                : rows.length === 0
                  ? `0 ${config.plural}`
                  : `${first.toLocaleString()}–${(first + rows.length - 1).toLocaleString()} of ${total.toLocaleString()}`}
            </Text>
          )}
          {board ? null : (
            <Inline gap="0.25rem">
              <Button
                variant="secondary"
                size="sm"
                aria-label="Previous page"
                disabled={page === 0 || loading}
                onClick={() => setPaging({ ...current, page: page - 1 })}
              >
                ‹
              </Button>
              <Button
                variant="secondary"
                size="sm"
                aria-label="Next page"
                disabled={!hasNext || loading}
                onClick={() => setPaging({ ...current, page: page + 1 })}
              >
                ›
              </Button>
            </Inline>
          )}
          {board ? null : (
            <div style={{ position: "relative" }}>
              <Button
                variant="secondary"
                size="sm"
                aria-expanded={pickerOpen}
                onClick={() => setPickerOpen((open) => !open)}
              >
                Columns
              </Button>
              {pickerOpen ? (
                <ColumnPicker
                  schema={schema}
                  columns={columns}
                  onToggle={toggleColumn}
                  onClose={() => setPickerOpen(false)}
                />
              ) : null}
            </div>
          )}
          {/* Offered only where the host said it can hand over a file. */}
          {app.hostCapabilities.downloadFile && !board ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={exporting === null && (total ?? 0) === 0}
              title={
                exporting !== null
                  ? "Stop the export"
                  : chosen.length
                    ? "Export the selected rows as CSV"
                    : "Export every row this view matches as CSV"
              }
              onClick={() => {
                if (exporting === null) void runExport();
                else cancelExport.current = true;
              }}
            >
              {exporting !== null
                ? `Exporting ${exporting.toLocaleString()}…`
                : chosen.length
                  ? `Export ${chosen.length.toLocaleString()}`
                  : "Export"}
            </Button>
          ) : null}
          {onImport ? (
            <Button variant="secondary" size="sm" onClick={onImport}>
              Import
            </Button>
          ) : null}
          <Button size="sm" onClick={onNew}>
            {config.newLabel}
          </Button>
        </div>
        {active.length || exportError ? (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: "0.35rem",
              padding: "0 1rem 0.6rem",
            }}
          >
            {active.map((spec) => (
              <span
                key={spec.path}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.35rem",
                  maxWidth: "100%",
                  background: tokens.bgSubtle,
                  borderRadius: 999,
                  padding: "0.1rem 0.2rem 0.1rem 0.6rem",
                  fontSize: tokens.textSmSize,
                }}
              >
                <span style={{ color: tokens.fgMuted }}>{spec.label}</span>
                <span
                  style={{
                    fontWeight: 500,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {describeEntry(spec, filters[spec.path])}
                </span>
                <button
                  type="button"
                  aria-label={`Remove the ${spec.label} filter`}
                  onClick={() => setFilter(spec.path, undefined)}
                  style={{
                    all: "unset",
                    cursor: "pointer",
                    padding: "0 0.4rem",
                    color: tokens.fgMuted,
                  }}
                >
                  ×
                </button>
              </span>
            ))}
            {active.length > 1 ? (
              <Button variant="ghost" size="sm" onClick={() => setFilters({})}>
                Clear
              </Button>
            ) : null}
            {exportError ? (
              <Text size="sm" tone="danger">
                Export failed: {exportError}
              </Text>
            ) : null}
          </div>
        ) : null}
      </div>

      {panelOpen ? (
        <FilterPanel
          entity={entity}
          schema={schema}
          state={filters}
          search={search}
          searchPaths={config.searchPaths}
          onChange={setFilter}
        />
      ) : null}

      {error ? (
        <Text size="sm" tone="danger" style={{ padding: "1rem" }}>
          {error}
        </Text>
      ) : board ? (
        <Board stages={liveStages} filter={boardFilter} showClosed={closedShown} weighted={weighted} onOpen={onOpen} />
      ) : (
        <Table
          data={rows}
          columns={tableColumns}
          rowKey={(row) => row.id}
          onRowClick={(row) => onOpen(row.id)}
          minWidth={tableMinWidth}
          style={{ opacity: loading ? 0.6 : 1 }}
          empty={
            loading ? (
              <Inline gap="0.5rem" style={{ padding: "1rem" }}>
                <Spinner />
                <Text size="sm" tone="muted">
                  Loading…
                </Text>
              </Inline>
            ) : filter ? (
              <EmptyState title={`No ${config.plural} match`} description="Loosen or clear the filters to see more." />
            ) : (
              <EmptyState
                title={`No ${config.plural} yet`}
                description={`Add one, or ask the agent to research and create ${config.plural}.`}
              />
            )
          }
        />
      )}

      {/* What a reader who has scrolled to the last row needs: the next page, and how many rows a page holds. */}
      {board ? null : (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            flexWrap: "wrap",
            padding: "0.55rem 1rem",
            borderTop: `1px solid ${tokens.border}`,
          }}
        >
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.4rem",
              fontSize: tokens.textSmSize,
              color: tokens.fgMuted,
            }}
          >
            Rows
            <select
              id={`${config.plural}-page-size`}
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              style={{
                font: "inherit",
                color: tokens.fg,
                background: tokens.bg,
                border: `1px solid ${tokens.border}`,
                borderRadius: tokens.radiusXs,
              }}
            >
              {PAGE_SIZES.map((n) => (
                <option key={n}>{n}</option>
              ))}
            </select>
          </label>
          <span style={{ flex: 1 }} />
          <Button
            variant="secondary"
            size="sm"
            disabled={page === 0 || loading}
            onClick={() => setPaging({ ...current, page: page - 1 })}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasNext || loading}
            onClick={() => setPaging({ ...current, page: page + 1 })}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
