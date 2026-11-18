import { useCallback, useEffect, useState } from "react";
import { useDataSync, useToolAllowed } from "@nimblebrain/synapse/react";
import {
  Button,
  type Column,
  Inline,
  Spinner,
  Table,
  Text,
  TextLink,
  tokens,
} from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { Field, Modal } from "../components/kit";
import { fmtDate } from "../format";
import {
  type ApiError,
  type DefineFieldResult,
  type DescribeFieldsResult,
  isApiError,
  type UpdateFieldResult,
} from "../types";
import { CustomMark } from "./cells";
import { PipelineSettings } from "./PipelineSettings";

const TYPES = [
  ["select", "Select — one of a set of options"],
  ["multi_select", "Multi-select — several options"],
  ["text", "Text"],
  ["number", "Number"],
  ["date", "Date"],
  ["boolean", "Yes / no"],
  ["list", "List — free-form items"],
] as const;

const ENTITY_LABEL: Record<string, string> = { contact: "Person", organization: "Company", opportunity: "Deal" };

interface FieldRowData {
  id: string;
  entity: string;
  key: string;
  label: string;
  type: string;
  options?: string[];
  description?: string;
  definedBy?: string;
  custom: boolean;
  retiredAt?: string;
  replacedBy?: string;
  defaultColumn?: boolean;
  minimum?: number;
  maximum?: number;
}

/** A number box's text as a bound: blank is none; anything else must parse. */
function parseBound(text: string): number | undefined | "invalid" {
  if (!text.trim()) return undefined;
  const n = Number(text);
  return Number.isFinite(n) ? n : "invalid";
}

/** `1–5`, `≥ 0`, `≤ 100`, or nothing. */
export function rangeText(minimum?: number, maximum?: number): string {
  if (minimum !== undefined && maximum !== undefined) return `${minimum}–${maximum}`;
  if (minimum !== undefined) return `≥ ${minimum}`;
  return maximum !== undefined ? `≤ ${maximum}` : "";
}

/** The path `update_field` takes as `replaced_by` for `to`, seen from a field on `from`. */
function replacementPath(from: string, to: FieldRowData): string {
  return to.entity === from ? to.key : `${to.entity}.${to.key}`;
}

/**
 * The live custom fields that can take a retired field's place: the field's own entity's,
 * by bare key, and a company's or a person's under their prefix, the two entities other
 * records reach as `organization.<key>` / `contact.<key>`.
 */
function replacementChoices(field: FieldRowData, rows: FieldRowData[]): FieldRowData[] {
  return rows.filter(
    (r) =>
      r.custom &&
      !r.retiredAt &&
      r.id !== field.id &&
      (r.entity === field.entity || r.entity === "organization" || r.entity === "contact"),
  );
}

const selectStyle = {
  width: "100%",
  font: "inherit",
  fontSize: tokens.textSmSize,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusSm,
  padding: "0.45rem 0.6rem",
};

