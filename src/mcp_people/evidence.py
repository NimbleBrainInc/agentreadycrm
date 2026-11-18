"""Evidence vocabularies and source-URL shape validation.

The evidence contract these serve: *a claim that cites a fact is a reference to a
record, and a record carries its verification; prose alone is never evidence.* A
record can only carry its verification if what it is checked against is fixed, so
unlike ``status`` / ``kind`` / ``stage``, the evidence vocabularies are **closed
at the application layer and rejected on write**.

That is a deliberate divergence from the bundle's "open strings, graceful
fallthrough" rule, and it stops exactly at the boundary the rule was written for.
The rule exists so a consumer inventing a lifecycle state cannot force a People
migration. These sets keep that property — the columns stay open
``Text`` with no ``CHECK`` and no DB enum, so widening a vocabulary is an edit to
a frozenset here, not a migration. What they add is that an unrecognized value is
an ``invalid_request`` at the write seam rather than a stored row nobody can read.

A signal's TYPE is not here: each workspace defines its own vocabulary as rows
(:mod:`mcp_people.signal_types`), because what counts as a signal depends on what
the workspace uses them for. A signal carries no grade: judging a fact against what
it is used for belongs to the tool that uses it.

:data:`SIGNAL_STATUSES` is the one lifecycle here and is closed for a different
reason: it has exactly two states and a transition verb (``withdraw_signal``) that
owns the move. There is no delete — a signal that turns out to be wrong is
withdrawn, so the record of having believed it survives.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "ACTIVE",
    "ANY_STATUS",
    "ATTRIBUTIONS",
    "FABRICATED_PLACEHOLDER",
    "MAX_PARTIES",
    "UNSTATED",
    "WORKSPACE",
    "QUOTE_HEDGES",
    "SIGNAL_STATUSES",
    "UNVERIFIED",
    "VERIFICATION_STATES",
    "VERIFIED",
    "WITHDRAWN",
    "dropped_hedge",
    "normalize_source_url",
    "parse_parties",
    "validate_choice",
]

ACTIVE = "active"
WITHDRAWN = "withdrawn"

# Signal lifecycle. Two states and a transition verb; there is no delete.
SIGNAL_STATUSES: frozenset[str] = frozenset({ACTIVE, WITHDRAWN})

# ``list_signals`` reads either state with this; it is never stored.
ANY_STATUS = "any"

# Whom a signal is evidence for. On the wire a party is
# ``WORKSPACE`` (the workspace's own pipeline, stored as a NULL organization) or an
# organization id. ``UNSTATED`` filters a listing to signals with no live party. Both
# words are reserved: neither is ever read as an organization id.
WORKSPACE = "workspace"
UNSTATED = "unstated"
_RESERVED_PARTIES = frozenset({WORKSPACE, UNSTATED})

# How many parties one call may name.
MAX_PARTIES = 10

VERIFIED = "verified"
UNVERIFIED = "unverified"
FABRICATED_PLACEHOLDER = "fabricated-placeholder"

# Proof-point verification. ``fabricated-placeholder`` is a LEGITIMATE state, not
# an error: copy gets workshopped against a placeholder outcome, and the set of
# placeholder records is the punch list of case studies the business still has to
# obtain. Marking it is what keeps it from being mistaken for evidence later.
VERIFICATION_STATES: frozenset[str] = frozenset({VERIFIED, UNVERIFIED, FABRICATED_PLACEHOLDER})


def validate_choice(field: str, value: str, allowed: Iterable[str]) -> str:
    """Trim ``value`` and assert it is in ``allowed``; return the clean value.

    Raises ``ValueError`` — which the tool layer maps to the structured
    ``invalid_request`` shape — naming the field and listing the accepted values,
    so a caller that guessed a vocabulary is told the real one in the same turn.
    """
    clean = value.strip() if value else ""
    if clean not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{field} must be one of: {options}")
    return clean


def parse_parties(evidence_for: Sequence[str], *, required: bool) -> list[str | None]:
    """The parties a write names, each as the organization id it stores (``None`` for the
    workspace), deduplicated in the order given.

    Raises ``ValueError`` (``invalid_request``) for more than :data:`MAX_PARTIES`, an empty
    entry, ``"unstated"`` (a filter, never a party), and, when ``required``, an empty list.
    ``"workspace"`` in any case is the workspace. Anything else is an organization id,
    which the store validates in scope.
    """
    if isinstance(evidence_for, str):
        raise ValueError(
            'evidence_for is a list of parties: ["workspace"], [<organization id>], or both'
        )
    if required and not evidence_for:
        raise ValueError(
            'evidence_for must name at least one party: "workspace" or an organization id'
        )
    if len(evidence_for) > MAX_PARTIES:
        raise ValueError(f"evidence_for names at most {MAX_PARTIES} parties per call")
    parties: list[str | None] = []
    for entry in evidence_for:
        clean = entry.strip() if isinstance(entry, str) else ""
        if not clean:
            raise ValueError('each evidence_for entry is "workspace" or an organization id')
        word = clean.lower()
        if word in _RESERVED_PARTIES and word != WORKSPACE:
            raise ValueError(
                f'"{word}" is not a party: name "workspace" or an organization id. It is a '
                "list_signals filter only"
            )
        party = None if word == WORKSPACE else clean
        if party not in parties:
            parties.append(party)
    return parties


# Shapes we accept for a source URL. Only the two web schemes: a signal's source
# has to be something a reviewer can open and read the quote in.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def normalize_source_url(url: str) -> str:
    """Validate a source URL's SHAPE and return its normalized form.

    Shape only — nothing here fetches the URL or asserts the page exists. What it
    guarantees is that the stored value is openable and comparable: an http(s)
    scheme, a host, no embedded whitespace. That is what makes ``source_url`` a
    usable half of the ``(organization, source_url, signal_type)`` dedupe key and
    what stops ``"see their blog"`` from being stored as attestation.

    Normalization is deliberately shallow — lowercase the scheme and host (both
    case-insensitive per RFC 3986) and drop the fragment (it addresses a position
    within a document, not a different document). Path, query, and their casing
    are preserved: they are case-sensitive on most servers, and rewriting them
    would change which page the record points at.

    Raises ``ValueError`` (→ ``invalid_request``) on any other shape.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("source_url must not be empty")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("source_url must not contain whitespace")
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:  # malformed enough that the parser refuses it
        raise ValueError("source_url is not a valid URL") from exc
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("source_url must be an http:// or https:// URL")
    if not parts.netloc:
        raise ValueError("source_url must include a host")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


