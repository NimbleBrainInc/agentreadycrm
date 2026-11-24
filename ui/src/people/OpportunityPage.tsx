import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Inline, PageHeader, Spinner, Text, TextLink, tokens, useBreakpoint } from "@nimblebrain/synapse/ui";
import { readAll, useCall } from "../api";
import { EditOpportunityForm, LogInteractionForm } from "../components/forms";
import { OverflowMenu } from "../components/kit";
import { fmtDate, fmtMoney, markdownToHtml, outcomeTone } from "../format";
import {
  type ApiError,
  type Contact,
  type DescribeFieldsResult,
  type GetOpportunityResult,
  type GetPipelineResult,
  type FieldDef,
  type GetContactResult,
  type GetOrganizationResult,
  type Interaction,
  isApiError,
  type Opportunity,
  type Organization,
  type PipelineStage,
} from "../types";
import { dealLines, DueChip, EmailValue } from "./cells";
import { CustomFields, FieldLine, HISTORY, RecordLayout, RecordTimeline, Section, show, type TimelineEvent } from "./pagekit";

type Modal = null | "edit" | "log";
// The lanes a deal's timeline filters by, besides Activity and History.
const LANES = ["Touches"];

export interface Loaded {
  opportunity: Opportunity;
  interactions: Interaction[];
  organization: Organization | null;
  contact: Contact | null;
  stages: PipelineStage[];
  defs: FieldDef[];
  /** The account's custom fields, for their labels. */
  organizationDefs: FieldDef[];
}

export function buildTimeline(loaded: Loaded): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  const o = loaded.opportunity;
  for (const i of loaded.interactions) {
    events.push({
      key: `i:${i.id}`,
      lane: "Touches",
      at: i.occurred_at,
      glyph: i.kind === "email" ? "✉" : i.kind === "call" ? "☏" : "•",
      title: i.summary || i.kind,
      summary: [i.kind, i.direction].filter(Boolean).join(" · "),
      detail: i.body ? <div dangerouslySetInnerHTML={{ __html: markdownToHtml(i.body) }} /> : undefined,
    });
  }
  for (const [key, details] of Object.entries(o.field_details ?? {})) {
    const def = loaded.defs.find((d) => d.key === key);
    if (!details.at) continue;
    events.push({
      key: `f:${key}`,
      lane: HISTORY,
      at: details.at,
      glyph: "✎",
      title: `${def?.label ?? key} set to ${show(o.fields?.[key])}`,
      summary: [details.by ? `by ${details.by}` : null, details.why].filter(Boolean).join(" · ") || undefined,
      change: { by: details.by ?? "", why: details.why },
    });
  }
  if (o.closed_at) {
    events.push({
      key: "closed",
      lane: HISTORY,
      at: o.closed_at,
      glyph: o.outcome === "won" ? "✓" : "✕",
      title: o.outcome === "won" ? "Closed won" : "Closed lost",
    });
  }
  events.push({ key: "added", lane: HISTORY, at: o.created_at, glyph: "+", title: "Added", summary: o.source ? `from ${o.source}` : undefined });
  return events.sort((a, b) => (a.at < b.at ? 1 : -1));
}

/**
 * One deal: what is known about it as cards on the left, its history on the
 * right, with the stage as a picker in the header.
 */