function NewFieldForm({ onClose }: { onClose: () => void }) {
  const call = useCall();
  const [entity, setEntity] = useState("organization");
  const [label, setLabel] = useState("");
  const [type, setType] = useState<string>("select");
  const [options, setOptions] = useState("");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const takesOptions = type === "select" || type === "multi_select";
  // The key is derived once from the label: it is what filters and views name, so
  // it is shown, and it does not change when the label later does.
  const key = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^(\d)/, "f_$1")
    .slice(0, 48);

  const submit = async () => {
    const low = type === "number" ? parseBound(minimum) : undefined;
    const high = type === "number" ? parseBound(maximum) : undefined;
    if (low === "invalid" || high === "invalid") return setError("Minimum and maximum must be numbers.");
    setBusy(true);
    setError(null);
    const res = await call<DefineFieldResult | ApiError>("define_field", {
      minimum: low,
      maximum: high,
      entity,
      key,
      label: label.trim(),
      type,
      options: takesOptions ? options.split(",").map((o) => o.trim()).filter(Boolean) : undefined,
      description: description.trim() || undefined,
      defined_by: "a person, in the People app",
    });
    setBusy(false);
    if (isApiError(res)) return setError(res.message ?? res.error);
    onClose();
  };

  return (
    <Modal
      title="New field"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={busy || !key}>
            {busy ? "Adding…" : "Add field"}
          </Button>
        </>
      }
    >
      <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
        <Text size="xs" tone="muted" weight="medium">
          Describes
        </Text>
        <select id="new-field-entity" value={entity} onChange={(e) => setEntity(e.target.value)} style={selectStyle}>
          <option value="organization">A company</option>
          <option value="contact">A person</option>
          <option value="opportunity">A deal</option>
        </select>
      </label>
      <Field label="Label" value={label} onChange={setLabel} placeholder="e.g. Funding stage" required autoFocus />
      {key ? (
        <Text size="xs" tone="faint" mono>
          key: {key}
        </Text>
      ) : null}
      <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
        <Text size="xs" tone="muted" weight="medium">
          Type
        </Text>
        <select id="new-field-type" value={type} onChange={(e) => setType(e.target.value)} style={selectStyle}>
          {TYPES.map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
      </label>
      {type === "number" ? (
        <span style={{ display: "flex", gap: "0.5rem" }}>
          <Field label="Minimum (optional)" value={minimum} onChange={setMinimum} placeholder="1" />
          <Field label="Maximum (optional)" value={maximum} onChange={setMaximum} placeholder="5" />
        </span>
      ) : null}
      {takesOptions ? (
        <Field label="Options, separated by commas" value={options} onChange={setOptions} placeholder="Seed, Series A, Series B" required />
      ) : null}
      <Field
        label="What it means, for the agent that fills it"
        value={description}
        onChange={setDescription}
        placeholder="e.g. Latest disclosed round. Leave empty if not public."
        textarea
        rows={3}
      />
      {error ? (
        <Text size="sm" tone="danger">
          {error}
        </Text>
      ) : null}
    </Modal>
  );
}

