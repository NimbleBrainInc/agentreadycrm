"""Pure validators for the evidence records.

No database: these are the app-side gates that make the open ``Text`` columns
behave like a closed vocabulary, and the source-URL shape check that stops
``"see their blog"`` from being stored as attestation. The DB-backed halves live
in ``test_evidence_store.py``.
"""

from __future__ import annotations

import re

import pytest

from mcp_people.evidence import (
    ATTRIBUTIONS,
    QUOTE_HEDGES,
    SIGNAL_STATUSES,
    VERIFICATION_STATES,
    dropped_hedge,
    normalize_source_url,
    validate_choice,
)


def test_closed_lists_are_the_documented_vocabularies():
    """The vocabularies are the contract the tool docstrings publish; pin them so a
    quiet widening has to be a deliberate edit with a test change beside it. A
    signal's type is not here: each workspace defines its own (test_signal_types.py)."""
    assert SIGNAL_STATUSES == {"active", "withdrawn"}
    assert VERIFICATION_STATES == {"verified", "unverified", "fabricated-placeholder"}


@pytest.mark.parametrize("value", sorted(VERIFICATION_STATES))
def test_validate_choice_accepts_every_listed_value(value: str):
    assert validate_choice("verification", value, VERIFICATION_STATES) == value


def test_validate_choice_trims_before_matching():
    assert validate_choice("verification", "  verified  ", VERIFICATION_STATES) == "verified"


@pytest.mark.parametrize("value", ["vibes", "", "  ", "VERIFIED", "Verified "])
def test_validate_choice_rejects_anything_else(value: str):
    """An unrecognized value is rejected, not stored — including a case variant, so
    the stored value is always one of the documented strings."""
    with pytest.raises(ValueError) as exc:
        validate_choice("verification", value, VERIFICATION_STATES)
    # The message lists the real vocabulary, so a caller that guessed is corrected
    # in the same turn — but it never echoes what they sent.
    assert "verification must be one of" in str(exc.value)
    assert "unverified" in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM/Press/Q3", "https://example.com/Press/Q3"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("HTTP://example.com/a?b=C", "http://example.com/a?b=C"),
        ("https://example.com/a#section-2", "https://example.com/a"),
        ("https://example.com", "https://example.com"),
    ],
)
def test_normalize_source_url_normalizes_only_the_case_insensitive_parts(raw: str, expected: str):
    """Scheme and host are lowercased and the fragment dropped; path and query keep
    their casing, because they are case-sensitive on most servers and rewriting
    them would change which page the record points at."""
    assert normalize_source_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "see their blog",
        "example.com/press",
        "ftp://example.com/press",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://",
        "https://example.com/a b",
    ],
)
def test_normalize_source_url_rejects_anything_a_reviewer_cannot_open(raw: str):
    with pytest.raises(ValueError):
        normalize_source_url(raw)


# --- a claim keeps the hedges its quote carries --------------------------------------- #
# Built from the reported shape: a threat-intelligence blog alleges an attack, and the
# claim restates it as fact. Fictional company throughout.

_ALLEGED = (
    "Acme Corp is investigating what a threat-intelligence blog alleges was a ransomware "
    "attack on its billing systems."
)


@pytest.mark.parametrize(
    ("claim", "quote", "hedge"),
    [
        ("Acme was hit by ransomware in August.", _ALLEGED, "alleges"),
        ("Acme's CFO resigned.", "The CFO has reportedly resigned.", "reportedly"),
        ("Acme had a data breach.", "A suspected data breach at Acme.", "suspected"),
        ("Acme is being acquired.", "Rumors of an Acme acquisition persist.", "Rumors"),
    ],
)
def test_a_claim_that_drops_its_quotes_hedge_is_caught(claim: str, quote: str, hedge: str):
    assert dropped_hedge(claim=claim, evidence_quote=quote) == hedge


@pytest.mark.parametrize(
    "claim",
    [
        # The hedge kept, in the source's word or another.
        "A threat-intelligence blog alleges a ransomware attack on Acme's billing systems.",
        "Acme was allegedly hit by ransomware.",
        # An attribution that says whose account it is.
        "A security blog reports a ransomware attack on Acme; Acme says it is investigating.",
        "According to a security blog, Acme suffered a ransomware attack.",
    ],
)
def test_a_claim_that_keeps_the_hedge_or_names_the_source_passes(claim: str):
    assert dropped_hedge(claim=claim, evidence_quote=_ALLEGED) is None


def test_a_flat_quote_passes_whatever_the_claim_says():
    """The edge this check has, stated: a source with no hedge gives it nothing to keep.
    The outbound bundle marks incident copy at approval for exactly this case."""
    quote = "Acme Corp suffered a ransomware attack on its billing systems in August."
    assert dropped_hedge(claim="Acme was hit by ransomware.", evidence_quote=quote) is None


def test_the_hedge_is_matched_as_a_whole_word():
    # "unsuspected" and "allegro" are not hedges.
    quote = "An unsuspected surge in demand; the Allegro line sold out."
    assert dropped_hedge(claim="Demand surged.", evidence_quote=quote) is None


