import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  Button,
  Divider,
  Heading,
  Inline,
  ListRow,
  Spinner,
  Stack,
  Text,
} from "@nimblebrain/synapse/ui";
import { readAll, useCall } from "../api";
import { fmtDate } from "../format";
import { CustomFields } from "../people/pagekit";
import {
  type ApiError,
  type Contact,
  type DescribeFieldsResult,
  type FieldDef,
  type GetOrganizationResult,
  type Interaction,
  isApiError,
  type OpenDeal,
  type Organization,
} from "../types";

/** What the page shows: the organization, its custom fields, its open deals, its people and its history. */
interface OrganizationView {
  organization: Organization;
  defs: FieldDef[];
  deals: OpenDeal[];
  dealsTotal: number;
  contacts: Contact[];
  interactions: Interaction[];
}
import { FieldRow, Timeline } from "./kit";
import { EditOrganizationForm } from "./forms";
import { OpenDeals } from "./OpenDeals";
import { SimilarCompanies } from "./SimilarCompanies";

type Modal = null | "edit";

export function OrganizationDetail({
  organizationId,
  onNavigate,
  onLoaded,
  openInApp,
  refreshKey = 0,
}: {
  organizationId: string;
  onNavigate: (id: string) => void;
  /** The organization's name once read, for the trail the app reports. */
  onLoaded?: (name: string) => void;
  /** The control that opens this organization in the app's own page, where the host offers one. */
  openInApp?: ReactNode;
  /** A change re-reads the organization in place, keeping any form open on the page. */
  refreshKey?: number;
}) {
  const call = useCall();
  const [data, setData] = useState<OrganizationView | null>(null);
  useEffect(() => {
    if (data) onLoaded?.(data.organization.name);
  }, [data, onLoaded]);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<Modal>(null);

  const load = useCallback(async (isStale: () => boolean) => {
    const [res, described, people, history] = await Promise.all([
      call<GetOrganizationResult | ApiError>("get_organization", { organization_id: organizationId }),
      call<DescribeFieldsResult | ApiError>("list_fields", { entity: "organization" }),
      readAll<Contact>(call, "list_contacts", { organization_id: organizationId }, "contacts"),
      readAll<Interaction>(call, "list_interactions", { organization_id: organizationId }, "interactions"),
    ]);
    if (isStale()) return;
    // A roster or history that did not load is an error, never an empty collection.
    if (isApiError(res) || isApiError(people) || isApiError(history)) {
      const [failed] = [res, people, history].filter(isApiError);
      setError(failed.message ?? failed.error);
      setData(null);
      return;
    }
    setError(null);
    setData({
      organization: res.organization,
      // Fields that did not load leave the card with none to edit, not the page unreadable.
      defs: isApiError(described) ? [] : (described.fields?.organization?.custom ?? []),
      deals: res.open_opportunities ?? [],
      dealsTotal: res.open_opportunities_total ?? 0,
      contacts: people,
      interactions: history,
    });
  }, [call, organizationId]);

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
      <Text size="sm" tone="danger">
        {error}
      </Text>
    );
  }
  if (!data) {
    return (
      <Inline gap="0.5rem">
        <Spinner />
        <Text size="sm" tone="muted">
          Loading…
        </Text>
      </Inline>
    );
  }

  const o = data.organization;

  return (
    <Stack gap="1.25rem">
      <Stack gap="0.3rem">
        <Heading size="md">{o.name}</Heading>
        {o.domain ? (
          <Text size="sm" tone="muted">
            {o.domain}
          </Text>
        ) : null}
      </Stack>

      <Inline gap="0.5rem" wrap>
        <Button variant="ghost" size="sm" onClick={() => setModal("edit")}>
          Edit
        </Button>
        {openInApp}
      </Inline>

      <Stack gap="0.5rem">
        <FieldRow label="Website">
          {o.website ? <Text size="sm">{o.website}</Text> : null}
        </FieldRow>
        <FieldRow label="Industry">
          {o.industry ? <Text size="sm">{o.industry}</Text> : null}
        </FieldRow>
        <FieldRow label="Region">
          {o.region ? <Text size="sm">{o.region}</Text> : null}
        </FieldRow>
        <FieldRow label="Source">
          {o.source ? <Text size="sm">{o.source}</Text> : null}
        </FieldRow>
        <FieldRow label="Added">
          <Text size="sm" tone="muted">
            {fmtDate(o.created_at)}
          </Text>
        </FieldRow>
      </Stack>

      <Divider />

      <Stack gap="0.5rem">
        <Heading size="sm">Deals ({data.dealsTotal})</Heading>
        <OpenDeals deals={data.deals} total={data.dealsTotal} onOpen={onNavigate} />
      </Stack>

      <Divider />

      <Stack gap="0.5rem">
        <Heading size="sm">Fields</Heading>
        <div>
          <CustomFields
            defs={data.defs}
            values={o.fields}
            details={o.field_details}
            edit={{ kind: "organization", id: o.id }}
          />
        </div>
      </Stack>

      <Divider />

      <Stack gap="0.5rem">
        <Heading size="sm">People ({data.contacts.length})</Heading>
        {data.contacts.length === 0 ? (
          <Text size="sm" tone="faint">
            No contacts at this organization yet.
          </Text>
        ) : (
          <Stack gap="0.1rem">
            {data.contacts.map((contact) => (
              <ListRow
                key={contact.id}
                interactive
                onClick={() => onNavigate(contact.id)}
                title={contact.name}
                meta={contact.title ?? undefined}
              />
            ))}
          </Stack>
        )}
      </Stack>

      <Divider />

      <Stack gap="0.75rem">
        <Heading size="sm">Timeline</Heading>
        <Timeline interactions={data.interactions} />
      </Stack>

      <Divider />

      <SimilarCompanies organizationId={o.id} domain={o.domain} />

      {modal === "edit" ? (
        <EditOrganizationForm
          organization={o}
          onClose={() => setModal(null)}
        />
      ) : null}
    </Stack>
  );
}
