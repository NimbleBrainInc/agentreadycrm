import { useCallback, useState } from "react";
import { Button, Stack, Text, tokens } from "@nimblebrain/synapse/ui";
import { useCall } from "../api";
import { isApiError, type Contact, type Opportunity, type Organization } from "../types";
import { Field, Modal } from "./kit";

type Done = () => void;

function pruneEmpty(obj: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(obj)) {
    const trimmed = value.trim();
    if (trimmed) out[key] = trimmed;
  }
  return out;
}

/**
 * Shared submit lifecycle: call the tool, surface a structured error, or close.
 * Nothing re-reads here: People announces each committed write, and the app's
 * `useDataSync` refreshes on it.
 */
function useToolForm(onClose: Done) {
  const call = useCall();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (tool: string, args: Record<string, unknown>) => {
      setBusy(true);
      setError(null);
      try {
        const data = await call<unknown>(tool, args);
        if (isApiError(data)) {
          setError(data.message ?? data.error);
          setBusy(false);
          return;
        }
        onClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setBusy(false);
      }
    },
    [call, onClose],
  );
  return { run, busy, error };
}

function SubmitFooter({
  busy,
  label,
  onSubmit,
  onClose,
  tone = "primary",
  submitDisabled = false,
}: {
  busy: boolean;
  label: string;
  onSubmit: () => void;
  onClose: Done;
  tone?: "primary" | "danger";
  /** Disables only the primary action (e.g. form invalid). Cancel stays usable. */
  submitDisabled?: boolean;
}) {
  return (
    <>
      <Button variant="ghost" onClick={onClose} disabled={busy}>
        Cancel
      </Button>
      <Button
        variant="primary"
        onClick={onSubmit}
        disabled={busy || submitDisabled}
        style={tone === "danger" ? { background: tokens.danger } : undefined}
      >
        {busy ? "Working…" : label}
      </Button>
    </>
  );
}

function ErrorText({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <Text size="sm" tone="danger">
      {error}
    </Text>
  );
}

// --------------------------------------------------------------------------- //
// Create                                                                       //
// --------------------------------------------------------------------------- //

export function NewContactForm({ onClose }: { onClose: Done }) {
  const { run, busy, error } = useToolForm(onClose);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [title, setTitle] = useState("");
  const [phone, setPhone] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [leadSource, setLeadSource] = useState("");

  const submit = () => {
    if (!name.trim()) return;
    run(
      "find_or_create_contact",
      pruneEmpty({
        name,
        email,
        title,
        phone,
        organization_name: organizationName,
        lead_source: leadSource,
      }),
    );
  };

  return (
    <Modal
      title="New contact"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Add contact" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Name" value={name} onChange={setName} required autoFocus />
        <Field label="Email" value={email} onChange={setEmail} type="email" />
        <Field label="Title" value={title} onChange={setTitle} />
        <Field label="Phone" value={phone} onChange={setPhone} />
        <Field
          label="Organization"
          value={organizationName}
          onChange={setOrganizationName}
          placeholder="Resolved or created by name"
        />
        <Field label="Lead source" value={leadSource} onChange={setLeadSource} />
      </Stack>
    </Modal>
  );
}

