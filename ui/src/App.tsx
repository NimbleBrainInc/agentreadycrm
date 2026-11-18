import { useCallback, useEffect, useRef, useState } from "react";
import {
  useApp,
  useDataSync,
  useModelContext,
  useToolInput,
  useToolResult,
  useTrail,
} from "@nimblebrain/synapse/react";
import { AppFrame, TextLink } from "@nimblebrain/synapse/ui";
import { NewContactForm, NewOpportunityForm, NewOrganizationForm } from "./components/forms";
import { OrganizationDetail } from "./components/OrganizationDetail";
import { type ListState, RecordList } from "./people/RecordList";
import type { Entity } from "./people/schema";
import { ImportPage } from "./people/ImportPage";
import { OpportunityPage } from "./people/OpportunityPage";
import { PersonPage } from "./people/PersonPage";
import { launchTarget } from "./launch";
import { kindOfId } from "./types";

/** What a mount of the app shows. The host places each as its own sidebar entry. */
export type View = "contact" | "organization" | "opportunity";

const PLURAL: Record<View, string> = {
  contact: "people",
  organization: "organizations",
  opportunity: "opportunities",
};

/** Each view's placement, as the host names it: the root of the trail the app reports. */
const LIST: Record<View, { id: string; label: string }> = {
  contact: { id: "ui://people/contacts", label: "Contacts" },
  organization: { id: "ui://people/organizations", label: "Organizations" },
  opportunity: { id: "ui://people/opportunities", label: "Opportunities" },
};

/**
 * A record's address: the resource URI the server stamps on it as `uri` and serves a read
 * at (`addresses.py`). The kind is in the id's prefix, so the address is the id's
 * collection plus the id. The trail reports it, and the host hands it back when the agent
 * opens People at a record it read.
 */
const COLLECTION: Record<"contact" | "organization" | "opportunity", string> = {
  contact: "contacts",
  organization: "organizations",
  opportunity: "opportunities",
};
const RECORD_URI = /^people:\/\/(contacts|organizations|opportunities)\/([a-z]+_[0-9A-Z]+)$/;

function recordUri(id: string): string {
  const kind = kindOfId(id);
  return kind ? `people://${COLLECTION[kind]}/${id}` : id;
}

/**
 * One view of People. The host chooses the view, so the app draws no tabs of its own: it
 * opens on the list its placement names, and a record opened from that list is shown in
 * place. A host that renders the view for a tool call (Claude, ChatGPT, any MCP Apps host)
 * hands it the call's input and result, and the view opens on what the call read: the record
 * a `get_*` call read, or the list searched for a `list_*` call's query (`launch.ts`).
 */
