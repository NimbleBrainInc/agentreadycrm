"""The signal types: the kinds of fact a workspace records about an account.

A type is a row of ``signal_type`` scoped to one workspace: a stable key a signal
stores in ``signal_type``, a label, and an optional description of what the type
means. ``record_signal`` takes only a key the workspace defines, so every signal
can be filtered and counted by a type somebody wrote down. There is no default
list: what counts as a signal depends on what the workspace uses them for, so each
workspace defines its own. This module holds what is pure: the vocabulary's cap and
the normalisers the store applies at the write seam.
"""

from __future__ import annotations

import re

from mcp_people.fields import FieldError

__all__ = [
    "MAX_SIGNAL_TYPES",
    "clean_signal_type_description",
    "clean_signal_type_label",
    "normalize_signal_type_key",
]

# A signal type key is a stable identifier a signal stores and a filter names.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CAP_LABEL = 64
_CAP_DESCRIPTION = 1024
MAX_SIGNAL_TYPES = 30


def normalize_signal_type_key(key: str) -> str:
    """A signal type key: lowercase letters, digits and underscores, up to 48 characters."""
    clean = (key or "").strip().lower()
    if not _KEY_RE.match(clean):
        raise FieldError(
            f"signal type key {key!r} must be lowercase letters, digits and underscores, "
            "starting with a letter, up to 48 characters",
            next_step="Use a key like 'hiring' or 'funding_round'.",
        )
    return clean


def clean_signal_type_label(label: str) -> str:
    clean = (label or "").strip()
    if not clean:
        raise ValueError("label must not be empty")
    if len(clean.encode("utf-8")) > _CAP_LABEL:
        raise ValueError(f"label exceeds {_CAP_LABEL} bytes")
    return clean


def clean_signal_type_description(description: str | None) -> str | None:
    """What the type means, for the agent deciding whether a fact is one; empty is none."""
    if description is None:
        return None
    clean = description.strip()
    if len(clean.encode("utf-8")) > _CAP_DESCRIPTION:
        raise ValueError(f"description exceeds {_CAP_DESCRIPTION} bytes")
    return clean or None
