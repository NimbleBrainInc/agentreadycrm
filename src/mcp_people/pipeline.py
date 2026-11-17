"""The pipeline: the stages a workspace moves its deals through.

A stage is a row of ``pipeline_stage`` scoped to one workspace: a stable key a deal
stores in ``stage``, a label, a position, a default probability, and an outcome
(``open`` / ``won`` / ``lost``). The board's columns, the agent's moves and the
won/lost projection all read the same rows. This module holds what is pure: the
default set a workspace starts with, the vocabulary, and the normalisers the
store applies at the write seam.
"""

from __future__ import annotations

import re

from mcp_people.fields import FieldError

__all__ = [
    "DEFAULT_STAGES",
    "LOST",
    "OPEN",
    "OUTCOMES",
    "WON",
    "clean_stage_label",
    "normalize_outcome",
    "normalize_probability",
    "normalize_stage_key",
]

OPEN = "open"
WON = "won"
LOST = "lost"
OUTCOMES = (OPEN, WON, LOST)

# A stage key is a stable identifier a deal stores and a filter names.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CAP_LABEL = 64
MAX_STAGES = 30

# What a workspace starts with: the vocabulary deals already carry, so no existing
# deal is left without a column. ``(key, label, probability, outcome)`` in order.
DEFAULT_STAGES: tuple[tuple[str, str, int, str], ...] = (
    ("lead", "Lead", 10, OPEN),
    ("qualified", "Qualified", 25, OPEN),
    ("proposal", "Proposal", 50, OPEN),
    ("negotiation", "Negotiation", 75, OPEN),
    ("closed_won", "Closed won", 100, WON),
    ("closed_lost", "Closed lost", 0, LOST),
)


def normalize_stage_key(key: str) -> str:
    """A stage key: lowercase letters, digits and underscores, up to 48 characters."""
    clean = (key or "").strip().lower()
    if not _KEY_RE.match(clean):
        raise FieldError(
            f"stage key {key!r} must be lowercase letters, digits and underscores, "
            "starting with a letter, up to 48 characters",
            next_step="Use a key like 'proposal' or 'contract_sent'.",
        )
    return clean


def clean_stage_label(label: str) -> str:
    clean = (label or "").strip()
    if not clean:
        raise ValueError("label must not be empty")
    if len(clean.encode("utf-8")) > _CAP_LABEL:
        raise ValueError(f"label exceeds {_CAP_LABEL} bytes")
    return clean


def normalize_outcome(outcome: str | None) -> str:
    clean = (outcome or OPEN).strip().lower()
    if clean not in OUTCOMES:
        raise FieldError(
            f"outcome must be one of: {', '.join(OUTCOMES)}",
            next_step="An open stage holds live deals; won and lost close them.",
        )
    return clean


def normalize_probability(probability: int | None, *, default: int = 0) -> int:
    value = default if probability is None else probability
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 100):
        raise ValueError("probability must be a whole number between 0 and 100")
    return value
