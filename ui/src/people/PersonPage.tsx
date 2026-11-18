import { useCallback, useEffect, useMemo, useState } from "react";
import { useApp } from "@nimblebrain/synapse/react";
import {
  Avatar,
  Badge,
  Button,
  Inline,
  PageHeader,
  Spinner,
  Text,
  TextLink,
  tokens,
  useBreakpoint,
} from "@nimblebrain/synapse/ui";
import { readAll, useCall } from "../api";
import { DossierForm, EditContactForm, LogInteractionForm, MergeContactForm } from "../components/forms";
import { Dossier, OverflowMenu } from "../components/kit";
import { fmtDate, markdownToHtml, statusTone } from "../format";
import {
  type ApiError,
  type Contact,
  type DescribeFieldsResult,
  type FieldDef,
  type FieldDetails,
  type GetContactResult,
  type GetOrganizationResult,
  type Interaction,
  isApiError,
  type ListSignalsResult,
  type Organization,
  type Signal,
} from "../types";
import { EmailValue } from "./cells";
import { CustomFields, FieldLine, HISTORY, RecordLayout, RecordTimeline, Section, show, type TimelineEvent } from "./pagekit";

type Modal = null | "edit" | "log" | "dossier" | "merge";

// The lanes a person's timeline filters by, besides Activity and History.
const LANES = ["Emails", "Signals"];

export interface Loaded {
  contact: Contact;
  interactions: Interaction[];
  organization: Organization | null;
  signals: Signal[];
  defs: Record<string, FieldDef[]>;
}


function SignalCard({ signal }: { signal: Signal }) {
  const withdrawn = signal.status === "withdrawn";
  let host = signal.source_url;
  try {
    host = new URL(signal.source_url).host;
  } catch {
    // Keep the raw string: the server validated its shape, this is display only.
  }
  return (
    <div style={{ padding: "0.45rem 0", fontSize: tokens.textSmSize }}>
      <div style={withdrawn ? { color: tokens.fgFaint, textDecoration: "line-through" } : undefined}>{signal.claim}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.15rem", fontSize: tokens.textXsSize, color: tokens.fgFaint }}>
        <span>{signal.signal_type}</span>
        <span>{host}</span>
        {signal.event_date ? <span>happened {fmtDate(signal.event_date)}</span> : null}
        <span>{signal.source_date ? `source ${fmtDate(signal.source_date)}` : "undated source"}</span>
      </div>
      {withdrawn && signal.withdrawn_reason ? (
        <div style={{ fontSize: tokens.textXsSize, color: tokens.fgMuted, marginTop: "0.15rem" }}>{signal.withdrawn_reason}</div>
      ) : null}
    </div>
  );
}

export function buildTimeline(loaded: Loaded): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  for (const ix of loaded.interactions) {
    const inbound = ix.direction === "inbound";
    events.push({
      key: ix.id,
      lane: ix.kind === "email" ? "Emails" : "Touches",
      at: ix.occurred_at,
      glyph: ix.kind === "email" ? (inbound ? "↙" : "↗") : "•",
      title: ix.kind === "email" ? (inbound ? "Email received" : "Email sent") : ix.kind.charAt(0).toUpperCase() + ix.kind.slice(1),
      summary: ix.summary,
      // Interaction bodies are agent-written markdown over prospect-controlled text.
      detail: ix.body ? <div dangerouslySetInnerHTML={{ __html: markdownToHtml(ix.body) }} /> : undefined,
    });
  }
  for (const signal of loaded.signals) {
    events.push({
      key: `${signal.id}:recorded`,
      lane: "Signals",
      at: signal.created_at,
      glyph: "◆",
      title: "Signal recorded",
      summary: signal.claim,
      detail: (
        <blockquote style={{ margin: 0, paddingLeft: "0.6rem", borderLeft: `2px solid ${tokens.border}` }}>{signal.evidence_quote}</blockquote>
      ),
    });
    if (signal.status === "withdrawn" && signal.withdrawn_at) {
      events.push({
        key: `${signal.id}:withdrawn`,
        lane: "Signals",
        at: signal.withdrawn_at,
        glyph: "◇",
        title: "Signal withdrawn",
        summary: signal.withdrawn_reason ?? signal.claim,
      });
    }
  }
  const changes: [string, FieldDef[], Record<string, unknown> | undefined, Record<string, FieldDetails> | undefined][] = [
    ["", loaded.defs.contact ?? [], loaded.contact.fields, loaded.contact.field_details],
    ["Company ", loaded.defs.organization ?? [], loaded.organization?.fields, loaded.organization?.field_details],
  ];
  for (const [prefix, defs, values, details] of changes) {
    for (const def of defs) {
      const info = details?.[def.key];
      if (!info?.at || values?.[def.key] === undefined) continue;
      events.push({
        key: `${prefix}${def.key}`,
        lane: HISTORY,
        at: info.at,
        glyph: "✎",
        title: `${prefix}${prefix ? def.label.toLowerCase() : def.label} set to ${show(values[def.key])}`,
        summary: [info.by ? `by ${info.by}` : null, info.why].filter(Boolean).join(" · ") || undefined,
        change: { by: info.by ?? "", why: info.why },
      });
    }
  }
  events.push({ key: "created", lane: HISTORY, at: loaded.contact.created_at, glyph: "+", title: "Added", summary: loaded.contact.source ? `from ${loaded.contact.source}` : undefined });
  return events.sort((a, b) => b.at.localeCompare(a.at));
}

