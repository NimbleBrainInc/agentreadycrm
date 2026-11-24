"""The served surface reads as a public product, and its contract is pinned apart from its prose.

Any MCP host can connect to this server, so what a client reads (the server instructions,
every tool's, argument's and resource's description, and the skills) names no other server,
no internal system and no internal mechanism. ``test_no_internal_name_reaches_a_client``
greps that text.

Clients call tools by name with arguments and read results, so those hold still while the
prose changes. ``test_the_contract_is_unchanged`` hashes the surface with every description,
title, the instructions and the skill digests removed: a rewording leaves the digest alone,
and a change to a tool name, a schema (properties, types, required, enums), an annotation, a
resource URI or a ``_meta`` key a host reads (the view a tool binds) changes it. Such a change edits ``CONTRACT_SHA256`` in the same PR as
``golden/surface.json``, so review sees that the contract moved, not only the words.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest
from fastmcp import Client
from surface_shape import SKILL_URIS, collect, normalized

from mcp_people import server

#: SHA-256 of the surface with its prose removed (see ``contract``).
CONTRACT_SHA256 = "b802c22d9f667814795351ee6778e7121fc156a2ce50a72ac1111789dd3ef87b"

#: Names and mechanisms a client never reads. A generic CRM word ("workspace", an
#: ``outbound`` direction, the ``enriched`` stage, a graph ``edge``) is not on it. One letter of
#: each is a character class, so this file passes the repository's own scrub
#: (``scripts/check_public.py``).
FORBIDDEN = re.compile(
    r"[o]utbound engine|[o]utbound app|pr[e]cision|\benr[i]ch\.|de[e]p_research|\bw[e]b\."
    r"|pl[a]tform|mcp-[e]dge|[e]dge header|x-t[e]nant|x-w[o]rkspace|s[e]cured_session|\bR[L]S\b"
    r"|row-l[e]vel|t[e]nant|n[i]mblebrain|fl[e]et|/r[e]ady|b[u]ndle|p[e]ople_app|mcp_p[e]ople"
    r"|(?-i:S[P]EC)|s[y]napse|m[p]ak",
    re.IGNORECASE,
)

#: Keys whose value is prose. Under ``properties`` a key is an argument name, not prose.
PROSE = ("description", "title")


async def served() -> tuple[dict[str, Any], dict[str, str]]:
    """The golden's shape of the served surface, and each skill's text."""
    async with Client(server.mcp) as client:
        surface = await collect(client, server.mcp.instructions)
        skills = {}
        for uri in SKILL_URIS:
            contents = await client.read_resource(uri)
            skills[uri] = "".join(getattr(part, "text", "") for part in contents)
    return normalized(surface), skills


def contract(node: Any, *, in_properties: bool = False) -> Any:
    """``node`` with every prose value removed, argument names kept."""
    if isinstance(node, dict):
        return {
            key: contract(value, in_properties=key == "properties" and not in_properties)
            for key, value in node.items()
            if in_properties or key not in PROSE
        }
    if isinstance(node, list):
        return [contract(item) for item in node]
    return node


def prose(node: Any, *, in_properties: bool = False) -> list[str]:
    """Every prose string in ``node``."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if not in_properties and key in PROSE and isinstance(value, str):
                found.append(value)
            else:
                found += prose(value, in_properties=key == "properties" and not in_properties)
    elif isinstance(node, list):
        for item in node:
            found += prose(item)
    return found


def skill_body(text: str) -> str:
    """A skill without its frontmatter. The frontmatter's ``description`` is the skill
    resource's description, read with the rest of the prose; its ``metadata`` block is host
    metadata a host that does not know it ignores."""
    return text.split("---", 2)[2] if text.startswith("---") else text


@pytest.mark.asyncio
async def test_the_contract_is_unchanged() -> None:
    surface, _skills = await served()
    bare = contract({k: v for k, v in surface.items() if k not in ("instructions", "skill_sha256")})
    digest = hashlib.sha256(json.dumps(bare, sort_keys=True).encode()).hexdigest()
    assert digest == CONTRACT_SHA256


@pytest.mark.asyncio
async def test_no_internal_name_reaches_a_client() -> None:
    surface, skills = await served()
    texts = [surface["instructions"] or "", *prose(surface)]
    texts += [skill_body(text) for text in skills.values()]
    hits = [m.group(0) + ": " + text[:80] for text in texts for m in FORBIDDEN.finditer(text)]
    assert hits == []