function EditFieldForm({
  field,
  onClose,
  onSaved,
}: {
  field: FieldRowData;
  onClose: () => void;
  onSaved: () => void;
}) {
  const call = useCall();
  const [label, setLabel] = useState(field.label);
  const [description, setDescription] = useState(field.description ?? "");
  const [removing, setRemoving] = useState<Set<string>>(new Set());
  const [minimum, setMinimum] = useState(field.minimum !== undefined ? String(field.minimum) : "");
  const [maximum, setMaximum] = useState(field.maximum !== undefined ? String(field.maximum) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const options = field.options ?? [];

  const toggle = (option: string) =>
    setRemoving((current) => {
      const next = new Set(current);
      if (next.has(option)) next.delete(option);
      else next.add(option);
      return next;
    });

  const submit = async () => {
    const low = field.type === "number" ? parseBound(minimum) : undefined;
    const high = field.type === "number" ? parseBound(maximum) : undefined;
    if (low === "invalid" || high === "invalid") return setError("Minimum and maximum must be numbers.");
    const rangeChanged = field.type === "number" && (low !== field.minimum || high !== field.maximum);
    setBusy(true);
    setError(null);
    try {
      const res = await call<UpdateFieldResult | ApiError>("update_field", {
        entity: field.entity,
        key: field.key,
        label,
        description,
        remove_options: removing.size ? [...removing] : undefined,
        // The form shows the whole range, so a change sends it whole: lift it, then set what is filled in.
        ...(rangeChanged ? { clear_bounds: true, minimum: low, maximum: high } : {}),
      });
      if (isApiError(res)) return setError(res.message ?? res.error);
      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit ${field.label}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={busy || !label.trim()}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <Text size="sm" tone="muted">
        Anything that uses the field by its key, {field.key}, keeps working.
      </Text>
      <Field label="Name" value={label} onChange={setLabel} />
      <Field
        label="What it means, for the agent that fills it"
        value={description}
        onChange={setDescription}
        textarea
        rows={3}
      />
      {field.type === "number" ? (
        <span style={{ display: "flex", gap: "0.5rem" }}>
          <Field label="Minimum" value={minimum} onChange={setMinimum} placeholder="None" />
          <Field label="Maximum" value={maximum} onChange={setMaximum} placeholder="None" />
        </span>
      ) : null}
      {options.length ? (
        <fieldset style={{ border: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <Text size="xs" tone="muted" weight="medium">
            Remove options no record holds
          </Text>
          {options.map((option) => (
            <label key={option} style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
              <input
                type="checkbox"
                aria-label={`Remove option ${option}`}
                checked={removing.has(option)}
                onChange={() => toggle(option)}
              />
              <Text size="sm">{option}</Text>
            </label>
          ))}
        </fieldset>
      ) : null}
      {error ? (
        <Text size="sm" tone="danger">
          {error}
        </Text>
      ) : null}
    </Modal>
  );
}

function RetireFieldForm({
  field,
  rows,
  onClose,
  onRetired,
}: {
  field: FieldRowData;
  rows: FieldRowData[];
  onClose: () => void;
  onRetired: () => void;
}) {
  const call = useCall();
  const [replacedBy, setReplacedBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const choices = replacementChoices(field, rows);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const res = await call<UpdateFieldResult | ApiError>("update_field", {
      entity: field.entity,
      key: field.key,
      retired: true,
      replaced_by: replacedBy || undefined,
    });
    setBusy(false);
    if (isApiError(res)) return setError(res.message ?? res.error);
    onRetired();
  };

  return (
    <Modal
      title={`Retire ${field.label}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={busy}>
            {busy ? "Retiring…" : "Retire field"}
          </Button>
        </>
      }
    >
      <Text size="sm" tone="muted">
        A retired field can no longer be set, filtered on or shown as a column. Its values stay on the records, and it can
        be restored.
      </Text>
      <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
        <Text size="xs" tone="muted" weight="medium">
          Replaced by
        </Text>
        <select id="retire-field-replacement" value={replacedBy} onChange={(e) => setReplacedBy(e.target.value)} style={selectStyle}>
          <option value="">Nothing</option>
          {choices.map((r) => {
            const path = replacementPath(field.entity, r);
            return (
              <option key={r.id} value={path}>
                {ENTITY_LABEL[r.entity] ?? r.entity} · {r.label} ({path})
              </option>
            );
          })}
        </select>
      </label>
      {error ? (
        <Text size="sm" tone="danger">
          {error}
        </Text>
      ) : null}
    </Modal>
  );
}

/**
 * People's settings section: what a person and a company can hold in this
 * workspace. The custom fields defined here, the core ones every workspace has,
 * and, for a workspace admin, the form that defines a new one.
 */
export function FieldsSettings() {
  const call = useCall();
  // Each control is offered when the host lists the tool it calls among the tools this viewer
  // may call. A host that keeps a tool to workspace admins refuses it to anyone else, whatever
  // this shows.
  const canDefine = useToolAllowed("define_field");
  const canUpdate = useToolAllowed("update_field");
  const [rows, setRows] = useState<FieldRowData[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showCore, setShowCore] = useState(false);
  const [showRetired, setShowRetired] = useState(false);
  const [retiring, setRetiring] = useState<FieldRowData | null>(null);
  const [editing, setEditing] = useState<FieldRowData | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  // Retired fields are read too, for the disclosure that restores them; every other view
  // reads the default, which leaves them out.
  const load = useCallback(async () => {
    const res = await call<DescribeFieldsResult | ApiError>("list_fields", { include_retired: true });
    if (isApiError(res)) return setError(res.message ?? res.error);
    const next: FieldRowData[] = [];
    const entities = ["contact", "organization", "opportunity"].filter((entity) => res.fields[entity]);
    // Custom first: they are what this workspace added and what this screen manages.
    for (const entity of entities) {
      for (const f of res.fields[entity].custom) {
        next.push({ id: `${entity}.${f.key}`, entity, key: f.key, label: f.label, type: f.type, options: f.options, description: f.description, definedBy: f.defined_by, custom: true, retiredAt: f.retired_at ?? undefined, replacedBy: f.replaced_by ?? undefined, defaultColumn: f.default_column ?? false, minimum: f.minimum ?? undefined, maximum: f.maximum ?? undefined });
      }
    }
    for (const entity of entities) {
      for (const f of res.fields[entity].core) {
        next.push({ id: `${entity}.${f.key}`, entity, key: f.key, label: f.key, type: f.type, description: f.description, custom: false });
      }
    }
    setError(null);
    setRows(next);
  }, [call]);

  useEffect(() => {
    void load();
  }, [load]);
  useDataSync(() => void load());

  const restore = async (r: FieldRowData) => {
    setRestoreError(null);
    const res = await call<UpdateFieldResult | ApiError>("update_field", { entity: r.entity, key: r.key, retired: false });
    if (isApiError(res)) return setRestoreError(res.message ?? res.error);
    await load();
  };

  const setDefaultColumn = async (r: FieldRowData, on: boolean) => {
    setRestoreError(null);
    const res = await call<UpdateFieldResult | ApiError>("update_field", { entity: r.entity, key: r.key, default_column: on });
    if (isApiError(res)) return setRestoreError(res.message ?? res.error);
    await load();
  };

  const fieldColumn: Column<FieldRowData> = {
    key: "field",
    header: "Field",
    width: 190,
    render: (r) => (
      <span style={{ display: "block", minWidth: 0 }}>
        <span style={{ display: "flex", alignItems: "center", gap: "0.45rem" }}>
          {r.custom ? <CustomMark /> : null}
          <Text size="sm" weight="medium" truncate>
            {r.custom ? r.label : r.key}
          </Text>
        </span>
        {r.custom ? (
          <Text size="xs" tone="faint" mono truncate>
            {r.key}
          </Text>
        ) : null}
      </span>
    ),
  };
  const onColumn: Column<FieldRowData> = {
    key: "on",
    header: "On",
    width: 90,
    render: (r) => <Text size="sm">{ENTITY_LABEL[r.entity] ?? r.entity}</Text>,
  };

  const columns: Column<FieldRowData>[] = [
    fieldColumn,
    onColumn,
    {
      key: "type",
      header: "Type",
      width: 180,
      render: (r) => (
        <span style={{ display: "block", minWidth: 0 }}>
          <Text size="sm">
            {r.type.replace("_", "-")}
            {rangeText(r.minimum, r.maximum) ? ` · ${rangeText(r.minimum, r.maximum)}` : ""}
          </Text>
          {r.options?.length ? (
            <Text size="xs" tone="muted" truncate title={r.options.join(", ")}>
              {r.options.join(", ")}
            </Text>
          ) : null}
        </span>
      ),
    },
    {
      key: "meaning",
      header: "What it means",
      render: (r) => (
        <span style={{ display: "block" }}>
          <Text size="sm" tone="muted">
            {r.description ?? ""}
          </Text>
          {r.custom && r.definedBy ? (
            <Text size="xs" tone="faint" style={{ display: "block" }}>
              Defined by {r.definedBy}
            </Text>
          ) : null}
        </span>
      ),
    },
    ...(canUpdate
      ? [
          {
            key: "default",
            header: "Default column",
            width: 120,
            render: (r: FieldRowData) =>
              r.custom ? (
                <input
                  type="checkbox"
                  aria-label={`Open ${r.label} as a default column`}
                  checked={r.defaultColumn ?? false}
                  onChange={(e) => void setDefaultColumn(r, e.target.checked)}
                />
              ) : null,
          },
          {
            key: "actions",
            header: "",
            width: 150,
            render: (r: FieldRowData) =>
              r.custom ? (
                <span style={{ display: "flex", gap: "0.25rem" }}>
                  <Button variant="ghost" size="sm" onClick={() => setEditing(r)}>
                    Edit
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setRetiring(r)}>
                    Retire
                  </Button>
                </span>
              ) : null,
          },
        ]
      : []),
  ];

  const retiredColumns: Column<FieldRowData>[] = [
    fieldColumn,
    onColumn,
    {
      key: "replaced",
      header: "Replaced by",
      width: 190,
      render: (r) => {
        if (!r.replacedBy) return <Text size="sm" tone="muted">Nothing</Text>;
        // A bare key is on the field's own entity; a prefixed one names the entity.
        const [entity, key] = r.replacedBy.includes(".") ? r.replacedBy.split(".", 2) : [r.entity, r.replacedBy];
        const to = rows?.find((f) => f.custom && f.entity === entity && f.key === key);
        return (
          <span style={{ display: "block", minWidth: 0 }}>
            <Text size="sm" truncate>
              {to ? `${ENTITY_LABEL[entity] ?? entity} · ${to.label}` : r.replacedBy}
            </Text>
            <Text size="xs" tone="faint" mono truncate>
              {r.replacedBy}
            </Text>
          </span>
        );
      },
    },
    { key: "retired", header: "Retired", width: 120, render: (r) => <Text size="sm">{fmtDate(r.retiredAt)}</Text> },
    ...(canUpdate
      ? [
          {
            key: "actions",
            header: "",
            width: 80,
            render: (r: FieldRowData) => (
              <Button variant="ghost" size="sm" onClick={() => void restore(r)}>
                Restore
              </Button>
            ),
          },
        ]
      : []),
  ];

  const heading = (text: string) => (
    <div style={{ padding: "1rem 1rem 0", fontSize: "0.6875rem", fontWeight: 600, letterSpacing: "0.07em", textTransform: "uppercase", color: tokens.fgFaint }}>
      {text}
    </div>
  );

  const retired = rows?.filter((r) => r.retiredAt) ?? [];

  return (
    <div>
      {heading("Pipeline")}
      <PipelineSettings />
      {heading("Fields")}
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap", padding: "0.75rem 1rem" }}>
        <Text size="sm" tone="muted" style={{ flex: 1, minWidth: 220 }}>
          Custom fields belong to this workspace. Each can be set on a record, shown as a column, and filtered and sorted on.
          {canDefine ? "" : " A workspace admin defines them."}
        </Text>
        {canDefine ? (
          <Button size="sm" onClick={() => setAdding(true)}>
            New field
          </Button>
        ) : null}
      </div>
      {error ? (
        <Text size="sm" tone="danger" style={{ padding: "1rem" }}>
          {error}
        </Text>
      ) : rows === null ? (
        <Inline gap="0.5rem" style={{ padding: "1rem" }}>
          <Spinner />
          <Text size="sm" tone="muted">
            Loading…
          </Text>
        </Inline>
      ) : (
        <>
          <Table
            data={rows.filter((r) => !r.retiredAt && (r.custom || showCore))}
            columns={columns}
            rowKey={(r) => r.id}
            minWidth={620}
            empty={
              <Text size="sm" tone="muted" style={{ display: "block", padding: "1rem" }}>
                No custom fields yet.
              </Text>
            }
          />
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", padding: "0.6rem 1rem" }}>
            <TextLink onClick={() => setShowCore((shown) => !shown)}>
              {showCore ? "Hide core fields" : `Show ${rows.filter((r) => !r.custom).length} core fields`}
            </TextLink>
            {retired.length ? (
              <TextLink onClick={() => setShowRetired((shown) => !shown)}>
                {showRetired ? "Hide retired fields" : `Show ${retired.length} retired ${retired.length === 1 ? "field" : "fields"}`}
              </TextLink>
            ) : null}
          </div>
          {showRetired && retired.length ? (
            <>
              {restoreError ? (
                <Text size="sm" tone="danger" style={{ display: "block", padding: "0 1rem 0.5rem" }}>
                  {restoreError} <TextLink onClick={() => setRestoreError(null)}>Dismiss</TextLink>
                </Text>
              ) : null}
              <Table data={retired} columns={retiredColumns} rowKey={(r) => r.id} minWidth={620} />
            </>
          ) : null}
        </>
      )}
      {adding ? <NewFieldForm onClose={() => setAdding(false)} /> : null}
      {editing ? (
        <EditFieldForm
          field={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
      {retiring && rows ? (
        <RetireFieldForm
          field={retiring}
          rows={rows}
          onClose={() => setRetiring(null)}
          onRetired={() => {
            setRetiring(null);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
