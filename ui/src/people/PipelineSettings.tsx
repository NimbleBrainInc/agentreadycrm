import { useCallback, useEffect, useState } from "react";
import { useDataSync, useToolAllowed } from "@nimblebrain/synapse/react";
import { Badge, Button, Inline, Spinner, Stack, Text, TextLink, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { Field, Modal } from "../components/kit";
import { outcomeTone } from "../format";
import { type ApiError, type GetPipelineResult, isApiError, type PipelineStage } from "../types";

const OUTCOMES = ["open", "won", "lost"];

/**
 * The key a new stage will be stored under: what was typed, else a slug of the
 * label, trimmed and lowercased the way the server normalises it, so the clash
 * check and the save speak of the same stage.
 */
export function stageKey(typed: string, label: string): string {
  const slug = label.replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
  return (typed || slug).trim().toLowerCase();
}

const inputStyle = {
  font: "inherit",
  fontSize: tokens.textSmSize,
  color: tokens.fg,
  background: tokens.bg,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radiusXs,
  padding: "0.2rem 0.4rem",
};

/**
 * A live stage's editable row: label, default probability, outcome, each saved on blur or
 * change, and Retire.
 */
function StageRow({
  stage,
  first,
  last,
  canDefine,
  onDefine,
  onRetire,
}: {
  stage: PipelineStage;
  first: boolean;
  last: boolean;
  /** Whether the host lists `update_pipeline_stage` among the tools this viewer may call. */
  canDefine: boolean;
  onDefine: (key: string, patch: Record<string, unknown>) => Promise<void>;
  onRetire: (stage: PipelineStage) => void;
}) {
  const [label, setLabel] = useState(stage.label);
  const [probability, setProbability] = useState(String(stage.probability));
  useEffect(() => {
    setLabel(stage.label);
    setProbability(String(stage.probability));
  }, [stage.label, stage.probability]);

  const cell = { padding: "0.45rem 0.6rem", borderBottom: `1px solid ${tokens.border}`, verticalAlign: "middle" as const };
  return (
    <tr>
      <td style={{ ...cell, whiteSpace: "nowrap", width: 70 }}>
        {canDefine ? (
          <Inline gap="0.1rem">
            <Button variant="ghost" size="sm" aria-label="Move up" disabled={first} onClick={() => void onDefine(stage.key, { position: stage.position - 1 })}>
              ↑
            </Button>
            <Button variant="ghost" size="sm" aria-label="Move down" disabled={last} onClick={() => void onDefine(stage.key, { position: stage.position + 1 })}>
              ↓
            </Button>
          </Inline>
        ) : (
          <Text size="sm" tone="faint" style={{ fontVariantNumeric: "tabular-nums" }}>
            {stage.position + 1}
          </Text>
        )}
      </td>
      <td style={cell}>
        {canDefine ? (
          <input
            aria-label={`Label of ${stage.key}`}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onBlur={() => label.trim() && label !== stage.label && void onDefine(stage.key, { label: label.trim() })}
            style={{ ...inputStyle, width: "100%" }}
          />
        ) : (
          <Text size="sm" weight="medium">
            {stage.label}
          </Text>
        )}
        <Text size="xs" tone="faint" mono>
          {stage.key}
        </Text>
      </td>
      <td style={{ ...cell, width: 110 }}>
        {canDefine ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.25rem" }}>
            <input
              aria-label={`Default probability of ${stage.key}`}
              type="number"
              min={0}
              max={100}
              value={probability}
              onChange={(e) => setProbability(e.target.value)}
              onBlur={() => {
                const n = Number(probability);
                if (Number.isInteger(n) && n >= 0 && n <= 100 && n !== stage.probability) void onDefine(stage.key, { probability: n });
                else setProbability(String(stage.probability));
              }}
              style={{ ...inputStyle, width: 64 }}
            />
            <Text size="sm" tone="muted">
              %
            </Text>
          </span>
        ) : (
          <Text size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>
            {stage.probability}%
          </Text>
        )}
      </td>
      <td style={{ ...cell, width: 110 }}>
        {canDefine ? (
          <select
            aria-label={`Outcome of ${stage.key}`}
            value={stage.outcome}
            onChange={(e) => void onDefine(stage.key, { outcome: e.target.value })}
            style={inputStyle}
          >
            {OUTCOMES.map((o) => (
              <option key={o}>{o}</option>
            ))}
          </select>
        ) : (
          <Badge tone={outcomeTone(stage.outcome)}>{stage.outcome}</Badge>
        )}
      </td>
      <td style={{ ...cell, width: 70, textAlign: "right" }}>
        <Text size="sm" tone="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
          {stage.deals.toLocaleString()}
        </Text>
      </td>
      <td style={{ ...cell, width: 80, textAlign: "right" }}>
        {canDefine ? (
          <Button variant="ghost" size="sm" onClick={() => onRetire(stage)}>
            Retire
          </Button>
        ) : null}
      </td>
    </tr>
  );
}