/**
 * One person: everything known about them as cards on the left — about, their
 * company, signals, custom fields, notes — and their history on the right.
 */
export function PersonPage({
  contactId,
  backLabel,
  hostShowsTrail,
  onBack,
  onLoaded,
  onOpenOrganization,
  refreshKey = 0,
}: {
  contactId: string;
  /** The list this person was opened from, named on the way back. */
  backLabel: string;
  /** Whether the host's bar shows this page's place, so the page needs no back control. */
  hostShowsTrail: boolean;
  onBack: () => void;
  /** The person's name once read, for the trail the app reports. */
  onLoaded: (name: string) => void;
  onOpenOrganization: (id: string) => void;
  /** A change re-reads the person in place, keeping any form open on the page. */
  refreshKey?: number;
}) {
  const call = useCall();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (loaded) onLoaded(loaded.contact.name);
  }, [loaded, onLoaded]);

  const load = useCallback(async (isStale: () => boolean) => {
    const [got, timeline, described] = await Promise.all([
      call<GetContactResult | ApiError>("get_contact", { contact_id: contactId }),
      readAll<Interaction>(call, "list_interactions", { contact_id: contactId }, "interactions"),
      call<DescribeFieldsResult | ApiError>("list_fields", {}),
    ]);
    if (isStale()) return;
    if (isApiError(got)) return setError(got.message ?? got.error);
    // A history that did not load is an error, never an empty timeline.
    if (isApiError(timeline)) return setError(timeline.message ?? timeline.error);
    const orgId = got.contact.organization_id;
    const [org, active, withdrawn] = orgId
      ? await Promise.all([
          call<GetOrganizationResult | ApiError>("get_organization", { organization_id: orgId }),
          call<ListSignalsResult | ApiError>("list_signals", { organization_id: orgId, limit: 50 }),
          call<ListSignalsResult | ApiError>("list_signals", { organization_id: orgId, status: "withdrawn", limit: 50 }),
        ])
      : [null, null, null];
    if (isStale()) return;
    setError(null);
    setLoaded({
      contact: got.contact,
      interactions: timeline,
      organization: org && !isApiError(org) ? org.organization : null,
      signals: [active, withdrawn].flatMap((r) => (r && !isApiError(r) ? r.signals : [])),
      defs: isApiError(described)
        ? {}
        : { contact: described.fields.contact?.custom ?? [], organization: described.fields.organization?.custom ?? [] },
    });
  }, [call, contactId]);

  // Overlapping re-reads can resolve out of order; only the latest one may land.
  useEffect(() => {
    let stale = false;
    void load(() => stale);
    return () => {
      stale = true;
    };
  }, [load, refreshKey]);

  if (error) {
    return (
      <div style={{ padding: "1rem" }}>
        <TextLink onClick={onBack}>← {backLabel}</TextLink>
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
    <PersonView
      loaded={loaded}
      backLabel={backLabel}
      hostShowsTrail={hostShowsTrail}
      onBack={onBack}
      onOpenOrganization={onOpenOrganization}
    />
  );
}

