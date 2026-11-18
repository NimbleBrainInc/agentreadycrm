"""External references: what another system calls a People record.

A reference is an opaque URI (``vendor://companies/123``) that names an organization or a
contact somewhere else. People stores it exactly as given, compares it exactly, and never
parses it: what the scheme or the path mean is the other system's business. Within a
workspace a reference names one record, which is what lets an import match a re-pull on it.

This module holds the rules a reference must meet. No database here; the store applies them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: The longest reference, in UTF-8 bytes, the same measure as every other cap here.
MAX_REF_BYTES = 512
#: The most references one record's write may name.
MAX_REFS_PER_RECORD = 20
#: The most references one lookup may ask about.
MAX_REFS_PER_LOOKUP = 500
#: How many of a record's references a by-id read returns.
MAX_REFS_SHOWN = 100

# An absolute URI: a lower-case scheme, a colon, and no whitespace after it. A lower-case
# scheme is the URI spec's canonical form, so two spellings of one scheme cannot both be
# stored; nothing past the colon is read.
_REF = re.compile(r"^[a-z][a-z0-9+.\-]*:\S+$")

#: People's own record addresses. A record's address is already its identity here.
_OWN_SCHEME = "people:"


def clean_ref(raw: object) -> str:
    """One reference, trimmed and checked, or a ``ValueError`` saying what is wrong."""
    if not isinstance(raw, str):
        raise ValueError("an external reference is a URI string, such as vendor://companies/123")
    ref = raw.strip()
    if not _REF.match(ref):
        raise ValueError(
            f"external reference {ref[:80]!r} is not an absolute URI; give a lower-case scheme "
            "and an id, such as vendor://companies/123"
        )
    if ref.startswith(_OWN_SCHEME):
        raise ValueError("a People address is the record itself, not an external reference")
    if len(ref.encode("utf-8")) > MAX_REF_BYTES:
        raise ValueError(f"an external reference is at most {MAX_REF_BYTES} bytes")
    return ref


def clean_refs(raw: object, *, limit: int = MAX_REFS_PER_RECORD) -> list[str]:
    """A list of references, each checked, duplicates dropped in order. ``None`` is none."""
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError("external_refs is a list of URIs")
    refs: list[str] = []
    for item in raw:
        ref = clean_ref(item)
        if ref not in refs:
            refs.append(ref)
    if len(refs) > limit:
        raise ValueError(f"at most {limit} external references per call")
    return refs