/**
 * Retire one stage, naming the live stage that replaces it. The server refuses while open
 * deals sit in it, and its message says where they are.
 */
function RetireStageForm({
  stage,
  live,
  onClose,
  onRetire,
}: {
  stage: PipelineStage;
  /** The live stages, of which any but this one can replace it. */
  live: PipelineStage[];
  onClose: () => void;
  onRetire: (key: string, patch: Record<string, unknown>) => Promise<string | null>;
}) {
  const [replacedBy, setReplacedBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const choices = live.filter((s) => s.key !== stage.key);

  const submit = async () => {
    setBusy(true);
    setError(null);
    const failure = await onRetire(stage.key, { retired: true, replaced_by: replacedBy || undefined });
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Modal
      title={`Retire ${stage.label}`}
      onClose={onClose}
      footer={
        <Inline gap="0.5rem" justify="end">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => void submit()} disabled={busy}>
            {busy ? "Retiring…" : "Retire stage"}
          </Button>
        </Inline>
      }
    >
      <Stack gap="0.75rem">
        <Text size="sm" tone="muted">
          A retired stage leaves the board, and no deal can enter it. The deals in it keep it, with its label and outcome,
          and it can be restored. A stage with open deals in it cannot be retired until they move.
        </Text>
        <label style={{ display: "grid", gap: "0.25rem", fontSize: tokens.textSmSize }}>
          <span style={{ color: tokens.fgMuted }}>Replaced by</span>
          <select aria-label="Replaced by" value={replacedBy} onChange={(e) => setReplacedBy(e.target.value)} style={inputStyle}>
            <option value="">Nothing</option>
            {choices.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label} ({s.key})
              </option>
            ))}
          </select>
        </label>
        {error ? (
          <Text size="sm" tone="danger">
            {error}
          </Text>
        ) : null}
      </Stack>
    </Modal>
  );
}

/** A retired stage's row: what it was, what replaced it, how many deals keep it, and Restore. */
function RetiredStageRow({
  stage,
  canDefine,
  onRestore,
}: {
  stage: PipelineStage;
  canDefine: boolean;
  onRestore: (key: string) => void;
}) {
  const cell = { padding: "0.45rem 0.6rem", borderBottom: `1px solid ${tokens.border}`, verticalAlign: "middle" as const };
  return (
    <tr>
      <td style={{ ...cell, width: 70 }} />
      <td style={cell}>
        <Text size="sm" tone="muted">
          {stage.label}
        </Text>
        <Text size="xs" tone="faint" mono>
          {stage.key}
          {stage.replaced_by ? ` → ${stage.replaced_by}` : ""}
        </Text>
      </td>
      <td style={{ ...cell, width: 110 }}>
        <Text size="sm" tone="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
          {stage.probability}%
        </Text>
      </td>
      <td style={{ ...cell, width: 110 }}>
        <Badge tone={outcomeTone(stage.outcome)}>{stage.outcome}</Badge>
      </td>
      <td style={{ ...cell, width: 70, textAlign: "right" }}>
        <Text size="sm" tone="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
          {stage.deals.toLocaleString()}
        </Text>
      </td>
      <td style={{ ...cell, width: 80, textAlign: "right" }}>
        {canDefine ? (
          <Button variant="ghost" size="sm" onClick={() => onRestore(stage.key)}>
            Restore
          </Button>
        ) : null}
      </td>
    </tr>
  );
}

function NewStageForm({
  taken,
  onClose,
  onDefine,
}: {
  /**
   * The keys the pipeline already holds, retired ones included: `define_pipeline_stage`
   * leaves a live one as it is and refuses a retired one.
   */
  taken: Set<string>;
  onClose: () => void;
  onDefine: (key: string, patch: Record<string, unknown>) => Promise<string | null>;
}) {
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [probability, setProbability] = useState("50");
  const [outcome, setOutcome] = useState("open");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const suggested = stageKey(key, label);

  const clash = taken.has(suggested);
  const submit = async () => {
    if (!label.trim() || !suggested || clash) return;
    setBusy(true);
    const failure = await onDefine(suggested, { label: label.trim(), probability: Number(probability) || 0, outcome });
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Modal
      title="New stage"
      onClose={onClose}
      footer={
        <Inline gap="0.5rem" justify="end">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" disabled={busy || !label.trim() || clash} onClick={() => void submit()}>
            Add
          </Button>
        </Inline>
      }
    >
      <Stack gap="0.75rem">
        {error ? (
          <Text size="sm" tone="danger">
            {error}
          </Text>
        ) : null}
        <Field label="Label" value={label} onChange={setLabel} required autoFocus />
        <Field label="Key" value={suggested} onChange={setKey} placeholder="contract_sent" />
        {clash ? (
          <Text size="sm" tone="danger">
            The pipeline already has a stage with the key {suggested}. Edit or restore that one in the table, or choose another key.
          </Text>
        ) : null}
        <Field label="Default probability %" value={probability} onChange={setProbability} />
        <label style={{ display: "grid", gap: "0.25rem", fontSize: tokens.textSmSize }}>
          <span style={{ color: tokens.fgMuted }}>Outcome</span>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)} style={inputStyle}>
            {OUTCOMES.map((o) => (
              <option key={o}>{o}</option>
            ))}
          </select>
        </label>
        <Text size="xs" tone="muted">
          The stage lands last. Move it with the arrows. A deal stores the key, so the key cannot change later; the label can.
        </Text>
      </Stack>
    </Modal>
  );
}

