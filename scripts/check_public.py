"""Fail when the public tree, or a pull request's title and body, names something private.

This repository is public from its first commit, so nothing in it may name an internal host,
cloud account, registry path, private repository, internal service or deployment detail.

Usage:

    python scripts/check_public.py                 # every tracked (and new, unignored) file
    python scripts/check_public.py --event         # also the PR title and body (GitHub Actions)
    python scripts/check_public.py --text FILE...  # text from files ("-" reads stdin)

``--event`` reads the ``pull_request`` payload GitHub writes to ``$GITHUB_EVENT_PATH``; on any
other event it checks only the tree. ``--text`` checks a commit message, a review comment or a
diff before it is posted.

Each pattern below spells one character as a class (``fl[e]et``), so the source of this file
never matches its own patterns and the file is scanned like every other. Base64 runs in a file
are decoded and the decoded text is scanned too.

``scripts/public_allowlist.txt`` holds the generic uses a pattern would otherwise catch (Python's
``sys.platform``, for one). Each entry is a regular expression, preceded by a comment saying
why it is allowed. A match of an entry is removed from a line before the patterns run.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "scripts" / "public_allowlist.txt"

_I = re.IGNORECASE

#: (what it is, pattern). Case-insensitive unless the pattern turns that off.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, flags))
    for name, pattern, flags in (
        # Internal systems and deployment detail.
        ("internal system", r"\bpl[a]tform\b", _I),
        ("internal system", r"fl[e]et", _I),
        ("internal system", r"mcp-[e]dge", _I),
        ("internal system", r"\b[e]dge[ -](header|identity|proxy|gateway|verified)", _I),
        ("identity header", r"\bx-t[e]nant", _I),
        ("identity header", r"\bx-w[o]rkspace-id", _I),
        ("internal system", r"s[e]cured[ _-]?(db|database|session)", _I),
        ("internal package", r"pgb[u]lkhead", _I),
        ("internal role", r"p[e]ople_app\b", _I),
        ("SDK extra", r"\[d[b]\]|\[d[b],|\[obs[e]rvability\]", _I),
        ("tenancy", r"\bt[e]nants?\b(?!-[ab]\b)", _I),
        ("tenancy", r"\bt[e]nant-(?![ab]\b)[a-z]+", _I),
        ("operations tool", r"1p[a]ssword|\bop:/[/]", _I),
        ("operations tool", r"gr[a]fana|\bl[o]ki\b|\bpr[o]metheus\b", _I),
        ("operations tool", r"\ba[r]go(cd)?\b|\bk[8]s\b|kub[e]rnetes|kub[e]ctl", _I),
        # Other NimbleBrain servers.
        ("other server", r"[o]utbound (engine|app)|pr[e]cision[ _]outbound", _I),
        ("other server", r"\benr[i]ch\.|de[e]p_research|\bweb\.de[e]p", _I),
        # Internal specs.
        ("internal spec", r"\bS[P]EC\b|S[P]EC-[A-Za-z]+(\.md)?", 0),
        ("internal spec", r"\u00a7", 0),
        ("internal doc", r"fl[e]et-ports|F[L]EET-SERVER", _I),
        # Private repositories.
        ("private repo", r"\b[a-z0-9]+-c[l]oud\b", _I),
        ("private repo", r"nimblebrain-[o]ps|skills-[i]nternal|\bmcp-s[e]rvers\b", _I),
        ("private repo", r"\b[i]nfra\b|\bd[e]ployments\b|\bh[q]\b", _I),
        # Hosts, accounts and registries.
        ("account id", r"(?<![0-9A-Za-z])[0-9]{1}[0-9]{11}(?![0-9A-Za-z])", 0),
        ("registry", r"dkr\.[e]cr|\b[e]cr\b|am[a]zonaws", _I),
        ("host", r"\bts\.n[e]t\b|\.sv[c]\.cluster|\bmcp-st[a]ging\b", _I),
        ("host", r"[a-z0-9-]+\.n[i]mblebrain\.ai\b", _I),
        ("local path", r"/Us[e]rs/|/h[o]me/[a-z]", 0),
    )
)

#: Base64 long enough to hide a sentence; shorter runs are ids and hashes.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def load_allowlist(path: Path = ALLOWLIST) -> list[re.Pattern[str]]:
    """The allowlist's entries. Each must follow a comment line saying why it is allowed."""
    entries: list[re.Pattern[str]] = []
    commented = False
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            commented = False
        elif line.startswith("#"):
            commented = True
        else:
            if not commented:
                raise SystemExit(f"{path.name}:{number}: an entry needs a comment line above it")
            entries.append(re.compile(line, _I))
            commented = False
    return entries


