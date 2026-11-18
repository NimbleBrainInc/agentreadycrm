"""The core and the local runtime load no Postgres driver and no optional SDK module.

Importing the core (``mcp_people``, its server, runtime contract, store and tables) or the local
runtime (``mcp_people_local``) loads no ``asyncpg`` and none of the SDK's optional database
(``nimblebrain_server_sdk.db``) or observability (``nimblebrain_server_sdk.observability``)
modules: none is a dependency of this package. Nor does binding the local runtime and building
its HTTP app. The SDK core is allowed. Each check runs in a fresh interpreter, so this suite's
own imports cannot hide a load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN = ("asyncpg", "nimblebrain_server_sdk.db", "nimblebrain_server_sdk.observability")

CORE_IMPORTS = (
    "mcp_people",
    "mcp_people.server",
    "mcp_people.runtime",
    "mcp_people.db",
    "mcp_people.db.store",
    "mcp_people.db.tables",
    "mcp_people.models",
    "mcp_people.fields",
    "mcp_people.query",
    "mcp_people.ui",
)

LOCAL_IMPORTS = (
    "mcp_people_local",
    "mcp_people_local.__main__",
    "mcp_people_local.auth",
    "mcp_people_local.config",
    "mcp_people_local.http",
    "mcp_people_local.runtime",
    "mcp_people_local.schema",
    "mcp_people_local.sqlite",
)


def _loaded_after(statement: str) -> list[str]:
    probe = f"import json, sys\n{statement}\nprint(json.dumps(sorted(sys.modules)))"
    env = {k: v for k, v in os.environ.items() if k != "PEOPLE_RUNTIME"}
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=env,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _forbidden(modules: list[str]) -> list[str]:
    return [m for m in modules if any(m == f or m.startswith(f"{f}.") for f in FORBIDDEN)]


@pytest.mark.parametrize("module", CORE_IMPORTS + LOCAL_IMPORTS)
def test_a_core_or_local_import_loads_nothing_private(module: str) -> None:
    assert _forbidden(_loaded_after(f"import {module}")) == []


def test_serving_the_local_runtime_loads_nothing_private(tmp_path: Path) -> None:
    statement = (
        "from pathlib import Path\n"
        "from mcp_people import server\n"
        "from mcp_people_local.runtime import LocalRuntime\n"
        f"server.install(LocalRuntime(Path({str(tmp_path / 'crm.db')!r})))\n"
        "server.app\n"
    )
    loaded = _loaded_after(statement)
    assert "mcp_people_local.runtime" in loaded
    assert _forbidden(loaded) == []


def test_the_probe_sees_what_a_statement_loads() -> None:
    # The checks above are only meaningful if the probe sees a module load, and the filter
    # matches a forbidden module and its submodules.
    assert "mcp_people.db.store" in _loaded_after("import mcp_people.server")
    assert _forbidden(["asyncpg", "asyncpg.connection", "nimblebrain_server_sdk.db.engine"]) == [
        "asyncpg",
        "asyncpg.connection",
        "nimblebrain_server_sdk.db.engine",
    ]
    assert _forbidden(["asyncpgx", "nimblebrain_server_sdk.dbx"]) == []