# --- the hedge and attribution lists: an example for every entry ------------------------ #
# Keyed by the entry itself, so a new entry without an example fails
# `test_every_list_entry_has_an_example` rather than going untested.

_QUOTE_HEDGE_EXAMPLES: dict[str, str] = {
    r"alleg(?:ed|edly|es|ations?)": "Allegations of a breach at Acme surfaced.",
    r"reportedly": "Acme reportedly lost customer records.",
    r"purported(?:ly)?": "A purported breach at Acme.",
    r"suspected": "A suspected data breach at Acme.",
    r"rumou?r(?:ed|s)?": "Acme is rumoured to be cutting staff.",
    r"unconfirmed": "Unconfirmed reports of an Acme outage.",
    r"unverified": "An unverified post claims Acme was breached.",
    r"unsubstantiated": "Unsubstantiated claims of fraud at Acme.",
    r"(?:a|an|the|of|another)\s+possible": "A possible breach at Acme is being examined.",
    r"possibly": "Acme was possibly breached in August.",
    r"potentially": "Acme potentially exposed customer data.",
    r"(?:an|the)\s+apparent": "An apparent ransomware attack hit Acme.",
    r"apparently": "Acme was apparently hit by ransomware.",
    r"presumabl[ey]": "Acme was presumably the target.",
    r"(?:may|might|could)\s+have": "Attackers may have accessed Acme's billing data.",
    r"(?:believed|said|understood)\s+to\s+(?:be|have)": "Acme is believed to have lost records.",
    r"thought\s+to\s+be": "Acme is thought to be the target.",
    r"(?:appears?|seems?)\s+to": "Acme appears to have suffered an outage.",
}

_ATTRIBUTION_EXAMPLES: dict[str, str] = {
    r"according\s+to": "According to a security blog, Acme was breached.",
    r"per\s+(?-i:[A-Z])\w*": "Per Examplewire, Acme was breached.",
    r"as\s+reported": "Acme was breached, as reported by a security blog.",
    r"(?:reported|claimed|alleged|described)\s+by": "A breach at Acme was reported by a blog.",
    (
        r"(?:reports?|reported|says|said|claims?|claimed|states?|stated|writes|wrote|told|"
        r"asserts?|asserted|suggests?|suggested)\s+(?:that|a|an|the|it|its|they|their|he|she|"
        r"his|her|there)"
    ): "A security blog writes that Acme was breached.",
    (
        r"(?:reports?|reported|says|said|claims?|claimed|states?|stated|writes|wrote|told)"
        r"\s+(?-i:[A-Z])\w*"
    ): "Examplewire reports Acme was hit by ransomware.",
}


def test_every_list_entry_has_an_example():
    assert set(_QUOTE_HEDGE_EXAMPLES) == set(QUOTE_HEDGES)
    assert set(_ATTRIBUTION_EXAMPLES) == set(ATTRIBUTIONS)


@pytest.mark.parametrize(("entry", "quote"), sorted(_QUOTE_HEDGE_EXAMPLES.items()))
def test_each_quote_hedge_is_caught_when_the_claim_drops_it(entry: str, quote: str):
    assert re.search(rf"\b(?:{entry})\b", quote, re.IGNORECASE), "example must match its entry"
    assert dropped_hedge(claim="Acme was breached.", evidence_quote=quote) is not None


@pytest.mark.parametrize(("entry", "claim"), sorted(_ATTRIBUTION_EXAMPLES.items()))
def test_each_attribution_keeps_the_hedge(entry: str, claim: str):
    assert re.search(rf"\b(?:{entry})\b", claim, re.IGNORECASE), "example must match its entry"
    assert dropped_hedge(claim=claim, evidence_quote=_ALLEGED) is None


@pytest.mark.parametrize(
    "claim",
    [
        # A reporting word that names no source is not an attribution.
        "Acme was hit by ransomware; its CEO said nothing.",
        "Acme's insurance claims system was hit by ransomware.",
        "Earnings reports show Acme was hit by ransomware.",
    ],
)
def test_a_bare_reporting_word_does_not_keep_the_hedge(claim: str):
    assert dropped_hedge(claim=claim, evidence_quote=_ALLEGED) == "alleges"


@pytest.mark.parametrize(
    "claim",
    [
        # A named source followed by the event: the most common news attribution.
        "Examplewire reports Acme was hit by ransomware.",
        "Exampletech said Acme laid off 200 staff.",
        "Per Examplewire, Acme was hit by ransomware.",
    ],
)
def test_a_named_source_keeps_the_hedge(claim: str):
    quote = "Acme was reportedly hit by ransomware."
    assert dropped_hedge(claim=claim, evidence_quote=quote) is None


@pytest.mark.parametrize(
    "quote",
    [
        "Acme wants to ship it as soon as possible.",
        "Acme promises the best possible service.",
        "The expansion was made possible by new funding.",
        "The new plant is making it possible to expand.",
        "Acme automates the work where possible.",
        "Customers can buy the highest possible tier.",
        "It became apparent that demand exceeded supply.",
    ],
)
def test_possible_or_apparent_without_doubt_is_not_a_hedge(quote: str):
    assert dropped_hedge(claim="Acme is expanding.", evidence_quote=quote) is None