function PersonView({
  loaded,
  backLabel,
  hostShowsTrail,
  onBack,
  onOpenOrganization,
}: {
  loaded: Loaded;
  backLabel: string;
  hostShowsTrail: boolean;
  onBack: () => void;
  onOpenOrganization: (id: string) => void;
}) {
  const app = useApp();
  // The pane's own width decides the layout: the host may give this app a narrow split.
  const { ref, isNarrow: narrow } = useBreakpoint(820);
  const [modal, setModal] = useState<Modal>(null);
  const [showWithdrawn, setShowWithdrawn] = useState(false);
  const events = useMemo(() => buildTimeline(loaded), [loaded]);

  const { contact: c, organization: org, signals } = loaded;
  const live = signals.filter((s) => s.status !== "withdrawn");
  const withdrawn = signals.filter((s) => s.status === "withdrawn");

  return (
    <div ref={ref}>
      <div style={{ padding: "0.9rem 1.25rem", borderBottom: `1px solid ${tokens.border}` }}>
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: "0.7rem" }}>
              {/* Where the host shows the trail in its bar, the page keeps no back
                  control; elsewhere the arrow returns to the list this record came from. */}
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
              <Avatar name={c.name} size={36} />
              {c.name}
            </span>
          }
          status={
            <>
              <Badge tone={statusTone(c.effective_status)}>{c.effective_status}</Badge>
              {c.archived_at ? <Badge tone="neutral">archived</Badge> : null}
            </>
          }
          description={
            <>
              {c.title ?? ""}
              {c.title && org ? " at " : ""}
              {org ? <TextLink tone="default" onClick={() => onOpenOrganization(org.id)}>{org.name}</TextLink> : null}
            </>
          }
          actions={
            <Inline gap="0.5rem">
              <Button variant="secondary" size="sm" onClick={() => setModal("log")}>
                Log interaction
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setModal("dossier")}>
                Notes
              </Button>
              <OverflowMenu
                label="More actions"
                items={[
                  { label: "Edit", onSelect: () => setModal("edit") },
                  { label: "Merge", onSelect: () => setModal("merge") },
                ]}
              />
            </Inline>
          }
        />
      </div>

      <RecordLayout
        narrow={narrow}
        cards={
          <>
            <Section title="About">
              <FieldLine
                label="Email"
                details={
                  c.email_status
                    ? { by: undefined, at: c.email_checked_at ?? undefined, why: `Checked: ${c.email_status}` }
                    : undefined
                }
              >
                <EmailValue email={c.email} status={c.email_status} />
              </FieldLine>
              {c.phone ? <FieldLine label="Phone">{c.phone}</FieldLine> : null}
              {c.title ? <FieldLine label="Title">{c.title}</FieldLine> : null}
              {c.lead_source ? <FieldLine label="Lead source">{c.lead_source}</FieldLine> : null}
              {c.source ? <FieldLine label="Source">{c.source}</FieldLine> : null}
              <FieldLine label="Added">{fmtDate(c.created_at)}</FieldLine>
            </Section>

          {org ? (
                <Section title="Company" action={<TextLink style={{ fontSize: tokens.textXsSize }} onClick={() => onOpenOrganization(org.id)}>Open</TextLink>}>
                <div style={{ fontSize: tokens.textSmSize, fontWeight: 500, marginBottom: "0.25rem" }}>
                  {org.name}{" "}
                  {org.domain ? <span style={{ fontWeight: 400, color: tokens.fgFaint }}>{org.domain}</span> : null}
                </div>
                {org.industry ? <FieldLine label="Industry">{org.industry}</FieldLine> : null}
                <CustomFields defs={loaded.defs.organization ?? []} values={org.fields} details={org.field_details} />
              </Section>
            ) : null}

            <Section
              title="Signals"
              count={live.length}
              action={
                org ? (
                  <TextLink
                    style={{ fontSize: tokens.textXsSize }}
                    onClick={() => app.sendMessage(`Research new signals for the organization with id ${org.id} and record the ones that qualify.`)}
                  >
                    Find more
                  </TextLink>
                ) : null
              }
            >
              {live.length === 0 ? (
                <Text size="sm" tone="faint">
                  {org ? "No live signals for this company." : "Signals attach to a company, and this person has none."}
                </Text>
              ) : null}
              {live.map((s) => (
                <SignalCard key={s.id} signal={s} />
              ))}
              {withdrawn.length ? (
                <>
                  <TextLink onClick={() => setShowWithdrawn((s) => !s)} style={{ fontSize: tokens.textXsSize }}>
                    {showWithdrawn ? "Hide withdrawn" : `${withdrawn.length} withdrawn`}
                  </TextLink>
                  {showWithdrawn ? withdrawn.map((s) => <SignalCard key={s.id} signal={s} />) : null}
                </>
              ) : null}
            </Section>

            <Section title="Fields" count={(loaded.defs.contact ?? []).filter((d) => c.fields?.[d.key] !== undefined).length}>
              <CustomFields defs={loaded.defs.contact ?? []} values={c.fields} details={c.field_details} />
            </Section>

            <Section title="Notes" action={<TextLink style={{ fontSize: tokens.textXsSize }} onClick={() => setModal("dossier")}>Edit</TextLink>}>
              <Dossier markdown={c.dossier} />
            </Section>
          </>
        }
        timeline={<RecordTimeline events={events} lanes={LANES} />}
      />

      {modal === "edit" ? <EditContactForm contact={c} onClose={() => setModal(null)} /> : null}
      {modal === "log" ? <LogInteractionForm contactId={c.id} onClose={() => setModal(null)} /> : null}
      {modal === "dossier" ? <DossierForm kind="contact" id={c.id} current={c.dossier} onClose={() => setModal(null)} /> : null}
      {modal === "merge" ? <MergeContactForm target={c} onClose={() => setModal(null)} /> : null}
    </div>
  );
}
