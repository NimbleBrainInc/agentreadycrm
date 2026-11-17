"""People database layer.

Re-exports the SQLAlchemy ``metadata`` and table objects so callers (store,
migrations, tests) can ``from mcp_people.db import metadata, contact`` without
reaching into ``tables`` directly. Sessions come from the runtime
(:meth:`mcp_people.runtime.Runtime.session`), not from here.
"""

from __future__ import annotations

from mcp_people.db.tables import (
    contact,
    interaction,
    metadata,
    opportunity,
    organization,
    people_audit,
    uq_contact_email,
    uq_org_domain,
)

__all__ = [
    "metadata",
    "organization",
    "contact",
    "interaction",
    "opportunity",
    "people_audit",
    "uq_contact_email",
    "uq_org_domain",
]
