"""The match key functions.

No database: a key is a pure function of the value, computed by the application.
"""

from __future__ import annotations

import pytest

from mcp_people.keys import address_key, match_key

# (a, b, whether match_key holds them as one name)
_NAMES = [
    ("Émile Zola", "ÉMILE ZOLA", True),
    ("Émile Zola", "émile zola", True),
    # Decomposed É (E + combining acute) is the precomposed É after NFKC.
    ("E\u0301mile", "\u00c9mile", True),
    ("Straße GmbH", "STRASSE GMBH", True),
    ("Straße GmbH", "strasse gmbh", True),
    ("ẞ Holdings", "ss holdings", True),
    ("ﬁnance co", "Finance Co", True),
    ("ＡＣＭＥ Ｗｉｄｇｅｔｓ", "Acme Widgets", True),
    ("Σίσυφος", "ΣΊΣΥΦΟΣ", True),  # final sigma folds with capital and medial
    ("  Acme  ", "acme", True),
    # No locale tailoring: Turkish dotless ı is its own letter, and İ folds to i plus a
    # combining dot above, not to plain i.
    ("ıstanbul", "istanbul", False),
    ("İstanbul", "istanbul", False),
    ("İstanbul", "i\u0307stanbul", True),
    ("Acme", "Acme Inc", False),
]


@pytest.mark.parametrize(("a", "b", "same"), _NAMES)
def test_match_key_folds_names(a: str, b: str, same: bool) -> None:
    assert (match_key(a) == match_key(b)) is same


# (a, b, whether address_key holds them as one address)
_ADDRESSES = [
    ("Émile@Example.com", "émile@example.com", True),
    ("ＡＣＭＥ.com", "acme.com", True),
    ("ﬁ@acme.com", "fi@acme.com", True),
    # IDNA2008 keeps ß and final ς as letters: these are different hosts, and a mailbox
    # local part belongs to its server. Lowercased, never case folded.
    ("straße.de", "strasse.de", False),
    ("STRASSE.de", "strasse.de", True),
    ("straße@example.de", "strasse@example.de", False),
    ("ς.gr", "σ.gr", False),
]


@pytest.mark.parametrize(("a", "b", "same"), _ADDRESSES)
def test_address_key_lowercases_without_case_folding(a: str, b: str, same: bool) -> None:
    assert (address_key(a) == address_key(b)) is same


@pytest.mark.parametrize("fold", [match_key, address_key])
@pytest.mark.parametrize("empty", [None, "", "   ", "\u3000"])
def test_an_empty_value_has_no_key(fold, empty) -> None:
    assert fold(empty) is None