export function OpportunityPage({
  opportunityId,
  backLabel,
  hostShowsTrail,
  onBack,
  onLoaded,
  onOpen,
  openInApp,
  refreshKey = 0,
}: {
  opportunityId: string;
  backLabel: string;
  hostShowsTrail: boolean;
  onBack: () => void;
  onLoaded: (title: string) => void;
  /** Open the account or the contact in place. */
  onOpen: (id: string) => void;
  /** The control that opens this deal in the app's own page, where the host offers one. */
  openInApp?: ReactNode;
  /** A change re-reads the deal in place, keeping any form open on the page. */
  refreshKey?: number;
}) {
  const call = useCall();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (isStale: () => boolean) => {
    try {
      const [got, history, pipeline, described] = await Promise.all([
        call<GetOpportunityResult | ApiError>("get_opportunity", { opportunity_id: opportunityId }),
        readAll<Interaction>(call, "list_interactions", { opportunity_id: opportunityId }, "interactions"),
        // Every stage, retired included, so a deal in a retired stage reads its label.
        call<GetPipelineResult | ApiError>("get_pipeline", { include_retired: true }),
        call<DescribeFieldsResult | ApiError>("list_fields", {}),
      ]);
      if (isStale()) return;
      if (isApiError(got)) return setError(got.message ?? got.error);
      // A history that did not load is an error, never an empty timeline.
      if (isApiError(history)) return setError(history.message ?? history.error);
      const o = got.opportunity;
      const [org, contact] = await Promise.all([
        o.organization_id
          ? call<GetOrganizationResult | ApiError>("get_organization", { organization_id: o.organization_id })
          : null,
        o.primary_contact_id ? call<GetContactResult | ApiError>("get_contact", { contact_id: o.primary_contact_id }) : null,
      ]);
      if (isStale()) return;
      setError(null);
      setLoaded({
        opportunity: o,
        interactions: history,
        organization: org && !isApiError(org) ? org.organization : null,
        contact: contact && !isApiError(contact) ? contact.contact : null,
        stages: isApiError(pipeline) ? [] : pipeline.stages,
        defs: isApiError(described) ? [] : (described.fields.opportunity?.custom ?? []),
        organizationDefs: isApiError(described) ? [] : (described.fields.organization?.custom ?? []),
      });
    } catch (err: unknown) {
      if (isStale()) return;
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [call, opportunityId]);

  // Overlapping re-reads can resolve out of order; only the latest one may land.
  useEffect(() => {
    let stale = false;
    void load(() => stale);
    return () => {
      stale = true;
    };
  }, [load, refreshKey]);
  useEffect(() => {
    if (loaded) onLoaded(loaded.opportunity.title);
  }, [loaded, onLoaded]);

  if (error) {
    return (
      <div style={{ padding: "1rem" }}>
        {hostShowsTrail ? null : <TextLink onClick={onBack}>← {backLabel}</TextLink>}
        <Text size="sm" tone="danger" style={{ display: "block", marginTop: "0.5rem" }}>
          {error}
        </Text>
      </div>
    );
  }
  if (!loaded) {
    return (
      <Inline gap="0.5rem" style={{ padding: "1rem" }}>
        <Spinner />
        <Text size="sm" tone="muted">
          Loading…
        </Text>
      </Inline>
    );
  }
  return (
    <OpportunityView
      loaded={loaded}
      backLabel={backLabel}
      hostShowsTrail={hostShowsTrail}
      onBack={onBack}
      onOpen={onOpen}
      openInApp={openInApp}
    />
  );
}

function OpportunityView({
  loaded,
  backLabel,
  hostShowsTrail,
  onBack,
  onOpen,
  openInApp,
}: {
  loaded: Loaded;
  backLabel: string;
  hostShowsTrail: boolean;
  onBack: () => void;
  onOpen: (id: string) => void;
  openInApp?: ReactNode;
}) {
  const call = useCall();
  const { ref, isNarrow: narrow } = useBreakpoint(820);
  const [modal, setModal] = useState<Modal>(null);
  const [moveError, setMoveError] = useState<string | null>(null);
  const events = useMemo(() => buildTimeline(loaded), [loaded]);
  const { opportunity: o, organization: org, contact, stages } = loaded;
  const stage = stages.find((s) => s.key === o.stage);
  // The account has its own line under the title, so the title does not repeat it.
  const lines = dealLines(o.title, org?.name);

  // The picker is move_opportunity; the announce that follows re-reads the page.
  const move = async (key: string) => {
    setMoveError(null);
    const res = await call<ApiError | { opportunity: unknown }>("move_opportunity", { opportunity_id: o.id, stage: key });
    if (isApiError(res)) setMoveError(res.message ?? res.error);
  };

  const menu = [
    ...(contact ? [{ label: "Log interaction", onSelect: () => setModal("log" as Modal) }] : []),
    { label: "Edit", onSelect: () => setModal("edit" as Modal) },
  ];

  return (
    <div ref={ref}>
      <div style={{ padding: "0.9rem 1.25rem", borderBottom: `1px solid ${tokens.border}` }}>
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: "0.7rem", minWidth: 0 }}>
              {hostShowsTrail ? null : (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Back to ${backLabel}`}
                  onClick={onBack}
                  style={{ fontSize: "1.1rem", lineHeight: 1, padding: "0.3rem 0.45rem", marginLeft: "-0.45rem" }}
                >
                  ←
                </Button>
              )}
              {lines.title}
            </span>
          }
          status={
            <span style={{ display: "inline-flex", alignItems: "center", gap: "0.5rem" }}>
              <select
                aria-label="Stage"
                value={o.stage}
                onChange={(e) => void move(e.target.value)}
                style={{
                  font: "inherit",
                  fontSize: tokens.textSmSize,
                  color: tokens.fg,
                  background: tokens.bg,
                  border: `1px solid ${tokens.border}`,
                  borderRadius: tokens.radiusXs,
                  padding: "0.15rem 0.4rem",
                }}
              >
                {stages
                  .filter((s) => !s.retired_at)
                  .map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.label} · {s.probability}%
                    </option>
                  ))}
                {stage?.retired_at ? (
                  <option value={o.stage} disabled>
                    {stage.label} (retired)
                  </option>
                ) : null}
                {stage ? null : <option value={o.stage}>{o.stage}</option>}
              </select>
              <Badge tone={outcomeTone(o.outcome)}>{o.outcome}</Badge>
            </span>
          }
          description={
            <>
              {org ? (
                <TextLink tone="default" onClick={() => onOpen(org.id)}>
                  {org.name}
                </TextLink>
              ) : null}
              {org && contact ? " · " : ""}
              {contact ? (
                <TextLink tone="default" onClick={() => onOpen(contact.id)}>
                  {contact.name}
                </TextLink>
              ) : null}
              {moveError ? (
                <Text size="sm" tone="danger" style={{ display: "block" }}>
                  {moveError}
                </Text>
              ) : null}
            </>
          }
          actions={
            <Inline gap="0.5rem">
              {openInApp}
              <OverflowMenu label="Actions" items={menu} />
            </Inline>
          }
        />
      </div>

      <RecordLayout
        narrow={narrow}
        cards={
          <>
            <Section title="Deal">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "0.5rem", margin: "0.2rem 0 0.5rem" }}>
                {[
                  [o.value != null ? fmtMoney(o.value, o.currency) : "–", "Value"],
                  [o.probability != null ? `${o.probability}%` : "–", o.probability_is_own ? "Own probability" : "Stage default"],
                  [o.weighted_value != null ? fmtMoney(o.weighted_value, o.currency) : "–", "Weighted"],
                ].map(([big, small]) => (
                  <div key={small} style={{ border: `1px solid ${tokens.border}`, borderRadius: tokens.radiusSm, padding: "0.45rem 0.6rem" }}>
                    <Text size="lg" weight="semibold" style={{ display: "block", fontVariantNumeric: "tabular-nums" }}>
                      {big}
                    </Text>
                    <Text size="xs" tone="muted">
                      {small}
                    </Text>
                  </div>
                ))}
              </div>
              <FieldLine label="Close date">
                <span style={{ display: "inline-flex", alignItems: "center", gap: "0.5rem" }}>
                  {o.close_date ? fmtDate(o.close_date) : <Text size="sm" tone="faint">–</Text>}
                  {o.close_date && o.outcome === "open" ? <DueChip closeDate={o.close_date} open /> : null}
                </span>
              </FieldLine>
              {o.closed_at ? <FieldLine label="Closed">{fmtDate(o.closed_at)}</FieldLine> : null}
              <FieldLine label="Source">{o.source ?? <Text size="sm" tone="faint">–</Text>}</FieldLine>
              <FieldLine label="Added">{fmtDate(o.created_at)}</FieldLine>
            </Section>
            <Section title="Fields">
              <CustomFields
                defs={loaded.defs}
                values={o.fields ?? undefined}
                details={o.field_details ?? undefined}
                edit={{ kind: "opportunity", id: o.id }}
              />
            </Section>
            <Section
              title="Account"
              action={org ? <TextLink onClick={() => onOpen(org.id)} style={{ fontSize: tokens.textXsSize }}>Open</TextLink> : undefined}
            >
              {org ? (
                <>
                  <Text size="sm" weight="medium">
                    {org.name}
                  </Text>{" "}
                  <Text size="sm" tone="muted">
                    {org.domain}
                  </Text>
                  {org.industry ? <FieldLine label="Industry">{org.industry}</FieldLine> : null}
                  {Object.entries(org.fields ?? {}).map(([key, value]) => (
                    <FieldLine key={key} label={loaded.organizationDefs.find((d) => d.key === key)?.label ?? key} custom>
                      {show(value)}
                    </FieldLine>
                  ))}
                </>
              ) : (
                <Text size="sm" tone="faint">
                  No account.
                </Text>
              )}
            </Section>
            <Section
              title="Primary contact"
              action={contact ? <TextLink onClick={() => onOpen(contact.id)} style={{ fontSize: tokens.textXsSize }}>Open</TextLink> : undefined}
            >
              {contact ? (
                <>
                  <Text size="sm" weight="medium">
                    {contact.name}
                  </Text>{" "}
                  <Text size="sm" tone="muted">
                    {contact.title ?? ""}
                  </Text>
                  <FieldLine label="Email">
                    <EmailValue email={contact.email} status={contact.email_status} />
                  </FieldLine>
                </>
              ) : (
                <Text size="sm" tone="faint">
                  No contact.
                </Text>
              )}
            </Section>
          </>
        }
        timeline={<RecordTimeline events={events} lanes={LANES} />}
      />

      {modal === "edit" ? <EditOpportunityForm opportunity={o} onClose={() => setModal(null)} /> : null}
      {modal === "log" && contact ? <LogInteractionForm contactId={contact.id} opportunityId={o.id} onClose={() => setModal(null)} /> : null}
    </div>
  );
}

