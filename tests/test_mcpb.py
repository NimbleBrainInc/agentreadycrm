"""The local MCP bundle in ``mcpb/`` runs this distribution, at this version, on the local runtime.

The bundle carries no code: its ``pyproject.toml`` pins the distribution, and the manifest
starts the local command with ``uv run``. Its tool list is the registered tools, and its only
host metadata is its trust declaration.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from mcp_people import server

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
BUNDLE_TOML = tomllib.loads((ROOT / "mcpb" / "pyproject.toml").read_text())
BUNDLE = BUNDLE_TOML["project"]
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())
MANIFEST = json.loads((ROOT / "mcpb" / "manifest.json").read_text())


def test_the_bundle_names_the_distribution_version_everywhere() -> None:
    version = PACKAGE["version"]
    assert MANIFEST["version"] == version
    assert BUNDLE["version"] == version
    assert BUNDLE["dependencies"] == [f"{PACKAGE['name']}=={version}"]


def test_the_bundle_holds_the_sdk_to_the_locked_version() -> None:
    """The bundle is locked at tag time, so it takes the SDK release CI and the goldens ran."""
    (locked,) = [p["version"] for p in LOCK["package"] if p["name"] == "nimblebrain-server-sdk"]
    constraints = BUNDLE_TOML["tool"]["uv"]["constraint-dependencies"]
    assert constraints == [f"nimblebrain-server-sdk=={locked}"]


def test_the_bundle_is_agent_ready_crm() -> None:
    assert MANIFEST["name"] == "@nimblebraininc/agentreadycrm"
    assert MANIFEST["display_name"] == "Agent Ready CRM"


def test_the_bundle_runs_the_local_command_with_uv_from_its_lock() -> None:
    server_entry = MANIFEST["server"]
    assert server_entry["type"] == "uv"
    config = server_entry["mcp_config"]
    assert config["command"] == "uv"
    assert config["args"] == [
        "run",
        "--directory",
        "${__dirname}",
        "--frozen",
        "--no-dev",
        "python",
        "-m",
        "mcp_people_local",
    ]
    assert config["env"] == {
        "PEOPLE_RUNTIME": "local",
        "PEOPLE_LOCAL_DB": "${user_config.database_path}",
    }
    assert set(MANIFEST["user_config"]) == {"database_path"}
    assert PACKAGE["scripts"]["agentreadycrm"] == "mcp_people_local.__main__:main"


def test_the_bundle_python_range_matches_its_project() -> None:
    assert MANIFEST["compatibility"]["runtimes"]["python"] == BUNDLE["requires-python"]
    assert PACKAGE["requires-python"] == ">=3.13"


async def test_the_bundle_publishes_exactly_the_registered_tools() -> None:
    published = [t["name"] for t in MANIFEST["tools"]]
    registered = {t.name for t in await server.mcp.list_tools()}
    assert len(published) == len(set(published))
    assert set(published) == registered


def test_the_bundle_places_nothing_in_a_nimblebrain_host() -> None:
    assert "ai.nimblebrain/host" not in json.dumps(MANIFEST)
    assert set(MANIFEST["_meta"]) == {"org.mpaktrust"}


async def test_the_server_reports_the_package_version() -> None:
    """serverInfo carries the installed distribution's version, the one the bundle pins."""
    from importlib.metadata import version

    from fastmcp import Client

    async with Client(server.mcp) as client:
        info = client.server_info
    assert info is not None
    assert info.version == version(PACKAGE["name"]) == PACKAGE["version"]
