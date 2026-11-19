"""UI resource loader — the ``ui://people/<view>`` shell.

These run WITHOUT a database on purpose: ``mcp_people.ui`` imports only the
stdlib, so this suite both locks the loader's file-read contract and proves the
security-relevant invariant that serving the UI never opens a
session or touches the database — the shell stays servable when the DB
is down. The registration test imports ``mcp_people.server`` (its engine is lazy, so
no DB is touched at import time) and asserts the MIME type the resource is served as.
"""

import asyncio
import hashlib
from pathlib import Path

import pytest

import mcp_people.ui as ui_mod
from mcp_people import query


def test_load_ui_returns_placeholder_when_unbuilt(tmp_path, monkeypatch):
    # Point at a dir with no built bundle -> the "please build" placeholder.
    monkeypatch.setattr(ui_mod, "_UI_DIR", tmp_path / "dist")
    html = ui_mod.load_ui("contacts")
    assert "not built" in html.lower()
    assert "<html" in html.lower()


def test_load_ui_returns_built_html(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    built = "<!doctype html><html lang='en'><title>People</title></html>"
    dist.joinpath("index.html").write_text(built, encoding="utf-8")
    monkeypatch.setattr(ui_mod, "_UI_DIR", dist)
    assert ui_mod.load_ui("contacts") == built  # no root element to name: returned as built


def test_load_ui_names_the_view_on_the_root(tmp_path, monkeypatch):
    """Every view is the same file with the view named on its root element, which
    is how one bundle serves the app's views and its settings section."""
    dist = tmp_path / "dist"
    dist.mkdir()
    dist.joinpath("index.html").write_text('<body><div id="root"></div></body>', encoding="utf-8")
    monkeypatch.setattr(ui_mod, "_UI_DIR", dist)
    for view in ui_mod.VIEWS:
        assert ui_mod.load_ui(view) == f'<body><div id="root" data-view="{view}"></div></body>'
    with pytest.raises(ValueError, match="unknown view"):
        ui_mod.load_ui("../etc/passwd")


def test_every_view_is_registered_as_mcp_app_html():
    from mcp_people.server import mcp

    for view in ui_mod.VIEWS:
        resource = asyncio.run(mcp.get_resource(f"ui://people/{view}"))
        assert resource is not None, f"ui://people/{view} resource is not registered"
        assert resource.mime_type == "text/html;profile=mcp-app"


def test_ui_request_limit_matches_the_servers():
    """The UI trims a list's opening columns to the limit `query` refuses past."""
    source = (Path(__file__).resolve().parents[1] / "ui/src/people/schema.ts").read_text()
    assert f"MAX_REQUEST_COLUMNS = {query.MAX_COLUMNS};" in source


def test_every_view_is_declared_as_a_self_contained_mcp_app():
    """Each view carries the MCP Apps ``_meta.ui`` FastMCP's ``app=`` publishes, with a policy
    that names no outside origin: the bundle is one file that loads nothing from elsewhere."""
    from mcp_people.server import mcp

    for view in ui_mod.VIEWS:
        resource = asyncio.run(mcp.get_resource(f"ui://people/{view}"))
        assert resource is not None
        assert (resource.meta or {}).get("ui") == {"csp": {}, "prefersBorder": True}


#: The read tools an MCP Apps host renders a view for (Claude, ChatGPT), and the view each opens.
BOUND = {
    "get_contact": "contacts",
    "list_contacts": "contacts",
    "get_organization": "organizations",
    "list_organizations": "organizations",
    "get_opportunity": "opportunities",
    "get_pipeline": "opportunities",
}

#: What ChatGPT reads on a view resource and its build template, beside ``_meta.ui``.
VIEW_META = {
    "openai/widgetDomain": "https://agentreadycrm.com",
    "ui": {"csp": {}, "prefersBorder": True},
}


def _read(uri: str):
    from mcp_people.server import mcp

    [entry] = asyncio.run(mcp.read_resource(uri)).contents
    return entry


def test_each_bound_tool_names_the_hash_of_the_view_it_serves():
    """A host renders a view only for a tool naming it in ``_meta.ui.resourceUri``. The URI is
    the view's plus the first 12 hex of the SHA-256 of exactly the HTML served, and it reads."""
    from mcp_people import server

    for name, view in BOUND.items():
        tool = asyncio.run(server.mcp.get_tool(name))
        assert tool is not None
        meta = tool.meta or {}
        served = _read(f"ui://people/{view}").content
        assert isinstance(served, str)
        key = hashlib.sha256(served.encode("utf-8")).hexdigest()[:12]
        assert meta["ui"] == {
            "resourceUri": f"ui://people/{view}/{key}",
            "visibility": ["model", "app"],
        }
        assert meta["openai/widgetAccessible"] is True
        bound = _read(meta["ui"]["resourceUri"])
        assert bound.mime_type == "text/html;profile=mcp-app"
        assert bound.content == served


def test_no_other_tool_names_a_view_and_every_tool_is_callable_from_a_view():
    """Settings is bound to no tool: its one natural read, ``list_fields``, is a lookup the
    agent makes before filtering or writing a field, where a settings editor would interrupt."""
    from mcp_people import server

    for tool in asyncio.run(server.mcp.list_tools()):
        meta = tool.meta or {}
        assert ("ui" in meta) == (tool.name in BOUND), tool.name
        assert meta["openai/widgetAccessible"] is True, tool.name


def test_every_uri_of_a_view_serves_its_html_and_chatgpts_keys():
    """The bound key, a key from an older build, and the listed URI all read the same view."""
    from mcp_people import server

    for view in ("contacts", "organizations", "opportunities"):
        uris = [
            server.bound_view_uri(view),
            f"ui://people/{view}/abcdefabcdef",
            f"ui://people/{view}",
        ]
        for uri in uris:
            entry = _read(uri)
            assert entry.mime_type == "text/html;profile=mcp-app"
            assert entry.content == server.VIEW_HTML[view]
            assert entry.meta == VIEW_META
    assert _read("ui://people/settings").meta == VIEW_META


def test_the_views_are_read_once_at_import(monkeypatch):
    """Reads serve the copies taken at import, the ones the keys hash, never a fresh read."""
    from mcp_people import server

    assert {view: ui_mod.load_ui(view) for view in ui_mod.VIEWS} == server.VIEW_HTML

    def _fail(view: str) -> str:
        raise AssertionError(f"load_ui({view!r}) called after import")

    monkeypatch.setattr(server, "load_ui", _fail)
    monkeypatch.setattr(ui_mod, "load_ui", _fail)
    for view in ui_mod.VIEWS:
        assert _read(f"ui://people/{view}").content == server.VIEW_HTML[view]


def test_the_server_is_named_for_the_product():
    from mcp_people import server

    assert server.mcp.name == "com.agentreadycrm/agentreadycrm"