export default function App({ view }: { view: View }) {
  // A record of another kind opened from this view (a person from an
  // organization, a company from a person). It shows here, in place: the app
  // cannot move the host's navigation, and leaving the reader's list to show one
  // record would lose their place in it.
  const [openId, setOpenId] = useState<string | null>(null);
  const [detailKey, setDetailKey] = useState(0);
  const [listState, setListState] = useState<ListState | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  // The import of a file into this view's list, shown in place of the list.
  const [importing, setImporting] = useState(false);
  // The open record's name once it is read; the trail shows a placeholder until then.
  const [openLabel, setOpenLabel] = useState<string | null>(null);
  // The query the list opens searching for, from the list call the view was rendered for.
  const [search, setSearch] = useState<string | undefined>(undefined);

  // Rendered for a tool call, the view opens on what it read. It opens each record once: going
  // back to the list stays there, though the host may deliver the same result again. The
  // input can name the record before the result arrives; the result then names the same one.
  const app = useApp();
  const toolInput = useToolInput();
  const toolResult = useToolResult();
  const toolName = app.toolInfo?.tool?.name;
  const launchedFor = useRef<string | null>(null);
  useEffect(() => {
    const target = launchTarget(
      typeof toolName === "string" ? toolName : undefined,
      toolInput,
      toolResult?.structuredContent,
    );
    if (target?.search) setSearch(target.search);
    const id = target?.recordId;
    if (!id || launchedFor.current === id) return;
    launchedFor.current = id;
    setImporting(false);
    setOpenLabel(null);
    setOpenId(id);
  }, [toolName, toolInput, toolResult]);

  // People announces every write — the agent's, another view's, this one's — and the
  // view re-reads when it comes back after a while hidden, so an open record re-reads.
  // The record's page is keyed by its id alone and re-reads on `refreshKey`, in place:
  // a remount would close a form open on it and lose what the reader typed. The lists
  // re-read on their own.
  useDataSync(() => setDetailKey((k) => k + 1));

  const openKind = openId ? kindOfId(openId) : null;
  // Every view is one list over its entity's list tool; deals also draw as a board.
  const listed: Entity = view;
  const listing = !openId && !importing;

  useModelContext(
    () =>
      listing && listState
        ? {
            state: { view: PLURAL[view], ...listState },
            summary:
              listState.total === null
                ? `Browsing ${PLURAL[view]}`
                : `Browsing ${listState.total.toLocaleString()} ${PLURAL[view]}${listState.filter ? " under a filter" : ""}`,
          }
        : {
            state: { view, openId, importing },
            summary: openId
              ? `Viewing ${openId}`
              : importing
                ? `Importing a file into ${PLURAL[view]}`
                : `Browsing ${PLURAL[view]}`,
          },
    [view, openId, importing, listing, listState],
  );

  const close = useCallback(() => {
    setOpenId(null);
    setOpenLabel(null);
    setImporting(false);
  }, []);

  // Where the app is, for the host's bar: the list, then the record open over it.
  // A host that draws trails shows this and hands an id back when a level is
  // picked; the record's page then keeps no back control of its own.
  //
  // The host also hands back an address the agent read, of a record not on screen: a
  // person, a company or a deal opens here, in place, whichever view this is.
  const openAddress = useCallback(
    (id: string) => {
      if (id === LIST[view].id) return close();
      const match = RECORD_URI.exec(id);
      if (!match) return;
      if (kindOfId(match[2]) === null) return;
      // The record already open keeps its page, and so its label: nothing remounts to report it again.
      if (match[2] === openId) return;
      setImporting(false);
      setOpenLabel(null);
      setOpenId(match[2]);
    },
    [view, close, openId],
  );
  const hostShowsTrail = useTrail(
    openId
      ? [LIST[view], { id: recordUri(openId), label: openLabel ?? "" }]
      : importing
        ? [LIST[view], { id: `${LIST[view].id}#import`, label: "Import" }]
        : [LIST[view]],
    openAddress,
  );

  return (
    <>
      {
        <AppFrame contentWidth="full">
          {/* Edge to edge: the pages pad themselves, and the body's own column would
          add a second gutter around them. The pages scroll inside this one
          container, which is what the list's bar pins to. */}
          <AppFrame.Body bleed>
            <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
              {/* The list stays mounted under an open record, so going back lands on
            the same filter, sort and page. */}
              {/* Every view keeps its list mounted under an open record, so going
                  back lands where the reader was: the same filter, sort, page or board. */}
              <div hidden={!listing}>
                <RecordList
                  entity={listed}
                  search={search}
                  onOpen={setOpenId}
                  onState={setListState}
                  onNew={() => setNewOpen(true)}
                  onImport={listed === "opportunity" ? undefined : () => setImporting(true)}
                />
              </div>
              {importing && !openId && listed !== "opportunity" ? (
                <ImportPage entity={listed} onBack={close} hostShowsTrail={hostShowsTrail} />
              ) : null}
              {openId && openKind === "contact" ? (
                <PersonPage
                  key={openId}
                  refreshKey={detailKey}
                  contactId={openId}
                  backLabel={LIST[view].label}
                  hostShowsTrail={hostShowsTrail}
                  onBack={close}
                  onLoaded={setOpenLabel}
                  onOpenOrganization={setOpenId}
                />
              ) : null}
              {openId && openKind === "opportunity" ? (
                <OpportunityPage
                  key={openId}
                  refreshKey={detailKey}
                  opportunityId={openId}
                  backLabel={LIST[view].label}
                  hostShowsTrail={hostShowsTrail}
                  onBack={close}
                  onLoaded={setOpenLabel}
                  onOpen={setOpenId}
                />
              ) : null}
              {openId && openKind === "organization" ? (
                <div style={{ padding: "1rem 1.25rem" }}>
                  {hostShowsTrail ? null : <TextLink onClick={close}>← {LIST[view].label}</TextLink>}
                  <div style={{ marginTop: "0.75rem" }}>
                    <OrganizationDetail
                      key={openId}
                      refreshKey={detailKey}
                      organizationId={openId}
                      onNavigate={setOpenId}
                      onLoaded={setOpenLabel}
                    />
                  </div>
                </div>
              ) : null}
            </div>
          </AppFrame.Body>
          {newOpen && listed === "contact" ? <NewContactForm onClose={() => setNewOpen(false)} /> : null}
          {newOpen && listed === "organization" ? <NewOrganizationForm onClose={() => setNewOpen(false)} /> : null}
          {newOpen && listed === "opportunity" ? <NewOpportunityForm onClose={() => setNewOpen(false)} /> : null}
        </AppFrame>
      }
    </>
  );
}
