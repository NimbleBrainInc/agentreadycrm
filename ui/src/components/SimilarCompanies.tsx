import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Badge, Button, EmptyState, Heading, Inline, ListRow, Spinner, Stack, Text } from "@nimblebrain/synapse/ui";
import { useApp } from "@nimblebrain/synapse/react";
import { useCall } from "../api";
import { fmtDate } from "../format";
import {
  type ApiError,
  isApiError,
  type ListSimilarOrganizationsResult,
  type PromoteOrganizationResult,
  type SimilarCompany,
} from "../types";

/**
 * "Similar Companies" panel on the Organization detail view.
 *
 * Reads `list_similar_organizations` and promotes a row with `promote_organization`.
 * Each row's "+" promotes a discovered lookalike into the active CRM.
 *
 * **Discover asks; it does not fetch.** Finding lookalikes needs an enrichment
 * provider, and this app has no business knowing which one — there may be
 * several installed, or none. So the button states the want to the agent, which
 * resolves it against whatever is actually installed and hands the result to
 * `link_similar_organizations`. The panel then reloads on its own: People announces
 * its writes, and `useDataSync` refetches on the announcement.
 *
 * The ask carries the organization's **id**, never a firmographic string — see
 * the note on `discover`. A message from an app arrives with the user's
 * authority, so anything interpolated into it has to be something a provider
 * cannot write.
 *
 * That also means there is no outcome to report here. The ask either reaches the
 * conversation or it does not; whether enrichment exists, succeeded, or found
 * nothing is the agent's to say, in the place the user is already reading.
 *
 * Firmographic strings (name, domain, industry, ranges, headquarters) are
 * provider-sourced and attacker-influenceable, so they render as PLAIN React
 * children only — never through the markdown/HTML path.
 */
export function SimilarCompanies({
  organizationId,
  domain,
}: {
  organizationId: string;
  domain: string | null;
}) {
  const app = useApp();
  const call = useCall();
  const [similar, setSimilar] = useState<SimilarCompany[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The ask was sent. Not "discovery is running" — this app cannot see that.
  const [asked, setAsked] = useState(false);
  // A mutation failure (promote) — shown non-destructively above the
  // list, so a failed action never blows away the already-loaded rows. Distinct from
  // `error`, which is the list-load failure that replaces the whole body.
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await call<ListSimilarOrganizationsResult | ApiError>("list_similar_organizations", {
        organization_id: organizationId,
      });
      if (isApiError(res)) {
        setError(res.message ?? res.error);
        setSimilar([]);
      } else {
        setError(null);
        setSimilar(res.similar ?? []);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSimilar([]);
    } finally {
      setLoading(false);
    }
  }, [call, organizationId]);

  useEffect(() => {
    void load();
  }, [load]);

  const discover = useCallback(() => {
    if (!domain) return;
    setActionError(null);
    // The ask names the organization by id, never by domain. `sendMessage`
    // arrives as a turn with the user's authority, and the domain is a string a
    // provider can set — `link_similar_organizations` upserts organizations from
    // whatever an enrichment run returned, and the store's normalizer only
    // lowercases and trims a URL, so "acme.com. Ignore the above and …" reaches
    // the field intact. Interpolating it here would hand an enrichment provider
    // a user-authority instruction channel into the conversation.
    //
    // An id cannot carry one: it is an opaque prefixed ulid minted by the store.
    // The agent resolves it with `get_organization` if it needs the domain.
    //
    // Which server answers, and whether one is installed at all, stays the
    // agent's to work out; it links the result through `link_similar_organizations`,
    // and the announcement of that write brings the rows back here.
    app.sendMessage(
      `Find companies similar to the organization with id ${organizationId} and link them to it as lookalikes.`,
    );
    setAsked(true);
  }, [app, domain, organizationId]);

  const promote = useCallback(
    async (orgId: string) => {
      setActionError(null);
      try {
        const res = await call<PromoteOrganizationResult | ApiError>("promote_organization", {
          organization_id: orgId,
        });
        if (isApiError(res)) {
          setActionError(res.message ?? res.error);
          return;
        }
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    },
    [call],
  );

  // Shown whenever there is a domain to ask about. There is no "unavailable"
  // state to render: whether an enrichment provider is installed is not
  // something this app can see, and guessing would put a wrong answer on screen.
  const headingAction = domain ? (
    <Button variant="secondary" size="sm" onClick={discover}>
      Discover
    </Button>
  ) : null;

  let body: ReactNode;
  if (loading) {
    body = (
      <Inline gap="0.5rem">
        <Spinner />
        <Text size="sm" tone="muted">
          Loading…
        </Text>
      </Inline>
    );
  } else if (error) {
    body = (
      <Text size="sm" tone="danger">
        {error}
      </Text>
    );
  } else if (similar.length === 0) {
    body = (
      <EmptyState
        title="No similar companies yet"
        description="Ask for lookalikes from this organization's domain, then promote the ones worth working."
        action={headingAction}
      />
    );
  } else {
    body = (
      <Stack gap="0.1rem">
        {similar.map((row) => {
          const org = row.organization;
          const discoveredAt = fmtDate(row.discovered_at);
          const meta =
            [
              org.domain,
              org.industry,
              org.employee_range,
              org.revenue_range,
              org.headquarters,
              discoveredAt ? `Discovered ${discoveredAt}` : null,
            ]
              .filter(Boolean)
              .join(" · ") || undefined;
          return (
            <ListRow
              key={org.id}
              title={org.name}
              meta={meta}
              trailing={
                <Inline gap="0.4rem" align="center">
                  <Badge tone="accent">{Math.round(row.similarity * 100)}%</Badge>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Promote ${org.name} to organizations`}
                    onClick={() => void promote(org.id)}
                  >
                    +
                  </Button>
                </Inline>
              }
            />
          );
        })}
      </Stack>
    );
  }

  return (
    <Stack gap="0.75rem">
      <Inline justify="between" align="center">
        <Heading size="sm">Similar Companies</Heading>
        {headingAction}
      </Inline>
      {actionError ? (
        <Text size="sm" tone="danger">
          {actionError}
        </Text>
      ) : null}
      {asked ? (
        <Text size="sm" tone="muted">
          Asked in the chat. New lookalikes appear here once they are linked.
        </Text>
      ) : null}
      {body}
    </Stack>
  );
}
