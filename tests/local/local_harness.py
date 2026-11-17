"""Helpers for the local runtime's suites: start it the way an install does, and drive it.

Every server here is a separate process on its own SQLite file, started as ``agentreadycrm``
(``python -m mcp_people_local``) over stdio or loopback HTTP, or as the generic entry point
with ``PEOPLE_RUNTIME=local``, so the suites see exactly what a client of an installed server
sees. The process gets no ``PEOPLE_*`` setting but the ones named here: no database URL, no UI
directory.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parents[2]

#: The four views the UI serves; a host reads each as ``ui://people/<view>``.
UI_VIEWS = ("contacts", "organizations", "opportunities", "settings")


def local_env(db_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PEOPLE_")}
    return {**env, "PEOPLE_LOCAL_DB": str(db_path), **extra}


def stdio_client(db_path: Path, *, generic: bool = False) -> Client:
    """A client of ``agentreadycrm`` over stdio, or of ``python -m mcp_people.server`` with
    ``PEOPLE_RUNTIME=local`` when ``generic``."""
    if generic:
        args, env = ["-m", "mcp_people.server"], local_env(db_path, PEOPLE_RUNTIME="local")
    else:
        args, env = ["-m", "mcp_people_local"], local_env(db_path)
    return Client(StdioTransport(command=sys.executable, args=args, env=env, cwd=str(REPO)))


def token(db_path: Path) -> str:
    """The bearer token local HTTP requires for ``db_path``, read the way an installer does."""
    done = subprocess.run(
        [sys.executable, "-m", "mcp_people_local", "--print-token"],
        env=local_env(db_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.strip()


def http_client(port: int, db_path: Path) -> Client:
    """A client of ``agentreadycrm --http`` on ``port`` that presents the install's token."""
    return Client(f"http://127.0.0.1:{port}/mcp", auth=token(db_path))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def http_server(db_path: Path, **extra_env: str) -> Generator[int]:
    """``agentreadycrm --http`` on a free port; yields the port once it accepts."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_people_local", "--http"],
        env=local_env(db_path, PEOPLE_LOCAL_PORT=str(port), **extra_env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"the server exited: {proc.stdout.read() if proc.stdout else ''}"
                )
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("the server did not start listening") from None
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """Call ``tool`` and return its structured result, failing on any error result."""
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    data = result.structured_content
    assert not result.is_error, f"{tool} failed: {result.content}"
    assert isinstance(data, dict), f"{tool} returned no structured content"
    assert "error" not in data, f"{tool} returned {data}"
    assert data.get("next_step"), f"{tool} returned no next_step: {data}"
    return data


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0), f"{value} is not UTC"
    return parsed


async def read_views(client: Client) -> dict[str, str]:
    """The HTML each ``ui://people/<view>`` resource serves."""
    views = {}
    for view in UI_VIEWS:
        [content] = await client.read_resource(f"ui://people/{view}")
        views[view] = getattr(content, "text", "")
    return views