/**
 * The workspace's pipeline on the connector's settings page: the stages in
 * order, each with its default probability, outcome and how many deals it
 * holds, then the retired ones. A workspace admin edits, retires and restores
 * in place; anyone else reads.
 */
export function PipelineSettings() {
  const canAdd = useToolAllowed("define_pipeline_stage");
  const canDefine = useToolAllowed("update_pipeline_stage");
  const call = useCall();
  const [stages, setStages] = useState<PipelineStage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [retiring, setRetiring] = useState<PipelineStage | null>(null);

  const load = useCallback(async () => {
    const res = await call<GetPipelineResult | ApiError>("get_pipeline", { include_retired: true });
    if (isApiError(res)) return setError(res.message ?? res.error);
    setError(null);
    // A deal can hold a key the pipeline no longer names; it has no stage to edit.
    setStages(res.stages.filter((stage) => stage.id));
  }, [call]);
  useEffect(() => {
    void load();
  }, [load]);
  useDataSync(() => void load());

  const save = useCallback(
    async (tool: string, key: string, patch: Record<string, unknown>): Promise<string | null> => {
      const res = await call<ApiError | { stage: PipelineStage }>(tool, { key, ...patch });
      if (isApiError(res)) {
        const message = res.message ?? res.error;
        setError(message);
        return message;
      }
      await load();
      return null;
    },
    [call, load],
  );
  const define = useCallback(
    (key: string, patch: Record<string, unknown>) => save("define_pipeline_stage", key, patch),
    [save],
  );
  const update = useCallback(
    (key: string, patch: Record<string, unknown>) => save("update_pipeline_stage", key, patch),
    [save],
  );

  const live = (stages ?? []).filter((s) => !s.retired_at);
  const retired = (stages ?? []).filter((s) => s.retired_at);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap", padding: "0.75rem 1rem" }}>
        <Text size="sm" tone="muted" style={{ flex: 1, minWidth: 220 }}>
          The stages a deal moves through, in order. A deal entering a stage takes its default probability unless the deal has its own.
          {canDefine ? "" : " A workspace admin changes them."}
        </Text>
        {canAdd ? (
          <Button size="sm" onClick={() => setAdding(true)}>
            New stage
          </Button>
        ) : null}
      </div>
      {error ? (
        <Text size="sm" tone="danger" style={{ display: "block", padding: "0 1rem 0.5rem" }}>
          {error} <TextLink onClick={() => setError(null)}>Dismiss</TextLink>
        </Text>
      ) : null}
      {stages === null ? (
        <Inline gap="0.5rem" style={{ padding: "1rem" }}>
          <Spinner />
          <Text size="sm" tone="muted">
            Loading…
          </Text>
        </Inline>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 560 }}>
            <thead>
              <tr>
                {["", "Stage", "Default", "Outcome", "Deals", ""].map((h, i) => (
                  <th
                    key={i}
                    style={{
                      textAlign: i >= 4 ? "right" : "left",
                      padding: "0.4rem 0.6rem",
                      borderBottom: `1px solid ${tokens.border}`,
                      fontSize: "0.6875rem",
                      fontWeight: 600,
                      letterSpacing: "0.07em",
                      textTransform: "uppercase",
                      color: tokens.fgMuted,
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {live.map((stage, i) => (
                <StageRow
                  key={stage.id}
                  stage={stage}
                  first={i === 0}
                  last={i === live.length - 1}
                  canDefine={canDefine}
                  onDefine={async (key, patch) => {
                    await update(key, patch);
                  }}
                  onRetire={setRetiring}
                />
              ))}
              {retired.length ? (
                <tr>
                  <td colSpan={6} style={{ padding: "0.9rem 0.6rem 0.3rem" }}>
                    <Text size="xs" tone="muted" weight="medium">
                      Retired
                    </Text>
                  </td>
                </tr>
              ) : null}
              {retired.map((stage) => (
                <RetiredStageRow key={stage.id} stage={stage} canDefine={canDefine} onRestore={(key) => void update(key, { retired: false })} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {retiring ? <RetireStageForm stage={retiring} live={live} onClose={() => setRetiring(null)} onRetire={update} /> : null}
      {adding ? (
        <NewStageForm taken={new Set((stages ?? []).map((s) => s.key))} onClose={() => setAdding(false)} onDefine={define} />
      ) : null}
    </div>
  );
}