export function NewOrganizationForm({ onClose }: { onClose: Done }) {
  const { run, busy, error } = useToolForm(onClose);
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [website, setWebsite] = useState("");
  const [industry, setIndustry] = useState("");
  const [region, setRegion] = useState("");

  const submit = () => {
    if (!name.trim()) return;
    run(
      "find_or_create_organization",
      pruneEmpty({ name, domain, website, industry, region }),
    );
  };

  return (
    <Modal
      title="New organization"
      onClose={onClose}
      footer={
        <SubmitFooter busy={busy} label="Add organization" onSubmit={submit} onClose={onClose} />
      }
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Name" value={name} onChange={setName} required autoFocus />
        <Field label="Domain" value={domain} onChange={setDomain} placeholder="acme.com" />
        <Field label="Website" value={website} onChange={setWebsite} />
        <Field label="Industry" value={industry} onChange={setIndustry} />
        <Field label="Region" value={region} onChange={setRegion} />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Edit                                                                          //
// --------------------------------------------------------------------------- //

export function EditContactForm({
  contact,
  onClose,
}: {
  contact: Contact;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [name, setName] = useState(contact.name ?? "");
  const [email, setEmail] = useState(contact.email ?? "");
  const [title, setTitle] = useState(contact.title ?? "");
  const [phone, setPhone] = useState(contact.phone ?? "");
  const [status, setStatus] = useState(contact.status ?? "");
  const [leadSource, setLeadSource] = useState(contact.lead_source ?? "");

  const submit = () => {
    run("update_contact", {
      contact_id: contact.id,
      ...pruneEmpty({ name, email, title, phone, status, lead_source: leadSource }),
    });
  };

  return (
    <Modal
      title="Edit contact"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Save" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Name" value={name} onChange={setName} required />
        <Field label="Email" value={email} onChange={setEmail} type="email" />
        <Field label="Title" value={title} onChange={setTitle} />
        <Field label="Phone" value={phone} onChange={setPhone} />
        <Field label="Status" value={status} onChange={setStatus} />
        <Field label="Lead source" value={leadSource} onChange={setLeadSource} />
      </Stack>
    </Modal>
  );
}

export function EditOrganizationForm({
  organization,
  onClose,
}: {
  organization: Organization;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [name, setName] = useState(organization.name ?? "");
  const [domain, setDomain] = useState(organization.domain ?? "");
  const [website, setWebsite] = useState(organization.website ?? "");
  const [industry, setIndustry] = useState(organization.industry ?? "");
  const [region, setRegion] = useState(organization.region ?? "");

  const submit = () => {
    run("update_organization", {
      organization_id: organization.id,
      ...pruneEmpty({ name, domain, website, industry, region }),
    });
  };

  return (
    <Modal
      title="Edit organization"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Save" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Name" value={name} onChange={setName} required />
        <Field label="Domain" value={domain} onChange={setDomain} />
        <Field label="Website" value={website} onChange={setWebsite} />
        <Field label="Industry" value={industry} onChange={setIndustry} />
        <Field label="Region" value={region} onChange={setRegion} />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Log interaction                                                              //
// --------------------------------------------------------------------------- //

export function LogInteractionForm({
  contactId,
  opportunityId,
  onClose,
}: {
  contactId: string;
  /** The deal the touch advances, when logged from one. */
  opportunityId?: string;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [kind, setKind] = useState("meeting");
  const [summary, setSummary] = useState("");
  const [body, setBody] = useState("");
  const [followUp, setFollowUp] = useState("");

  const submit = () => {
    if (!summary.trim()) return;
    const args: Record<string, unknown> = {
      contact_id: contactId,
      ...pruneEmpty({ kind, summary, body }),
    };
    if (opportunityId) args.opportunity_id = opportunityId;
    if (followUp.trim()) args.follow_up_at = followUp.trim();
    run("log_interaction", args);
  };

  return (
    <Modal
      title="Log interaction"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Log it" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Kind" value={kind} onChange={setKind} placeholder="meeting / email / call / note" />
        <Field label="Summary" value={summary} onChange={setSummary} required autoFocus />
        <Field label="Details" value={body} onChange={setBody} textarea />
        <Field label="Follow up at" value={followUp} onChange={setFollowUp} type="datetime-local" />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Deals                                                                         //
// --------------------------------------------------------------------------- //

const money = (text: string): number | undefined => {
  const n = Number(text.replace(/[,\s]/g, ""));
  return text.trim() && Number.isFinite(n) ? n : undefined;
};

export function NewOpportunityForm({
  organizationId,
  contactId,
  onClose,
}: {
  /** The account to open the deal on, when known. */
  organizationId?: string;
  contactId?: string;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [title, setTitle] = useState("");
  const [value, setValue] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [closeDate, setCloseDate] = useState("");

  const submit = () => {
    if (!title.trim()) return;
    const args: Record<string, unknown> = { title: title.trim(), ...pruneEmpty({ currency }) };
    if (organizationId) args.organization_id = organizationId;
    if (contactId) args.primary_contact_id = contactId;
    const amount = money(value);
    if (amount !== undefined) args.value = amount;
    if (closeDate.trim()) args.close_date = closeDate.trim();
    run("create_opportunity", args);
  };

  return (
    <Modal
      title="New deal"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Create" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Title" value={title} onChange={setTitle} required autoFocus />
        <Field label="Value" value={value} onChange={setValue} placeholder="12000" />
        <Field label="Currency" value={currency} onChange={setCurrency} />
        <Field label="Close date" value={closeDate} onChange={setCloseDate} type="date" />
        <Text size="xs" tone="muted">
          The deal enters the pipeline's first stage. Move it from its page.
        </Text>
      </Stack>
    </Modal>
  );
}

export function EditOpportunityForm({ opportunity, onClose }: { opportunity: Opportunity; onClose: Done }) {
  const { run, busy, error } = useToolForm(onClose);
  const [title, setTitle] = useState(opportunity.title);
  const [value, setValue] = useState(opportunity.value != null ? String(opportunity.value) : "");
  const [currency, setCurrency] = useState(opportunity.currency ?? "");
  // The deal's own number. Leaving it empty keeps whatever the deal has; a stage
  // move is what returns a deal to its stage's default.
  const [probability, setProbability] = useState(
    opportunity.probability_is_own && opportunity.probability != null ? String(opportunity.probability) : "",
  );
  const [closeDate, setCloseDate] = useState(opportunity.close_date ?? "");

  const submit = () => {
    if (!title.trim()) return;
    const args: Record<string, unknown> = {
      opportunity_id: opportunity.id,
      title: title.trim(),
      ...pruneEmpty({ currency, close_date: closeDate }),
    };
    const amount = money(value);
    if (amount !== undefined) args.value = amount;
    const own = Number(probability);
    if (probability.trim() && Number.isInteger(own)) args.probability = own;
    run("update_opportunity", args);
  };

  return (
    <Modal
      title="Edit deal"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Save" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Field label="Title" value={title} onChange={setTitle} required />
        <Field label="Value" value={value} onChange={setValue} />
        <Field label="Currency" value={currency} onChange={setCurrency} />
        <Field label="Probability %" value={probability} onChange={setProbability} placeholder="leave as is" />
        <Field label="Close date" value={closeDate} onChange={setCloseDate} type="date" />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Dossier                                                                       //
// --------------------------------------------------------------------------- //

export function DossierForm({
  kind,
  id,
  current,
  onClose,
}: {
  kind: "contact" | "organization" | "opportunity";
  id: string;
  current: string | null;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [dossier, setDossier] = useState(current ?? "");

  const submit = () => {
    const tool = `update_${kind}`;
    const idKey = `${kind}_id`;
    // Empty string is a valid value here — it clears the dossier.
    run(tool, { [idKey]: id, dossier });
  };

  return (
    <Modal
      title="Edit dossier"
      onClose={onClose}
      footer={<SubmitFooter busy={busy} label="Save dossier" onSubmit={submit} onClose={onClose} />}
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Text size="xs" tone="faint">
          Markdown research notes. An empty dossier clears it.
        </Text>
        <Field label="Dossier" value={dossier} onChange={setDossier} textarea rows={10} autoFocus />
      </Stack>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Merge (confirm-before-archive)                                               //
// --------------------------------------------------------------------------- //

export function MergeContactForm({
  target,
  onClose,
}: {
  target: Contact;
  onClose: Done;
}) {
  const { run, busy, error } = useToolForm(onClose);
  const [sourceId, setSourceId] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const canSubmit = sourceId.trim().startsWith("ct_") && sourceId.trim() !== target.id && confirmed;

  const submit = () => {
    if (!canSubmit) return;
    run("merge_contacts", { target_id: target.id, source_id: sourceId.trim() });
  };

  return (
    <Modal
      title="Merge duplicate"
      onClose={onClose}
      footer={
        <SubmitFooter
          busy={busy}
          submitDisabled={!canSubmit}
          label="Merge and archive"
          onSubmit={submit}
          onClose={onClose}
          tone="danger"
        />
      }
    >
      <Stack gap="0.75rem">
        <ErrorText error={error} />
        <Text size="sm">
          This <strong>keeps</strong> “{target.name}” (<code>{target.id}</code>) and{" "}
          <strong>archives</strong> the duplicate you enter below, moving its interactions and
          filling empty fields. This cannot be undone.
        </Text>
        <Field
          label="Duplicate contact id to archive"
          value={sourceId}
          onChange={setSourceId}
          placeholder="ct_…"
          autoFocus
        />
        <label style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <Text size="sm">
            I confirm “{target.name}” is kept and the other record is archived.
          </Text>
        </label>
      </Stack>
    </Modal>
  );
}
