"""UI resource loader — the ``ui://people/<view>`` shell.

These run WITHOUT a database on purpose: ``mcp_people.ui`` imports only the
stdlib, so this suite both locks the loader's file-read contract and proves the
security-relevant invariant that serving the UI never opens a
session or touches the database — the shell stays servable when the DB
is down. The registration test imports ``mcp_people.server`` (its engine is lazy, so
no DB is touched at import time) and asserts the MIME type the resource is served as.
"""

import asyncio
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
        assert (resource.meta or {}).get("ui") == {"csp": {}}