async def exercise_every_tool(client: Client) -> set[str]:
    """Drive every tool end to end on an empty database; returns the tools it called."""
    called: set[str] = set()

    async def run(tool: str, **arguments: Any) -> dict[str, Any]:
        called.add(tool)
        return await call(client, tool, **arguments)

    # Custom fields, defined before anything holds them.
    await run(
        "define_field",
        entity="organization",
        key="fit",
        label="Fit",
        type="select",
        options=["strong", "weak"],
    )
    await run("define_field", entity="contact", key="score", label="Score", type="number")
    await run("update_field", entity="contact", key="score", minimum=0, maximum=10)
    described = (await run("list_fields"))["fields"]
    assert [f["key"] for f in described["organization"]["custom"]] == ["fit"]
    assert [f["key"] for f in described["contact"]["custom"]] == ["score"]

    # Organizations and contacts.
    acme = await run(
        "find_or_create_organization",
        name="Acme Corp",
        domain="acme.example",
        industry="Software",
        fields={"fit": "strong"},
        external_refs=["vendor://companies/1"],
    )
    org_id = acme["organization"]["id"]
    again = await run("find_or_create_organization", name="ACME corp", domain="ACME.example")
    assert again["organization"]["id"] == org_id
    await run("update_organization", organization_id=org_id, region="EMEA")
    await run("update_organization", organization_id=org_id, dossier="Buys tooling.")
    assert (await run("get_organization", organization_id=org_id))["organization"]["region"] == (
        "EMEA"
    )
    ada = await run(
        "find_or_create_contact",
        name="Ada Lovelace",
        email="ada@acme.example",
        organization_id=org_id,
    )
    contact_id = ada["contact"]["id"]
    await run("update_contact", contact_id=contact_id, title="CTO", fields={"score": 7})
    await run("update_contact", contact_id=contact_id, dossier_append="Met at a conference.")
    assert (await run("get_contact", contact_id=contact_id))["contact"]["title"] == "CTO"
    found = await run("resolve_external_refs", refs=["vendor://companies/1", "vendor://x/2"])
    assert found["missing"] == ["vendor://x/2"]

    # Imports, merges and the similar-companies graph.
    imported = await run(
        "import_organizations",
        rows=[{"name": "Globex", "domain": "globex.example"}, {"name": "Initech"}],
    )
    assert imported["created"] == 2
    await run(
        "import_contacts",
        rows=[{"name": "Grace Hopper", "email": "grace@globex.example"}],
    )
    dup = await run("find_or_create_contact", name="Ada L.", email="ada.l@acme.example")
    await run("merge_contacts", target_id=contact_id, source_id=dup["contact"]["id"])
    archived = await run("archive_contact", contact_id=dup["contact"]["id"], archived=False)
    assert archived["contact"].get("archived_at") is None
    await run("archive_contact", contact_id=dup["contact"]["id"])
    initech = await run("find_or_create_organization", name="Initech")
    dupe_org = await run("find_or_create_organization", name="Initech Inc")
    await run(
        "merge_organizations",
        target_id=initech["organization"]["id"],
        source_id=dupe_org["organization"]["id"],
    )
    await run(
        "link_similar_organizations",
        organization_id=org_id,
        companies=[{"domain": "umbrella.example", "name": "Umbrella", "similarity": 0.8}],
    )
    similar = await run("list_similar_organizations", organization_id=org_id)
    [lookalike] = similar["similar"]
    assert lookalike["organization"]["status"] == "discovered"
    await run("promote_organization", organization_id=lookalike["organization"]["id"])
    await run("archive_organization", organization_id=lookalike["organization"]["id"])
    hidden = await run("list_organizations", include_archived=True)
    assert lookalike["organization"]["id"] in {o["id"] for o in hidden["organizations"]}

    # Interactions and timelines.
    logged = await run(
        "log_interaction",
        contact_id=contact_id,
        kind="email",
        summary="Intro sent",
        direction="outbound",
        idempotency_key="intro",
    )
    assert _utc(logged["interaction"]["occurred_at"])
    timeline = await run("list_interactions", contact_id=contact_id)
    assert [i["id"] for i in timeline["interactions"]] == [logged["interaction"]["id"]]
    assert (await run("list_interactions", organization_id=org_id))["interactions"]

    # Lists, paging, search and counts.
    page = await run("list_organizations", limit=1)
    assert page["next_cursor"]
    rest = await run("list_organizations", limit=1, cursor=page["next_cursor"])
    assert page["organizations"][0]["id"] != rest["organizations"][0]["id"]
    strong = await run(
        "list_contacts", filter={"field": "organization.fit", "op": "eq", "value": "strong"}
    )
    assert [c["id"] for c in strong["contacts"]] == [contact_id]
    assert (await run("list_organizations", query="acme"))["organizations"]
    counted = await run("count_records", entity="organization", field="industry")
    assert {"value": "Software", "count": 1} in counted["values"]

    # The pipeline and opportunities.
    await run("define_pipeline_stage", key="pilot", label="Pilot", probability=60)
    stages = [s["key"] for s in (await run("get_pipeline"))["stages"]]
    assert "pilot" in stages
    deal = await run(
        "create_opportunity",
        title="Acme rollout",
        organization_id=org_id,
        primary_contact_id=contact_id,
        value=12500.5,
        currency="USD",
        idempotency_key="deal",
    )
    deal_id = deal["opportunity"]["id"]
    await run("update_opportunity", opportunity_id=deal_id, probability=40)
    await run("move_opportunity", opportunity_id=deal_id, stage="pilot")
    await run("update_opportunity", opportunity_id=deal_id, dossier="Champion: Ada.")
    assert (await run("get_opportunity", opportunity_id=deal_id))["opportunity"]["stage"] == "pilot"
    assert (await run("list_opportunities"))["opportunities"]
    assert "interactions" in await run("list_interactions", opportunity_id=deal_id)
    pipeline = await run("get_pipeline")
    assert {s["key"]: s["deals"] for s in pipeline["stages"]}["pilot"] == 1
    assert pipeline["total_value"] == 12500.5
    await run("update_pipeline_stage", key="pilot", label="Paid pilot")

    # Signals and proof points.
    await run("define_signal_type", key="hiring", label="Hiring")
    assert "hiring" in {t["key"] for t in (await run("list_signal_types"))["signal_types"]}
    recorded = await run(
        "record_signal",
        organization_id=org_id,
        claim="Acme is hiring data engineers.",
        evidence_quote="We are hiring data engineers.",
        source_url="https://acme.example/careers",
        signal_type="hiring",
    )
    signal_id = recorded["signal"]["id"]
    await run("update_signal", signal_id=signal_id, add_evidence_for=["workspace"])
    await run("update_signal", signal_id=signal_id, remove_evidence_for=["workspace"])
    assert (await run("get_signal", signal_id=signal_id))["signal"]["id"] == signal_id
    assert (await run("list_signals", organization_id=org_id))["signals"]
    await run("withdraw_signal", signal_id=signal_id, reason="The posting closed.")
    await run("define_signal_type", key="funding", label="Funding")
    await run("update_signal_type", key="funding", description="New capital raised.")
    proof = await run(
        "record_proof_point",
        claim="Cut onboarding time by half.",
        organization_id=org_id,
        metric="50%",
    )
    proof_id = proof["proof_point"]["id"]
    await run(
        "verify_proof_point", proof_point_id=proof_id, verification="verified", attested_by="Ada"
    )
    assert (await run("get_proof_point", proof_point_id=proof_id))["proof_point"][
        "verification"
    ] == "verified"
    assert (await run("list_proof_points"))["proof_points"]

    # Deletion.
    gone = await run("find_or_create_contact", name="Delete Me", email="delete.me@globex.example")
    gone_id = gone["contact"]["id"]
    deleted = await run("delete_contact", contact_id=gone_id, confirm=gone_id)
    assert deleted["deleted"]["contact"] == 1
    gone_read = await client.call_tool("get_contact", {"contact_id": gone_id}, raise_on_error=False)
    assert (gone_read.structured_content or {}).get("error", {}).get("code") == "not_found"
    lone = await run("find_or_create_organization", name="Delete Co", domain="delete.example")
    lone_id = lone["organization"]["id"]
    deleted = await run("delete_organization", organization_id=lone_id, confirm=lone_id)
    assert deleted["deleted"]["organization"] == 1
    return called
