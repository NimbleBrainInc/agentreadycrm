"""The shape ``tests/golden/surface.json`` holds, built from what a client receives.

One definition for every reader of the golden: each gathers the same protocol results its own
way (an in-process client in ``test_golden_surface``) and hands them here, so every runtime is
compared under one normalization.

The server serves two skills, so the golden holds a digest of each, keyed by its URI.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).parent / "golden" / "surface.json"

#: The skill resources whose text the golden holds as a digest.
SKILL_URIS = ("skill://people/SKILL.md", "skill://people-evidence/SKILL.md")


def dump(model: Any) -> dict[str, Any]:
    """A protocol model as it goes on the wire: aliases, and no unset optional fields."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def shape(
    *,
    instructions: str | None,
    capabilities: Any,
    tools: Iterable[Any],
    resources: Iterable[Any],
    templates: Iterable[Any],
    skill_texts: Mapping[str, str],
) -> dict[str, Any]:
    """The golden's shape from protocol models (``mcp.types``), before normalization."""
    return {
        "instructions": instructions,
        "capabilities": dump(capabilities) if capabilities else {},
        "tools": sorted((dump(t) for t in tools), key=lambda t: t["name"]),
        "resources": sorted((dump(r) for r in resources), key=lambda r: r["uri"]),
        "resource_templates": sorted((dump(t) for t in templates), key=lambda t: t["uriTemplate"]),
        "skill_sha256": {
            uri: hashlib.sha256(text.encode()).hexdigest()
            for uri, text in sorted(skill_texts.items())
        },
    }


async def collect(client: Any, instructions: str | None) -> dict[str, Any]:
    """The golden's shape of the server a connected ``fastmcp.Client`` talks to.

    Lists follow every page (the client's auto-pagination). ``instructions`` is passed in, so
    each caller states where it reads them from.
    """
    skill_texts = {}
    for uri in SKILL_URIS:
        contents = await client.read_resource(uri)
        skill_texts[uri] = "".join(getattr(part, "text", "") for part in contents)
    return shape(
        instructions=instructions,
        capabilities=client.server_capabilities,
        tools=await client.list_tools(),
        resources=await client.list_resources(),
        templates=await client.list_resource_templates(),
        skill_texts=skill_texts,
    )


def normalized(surface: dict[str, Any]) -> Any:
    """``surface`` as the golden compares it: JSON types only."""
    return json.loads(json.dumps(surface))
