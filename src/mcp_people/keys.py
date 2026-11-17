"""The match keys that decide whether two records are the same person or company.

A key is computed here, once, by the application, and stored in a key column
(``organization.domain_key`` / ``name_key``, ``contact.email_key`` / ``name_key``).
The unique dedupe indexes, the ``ON CONFLICT`` targets and every lookup compare
those stored bytes with ``=``. Nothing calls the database's ``lower()`` to decide
identity: ``lower()`` follows the database's locale (and folds ASCII only on some
engines), so two databases would disagree about which records are one record.

Two folds, because two kinds of value are being matched:

- :func:`match_key` is for **names**. Unicode NFKC, then full case folding
  (``str.casefold``), then surrounding whitespace stripped. NFKC folds
  compatibility forms (the ``ﬁ`` ligature to ``fi``, full-width ``Ａ`` to ``A``);
  case folding is the Unicode rule for caseless matching, so ``Straße`` and
  ``STRASSE`` are one name. No locale tailoring: Turkish dotless ``ı`` stays its
  own letter, and ``İ`` folds to ``i`` plus a combining dot, not to ``i``.
- :func:`address_key` is for **emails and domains**. NFKC, then lowercase
  (``str.lower``), then stripped. Not case folding: IDNA2008 keeps ``ß`` and
  final ``ς`` as distinct letters in a domain (``straße.de`` and ``strasse.de``
  are different hosts), and a mailbox's local part belongs to its server, which
  may treat ``straße@`` and ``strasse@`` as two people. Lowercasing is the policy
  the store already applied to these values; NFKC adds the compatibility forms
  UTS #46 maps too, so a full-width address is the address.

Both return ``None`` for ``None`` or a value that is empty after folding.

A key is a function of the stored column alone, so a migration that backfills
keys carries a frozen copy of these functions rather than importing this module:
the keys a revision wrote must not change when this module does.
"""

from __future__ import annotations

import unicodedata


def match_key(value: str | None) -> str | None:
    """The key a name is matched on: NFKC, case-folded, stripped; ``None`` if empty."""
    if value is None:
        return None
    return unicodedata.normalize("NFKC", value).casefold().strip() or None


def address_key(value: str | None) -> str | None:
    """The key an email or domain is matched on: NFKC, lowercased, stripped; ``None`` if empty."""
    if value is None:
        return None
    return unicodedata.normalize("NFKC", value).lower().strip() or None
