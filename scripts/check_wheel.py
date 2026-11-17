"""Check what the built wheel carries: the files read at runtime, and the built UI.

Run on a directory holding one ``agentreadycrm`` wheel (``uv build --wheel``). The server reads
its skills, the local schema history and the built UI from inside its packages, so a wheel
without one of them installs a server that fails or serves a placeholder.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The built UI inside the wheel (``hatch_build.py``).
UI_INDEX = "mcp_people/ui_dist/index.html"
#: The only top-level directories a wheel may hold besides its ``.dist-info``.
PACKAGES = {"mcp_people", "mcp_people_local"}
ENTRY_POINTS = (
    "agentreadycrm = mcp_people_local.__main__:main",
    "local = mcp_people_local.runtime:LocalRuntime",
)


def required() -> set[str]:
    """Every skill file and every local migration in the source tree, and the built UI."""
    src = ROOT / "src"
    skills = {str(p.relative_to(src)) for p in (src / "mcp_people").rglob("*.md")}
    history = src / "mcp_people_local" / "migrations"
    migrations = {
        str(p.relative_to(src))
        for p in history.rglob("*")
        if p.is_file() and p.suffix in (".py", ".mako")
    }
    return skills | migrations | {UI_INDEX, "mcp_people_local/runtime.py"}


def check(wheel_path: Path) -> list[str]:
    wheel = zipfile.ZipFile(wheel_path)
    names = set(wheel.namelist())
    failures = [f"missing {f}" for f in sorted(required() - names)]
    if UI_INDEX in names and b"<html" not in wheel.read(UI_INDEX).lower():
        failures.append(f"{UI_INDEX} is not the built UI")
    tops = {n.split("/", 1)[0] for n in names if "/" in n and ".dist-info" not in n}
    failures += [f"carries {t}" for t in sorted(tops - PACKAGES)]
    [entry_points] = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
    declared = wheel.read(entry_points).decode()
    failures += [f"declares no entry point {e!r}" for e in ENTRY_POINTS if e not in declared]
    return [f"{wheel_path.name}: {f}" for f in failures]


def main(dist: str) -> int:
    wheels = sorted(Path(dist).glob("agentreadycrm-*.whl"))
    if len(wheels) != 1:
        print(f"expected one agentreadycrm-*.whl in {dist}, found {len(wheels)}", file=sys.stderr)
        return 1
    failures = check(wheels[0])
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print(f"{wheels[0].name}: OK ({len(required())} required files)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
