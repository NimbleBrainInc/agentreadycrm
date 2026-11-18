"""The address of each record People holds that another tool may cite: a resource URI.

A contact, an organization, an opportunity, a signal and a proof point each have one, and
it is the same value everywhere it appears: the ``uri`` on every record a tool returns and
the resource this server serves at it. Another tool cites a People record by this address
and never by its id, because the address names the system that holds it and the id does
not. The first three are also the step the UI reports to the host as its location,
so the host can open the view showing them; a signal and a
proof point have no view, only the resource.

The kind is in the id's prefix (``ct_`` / ``org_`` / ``op_`` / ``sg_`` / ``pp_``,
:mod:`mcp_people.ids`), so the address is a pure function of the id. The UI parses the
three view shapes (``ui/src/App.tsx``); ``test_addresses.py`` pins them all on this side.
"""

from __future__ import annotations

from mcp_people.ids import (
    CONTACT_PREFIX,
    OPPORTUNITY_PREFIX,
    ORG_PREFIX,
    PROOF_POINT_PREFIX,
    SIGNAL_PREFIX,
)

CONTACT_URI_TEMPLATE = "people://contacts/{contact_id}"
ORGANIZATION_URI_TEMPLATE = "people://organizations/{organization_id}"
OPPORTUNITY_URI_TEMPLATE = "people://opportunities/{opportunity_id}"
SIGNAL_URI_TEMPLATE = "people://signals/{signal_id}"
PROOF_POINT_URI_TEMPLATE = "people://proof-points/{proof_point_id}"

_COLLECTION_BY_PREFIX = {
    CONTACT_PREFIX: "contacts",
    ORG_PREFIX: "organizations",
    OPPORTUNITY_PREFIX: "opportunities",
    SIGNAL_PREFIX: "signals",
    PROOF_POINT_PREFIX: "proof-points",
}


def record_uri(record_id: str) -> str | None:
    """The address of the record ``record_id`` names.

    ``None`` for an id of a kind no other tool cites (an interaction, an edge).
    """
    prefix, _, body = record_id.partition("_")
    collection = _COLLECTION_BY_PREFIX.get(prefix)
    return f"people://{collection}/{record_id}" if collection and body else None