# A hedge in the source is part of the fact: "a blog alleges a ransomware attack" and "Acme
# was hit by ransomware" are different claims, and only the first is what the source says.
# These are the words and phrases that mark whether an event HAPPENED as unsettled, one
# entry each, matched case-insensitively as whole words with any whitespace between words.
# Intent ("plans to") is not here: its wording varies too much for a word match to tell a
# kept plan from a dropped one. Nor are "potential" and "likely": business copy uses both
# for markets and customers far more often than for events, and this check refuses the write.
# `tests/test_evidence_validation.py` holds a quote for every entry and fails on one without.
QUOTE_HEDGES: tuple[str, ...] = (
    r"alleg(?:ed|edly|es|ations?)",
    r"reportedly",
    r"purported(?:ly)?",
    r"suspected",
    r"rumou?r(?:ed|s)?",
    r"unconfirmed",
    r"unverified",
    r"unsubstantiated",
    # "Possible" and "apparent" hedge only as the adjective on a thing ("a possible breach",
    # "reports of possible fraud", "an apparent attack"). Elsewhere they carry no doubt:
    # "where possible", "making it possible", "it became apparent that".
    r"(?:a|an|the|of|another)\s+possible",
    r"possibly",
    r"potentially",
    r"(?:an|the)\s+apparent",
    r"apparently",
    r"presumabl[ey]",
    r"(?:may|might|could)\s+have",
    r"(?:believed|said|understood)\s+to\s+(?:be|have)",
    r"thought\s+to\s+be",
    r"(?:appears?|seems?)\s+to",
)

# What keeps a claim hedged: one of the words above, or an attribution that says whose account
# it is. An attribution is a shape, not a word: "a blog reports a breach" names a source, and
# "Acme's insurance claims system" or "earnings reports show" does not, though each contains
# a reporting word. So a reporting verb counts only where a clause, an object, or a name
# follows it ("TechCrunch said Acme laid off …"); the name is matched by its capital letter.
ATTRIBUTIONS: tuple[str, ...] = (
    r"according\s+to",
    r"per\s+(?-i:[A-Z])\w*",
    r"as\s+reported",
    r"(?:reported|claimed|alleged|described)\s+by",
    r"(?:reports?|reported|says|said|claims?|claimed|states?|stated|writes|wrote|told|"
    r"asserts?|asserted|suggests?|suggested)\s+(?:that|a|an|the|it|its|they|their|he|she|"
    r"his|her|there)",
    r"(?:reports?|reported|says|said|claims?|claimed|states?|stated|writes|wrote|told)"
    r"\s+(?-i:[A-Z])\w*",
)


def _compile(entries: Sequence[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(entries) + r")\b", re.IGNORECASE)


_QUOTE_HEDGE = _compile(QUOTE_HEDGES)
_CLAIM_HEDGE = _compile((*QUOTE_HEDGES, *ATTRIBUTIONS))


def dropped_hedge(*, claim: str, evidence_quote: str) -> str | None:
    """The hedge word the quote carries and the claim drops, or ``None``.

    Whole words only. A quote with no hedge passes whatever the claim says: a source that
    states an incident flatly is not caught here, and the outbound bundle marks incident
    copy at approval for that reason.
    """
    found = _QUOTE_HEDGE.search(evidence_quote)
    if found is None or _CLAIM_HEDGE.search(claim) is not None:
        return None
    return found.group(0)