def findings(text: str, allow: list[re.Pattern[str]]) -> Iterator[tuple[int, str, str]]:
    """``(line number, what, matched text)`` for each forbidden match in ``text``."""
    for number, line in enumerate(text.splitlines(), 1):
        for entry in allow:
            line = entry.sub(" ", line)
        for name, pattern in PATTERNS:
            for match in pattern.finditer(line):
                yield number, name, match.group(0)


def decoded_runs(text: str) -> Iterator[tuple[int, str]]:
    """``(line number, text)`` for each base64 run in ``text`` that decodes to UTF-8 text."""
    for number, line in enumerate(text.splitlines(), 1):
        for run in BASE64_RUN.findall(line):
            padded = run + "=" * (-len(run) % 4)
            try:
                decoded = base64.b64decode(padded, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if decoded.isprintable() or "\n" in decoded:
                yield number, decoded


def check_text(label: str, text: str, allow: list[re.Pattern[str]]) -> list[str]:
    found = [f"{label}:{n}: {what}: {hit!r}" for n, what, hit in findings(text, allow)]
    for number, decoded in decoded_runs(text):
        found += [
            f"{label}:{number}: {what} (in base64): {hit!r}"
            for _, what, hit in findings(decoded, allow)
        ]
    return found


def tree_files() -> list[Path]:
    """Every tracked file, and every new file git does not ignore."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [ROOT / name for name in sorted(set(listed.split("\0"))) if name]


def check_tree(allow: list[re.Pattern[str]]) -> list[str]:
    found: list[str] = []
    for path in tree_files():
        relative = path.relative_to(ROOT).as_posix()
        found += [
            f"path: {what}: {hit!r} in {relative}" for _, what, hit in findings(relative, allow)
        ]
        if not path.is_file():
            continue
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            continue  # binary (an image)
        found += check_text(relative, text, allow)
    return found


def check_event(allow: list[re.Pattern[str]]) -> list[str]:
    """The pull request's title and body, from the payload GitHub Actions provides."""
    if os.environ.get("GITHUB_EVENT_NAME") not in ("pull_request", "pull_request_target"):
        return []
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event.get("pull_request") or {}
    return check_text("PR title", pr.get("title") or "", allow) + check_text(
        "PR body", pr.get("body") or "", allow
    )


def check_texts(paths: Iterable[str], allow: list[re.Pattern[str]]) -> list[str]:
    found: list[str] = []
    for name in paths:
        text = sys.stdin.read() if name == "-" else Path(name).read_text("utf-8")
        found += check_text(name, text, allow)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when the tree, or a PR title and body, names something private."
    )
    parser.add_argument("--event", action="store_true", help="also check the PR title and body")
    parser.add_argument("--text", nargs="+", metavar="FILE", help="check these texts only")
    args = parser.parse_args(argv)
    allow = load_allowlist()
    if args.text:
        found = check_texts(args.text, allow)
    else:
        found = check_tree(allow) + (check_event(allow) if args.event else [])
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print(f"{len(found)} private name(s) found; see AGENTS.md", file=sys.stderr)
        return 1
    print("check_public: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
