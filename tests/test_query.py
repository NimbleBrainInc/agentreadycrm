"""The filter grammar, sort, projection and counts.

One small workspace is seeded per test: three organizations with a fit and a
headcount, six contacts across them with an audience and an email status, one
live and one withdrawn signal, and two logged emails. Every assertion names the
contacts it expects, so a predicate that is merely plausible fails.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import define_signal_types, make_ctx, session_for
from pgkeyset import InvalidCursor
from pgkeyset.testing import walk

from mcp_people import query
from mcp_people.db import store
from mcp_people.fields import FieldError

pytestmark = pytest.mark.asyncio

_ORGS = {
    # name: (fit, headcount, industry)
    "Larkfield": ("Strong", 27, "Software Development"),
    "Quillstack": ("Moderate", 140, "software development"),
    "Brightforge": (None, None, "IT services"),
}
_PEOPLE = {
    # name: (organization, title, audience, email status, tags)
    "Dana": ("Larkfield", "VP of Sales", "Sales leaders", "valid", ["warm", "event"]),
    "Miles": ("Larkfield", "Founder & CEO", "Founders", "invalid", ["warm"]),
    "Priya": ("Quillstack", "Chief Revenue Officer", "Sales leaders", "valid", None),
    "Tobias": ("Quillstack", "Head of Growth", "Marketing leaders", None, None),
    "Lena": ("Brightforge", "Founder", "Founders", "risky", ["event"]),
    "Marcus": (None, "Advisor", None, None, None),
}


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _seed(ctx) -> dict[str, str]:
    """Seed the workspace; returns ``{name: id}`` for every contact and organization."""
    ids: dict[str, str] = {}
    async with session_for(ctx) as conn:
        for entity, key, field_type, options in [
            ("organization", "fit", "select", ["Strong", "Moderate"]),
            ("organization", "headcount", "number", None),
            ("contact", "audience", "select", ["Sales leaders", "Founders", "Marketing leaders"]),
            ("contact", "tags", "list", None),
            ("contact", "vip", "boolean", None),
            ("contact", "met_on", "date", None),
        ]:
            await store.define_field(
                conn, ctx, entity=entity, key=key, label=key, field_type=field_type, options=options
            )
        for name, (fit, headcount, industry) in _ORGS.items():
            fields = {k: v for k, v in (("fit", fit), ("headcount", headcount)) if v is not None}
            org, _a, _p = await store.find_or_create_organization(
                conn,
                ctx,
                name=name,
                domain=f"{name.lower()}.example",
                industry=industry,
                fields=fields,
            )
            ids[name] = org.id
        for name, (org_name, title, audience, email_status, tags) in _PEOPLE.items():
            fields = {k: v for k, v in (("audience", audience), ("tags", tags)) if v is not None}
            person, _a, _p = await store.find_or_create_contact(
                conn,
                ctx,
                name=name,
                email=f"{name.lower()}@people.example",
                title=title,
                organization_id=ids[org_name] if org_name else None,
                email_status=email_status,
                fields=fields,
            )
            ids[name] = person.id
        await store.update_contact(
            conn, ctx, contact_id=ids["Dana"], fields={"vip": True, "met_on": "2026-09-04"}
        )
        await store.update_contact(
            conn, ctx, contact_id=ids["Lena"], fields={"vip": False, "met_on": "2026-08-01"}
        )
        await define_signal_types(conn, ctx, "hiring", "announcement")
        live, _a, _s = await store.record_signal(
            conn,
            ctx,
            organization_id=ids["Larkfield"],
            claim="Halvorsen Freight posted a procurement lead role.",
            evidence_quote="We are hiring a procurement lead.",
            source_url="https://careers.halvorsen.example/procurement-lead",
            signal_type="hiring",
        )
        gone, _a, _s = await store.record_signal(
            conn,
            ctx,
            organization_id=ids["Quillstack"],
            claim="Castellane Foods announced a partner program.",
            evidence_quote="Today we announce our partner program.",
            source_url="https://castellane.example/news/partners",
            signal_type="announcement",
        )
        await store.withdraw_signal(conn, ctx, signal_id=gone.id, reason="already sent")
        when = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
        for offset, name in enumerate(("Dana", "Dana", "Priya")):
            await store.log_interaction(
                conn,
                ctx,
                contact_id=ids[name],
                kind="email",
                direction="outbound",
                summary=f"Email {offset}",
                occurred_at=when + timedelta(days=offset),
            )
    return ids


async def _names(ctx, **kwargs: Any) -> list[str]:
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, **kwargs)
    return [row.name if hasattr(row, "name") else row["name"] for row in page.rows]


def _clause(field: str, op: str, value: Any) -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


# --------------------------------------------------------------------------- #
# Filters                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        # select: case-insensitive value, canonical match
        (_clause("audience", "eq", "sales leaders"), {"Dana", "Priya"}),
        (_clause("audience", "in", ["Founders", "marketing leaders"]), {"Miles", "Lena", "Tobias"}),
        # "is not" includes the rows that hold no value
        (_clause("audience", "ne", "Founders"), {"Dana", "Priya", "Tobias", "Marcus"}),
        (_clause("audience", "not_in", ["Founders", "Sales leaders"]), {"Tobias", "Marcus"}),
        (_clause("audience", "is_set", False), {"Marcus"}),
        # core select
        (_clause("email_status", "eq", "Valid"), {"Dana", "Priya"}),
        (_clause("email_status", "in", ["invalid", "risky"]), {"Miles", "Lena"}),
        (_clause("email_status", "is_set", False), {"Tobias", "Marcus"}),
        # core text: case-insensitive equality, substring, prefix
        (_clause("title", "eq", "founder"), {"Lena"}),
        (_clause("title", "contains", "REVENUE"), {"Priya"}),
        (_clause("title", "starts_with", "founder"), {"Miles", "Lena"}),
        # a text field with a stored lower() key matches the same way, through the key
        (_clause("name", "contains", "OBI"), {"Tobias"}),
        (_clause("name", "starts_with", "ma"), {"Marcus"}),
        (_clause("organization.name", "contains", "FORGE"), {"Lena"}),
        # a list field
        (_clause("tags", "has", "event"), {"Dana", "Lena"}),
        (_clause("tags", "has", ["warm", "event"]), {"Dana", "Miles", "Lena"}),
        (_clause("tags", "has_all", ["warm", "event"]), {"Dana"}),
        (_clause("tags", "is_set", True), {"Dana", "Miles", "Lena"}),
        # boolean and date
        (_clause("vip", "eq", True), {"Dana"}),
        (_clause("vip", "eq", False), {"Lena"}),
        (_clause("met_on", "gte", "2026-09-01"), {"Dana"}),
        (_clause("met_on", "between", ["2026-08-01", "2026-08-31"]), {"Lena"}),
        # the organization's fields, reached from the contact
        (_clause("organization.fit", "eq", "strong"), {"Dana", "Miles"}),
        (_clause("organization.fit", "is_set", False), {"Lena", "Marcus"}),
        (_clause("organization.headcount", "gt", 100), {"Priya", "Tobias"}),
        (_clause("organization.headcount", "between", [20, 30]), {"Dana", "Miles"}),
        (_clause("organization.headcount", "lte", "27"), {"Dana", "Miles"}),
        (_clause("organization.name", "eq", "brightforge"), {"Lena"}),
        # an open string that holds one value in two casings
        (
            _clause("organization.industry", "eq", "Software Development"),
            {"Dana", "Miles", "Priya", "Tobias"},
        ),
        # rollups: only an active signal counts, and it belongs to the organization
        (_clause("signals.active", "gte", 1), {"Dana", "Miles"}),
        (_clause("signals.active", "eq", 0), {"Priya", "Tobias", "Lena", "Marcus"}),
        (_clause("interactions.count", "eq", 2), {"Dana"}),
        (_clause("interactions.count", "eq", 0), {"Miles", "Tobias", "Lena", "Marcus"}),
        (_clause("interactions.last_at", "gte", "2026-09-09"), {"Priya"}),
        (_clause("interactions.last_at", "is_set", False), {"Miles", "Tobias", "Lena", "Marcus"}),
    ],
    ids=lambda v: f"{v['field']}:{v['op']}" if isinstance(v, dict) else None,
)
async def test_each_operator_selects_exactly_its_rows(ctx, clause, expected) -> None:
    await _seed(ctx)
    assert set(await _names(ctx, filter=clause)) == expected


async def test_and_or_combine_and_nest(ctx) -> None:
    await _seed(ctx)
    strong_sales = {
        "and": [
            _clause("organization.fit", "eq", "Strong"),
            _clause("audience", "eq", "Sales leaders"),
        ]
    }
    assert set(await _names(ctx, filter=strong_sales)) == {"Dana"}
    either = {"or": [strong_sales, _clause("email_status", "eq", "risky")]}
    assert set(await _names(ctx, filter=either)) == {"Dana", "Lena"}
    # The legacy arguments AND with the filter.
    assert (
        set(await _names(ctx, filter=_clause("audience", "eq", "Founders"), lead_source="nobody"))
        == set()
    )


async def test_the_default_op_is_eq(ctx) -> None:
    await _seed(ctx)
    assert set(await _names(ctx, filter={"field": "audience", "value": "Founders"})) == {
        "Miles",
        "Lena",
    }


@pytest.mark.parametrize(
    ("filter_", "message", "hint"),
    [
        (_clause("audiense", "eq", "x"), "no contact field named 'audiense'", "audience"),
        (_clause("audience", "gt", "x"), "does not take the operator 'gt'", "eq, ne, in"),
        (_clause("audience", "eq", "Everyone"), "has no option 'Everyone'", "Sales leaders"),
        (_clause("organization.headcount", "gt", "many"), "compares against a number", "number"),
        (_clause("met_on", "gte", "last week"), "date as YYYY-MM-DD", "date"),
        (_clause("organization.headcount", "between", [1]), "exactly 2 values", "number"),
        (_clause("vip", "eq", "yes"), "true or false", "boolean"),
        (_clause("tags", "is_set", "yes"), "true or false with is_set", "list"),
        ({"and": []}, "non-empty list of filters", '{"field"'),
        ({"field": "audience", "equals": "x"}, "needs a field", '{"field"'),
        (["audience"], "must be an object", '{"field"'),
    ],
)
async def test_a_bad_filter_says_what_would_have_worked(ctx, filter_, message, hint) -> None:
    await _seed(ctx)
    with pytest.raises(FieldError, match=message) as caught:
        await _names(ctx, filter=filter_)
    assert hint in caught.value.next_step


async def test_archived_contacts_stay_hidden_unless_asked_for(ctx) -> None:
    ids = await _seed(ctx)
    async with session_for(ctx) as conn:
        await store.merge_contacts(conn, ctx, target_id=ids["Lena"], source_id=ids["Marcus"])
    assert "Marcus" not in await _names(ctx, filter=_clause("title", "is_set", True))
    # Naming a stage is not asking for archived contacts; include_archived is.
    assert await _names(ctx, filter=_clause("archived_at", "is_set", True)) == []
    assert await _names(
        ctx, include_archived=True, filter=_clause("archived_at", "is_set", True)
    ) == ["Marcus"]
    # The stage a filter sees is the effective one: two logged emails read `contacted`.
    assert set(await _names(ctx, filter=_clause("status", "eq", "contacted"))) == {"Dana", "Priya"}
    # Mentioning the stage is not asking for the hidden one: "not lost" is the list a
    # person works through, and a merged duplicate must not come back into it.
    assert "Marcus" not in await _names(ctx, filter=_clause("status", "ne", "lost"))
    assert "Marcus" not in await _names(
        ctx,
        filter={"or": [_clause("status", "eq", "contacted"), _clause("title", "eq", "Advisor")]},
    )
    assert await _counts(
        ctx, entity="contact", field="status", filter=_clause("status", "ne", "lost")
    ) == {
        "contacted": 2,
        "sourced": 3,
    }


async def test_unpromoted_organizations_stay_hidden_unless_asked_for(ctx) -> None:
    ids = await _seed(ctx)
    async with session_for(ctx) as conn:
        # A lookalike arrives `discovered`, hidden until promoted.
        await store.link_similar_companies(
            conn,
            ctx,
            org_id=ids["Brightforge"],
            companies=[{"domain": "look.example", "name": "Lookalike", "similarity": 0.9}],
        )

    async def names(**kwargs: Any) -> set[str]:
        async with session_for(ctx) as conn:
            page = await store.page_organizations(conn, ctx, **kwargs)
        return {row.name for row in page.rows}

    assert "Lookalike" not in await names(filter=_clause("status", "ne", "lost"))
    assert "Lookalike" not in await names(
        filter={"or": [_clause("status", "eq", "active"), _clause("name", "starts_with", "Look")]}
    )
    assert await names(filter=_clause("status", "eq", "discovered")) == {"Lookalike"}
    # The default keeps one value, so ruling that value out asks for the rest.
    assert await names(filter=_clause("status", "ne", "active")) == {"Lookalike"}
    assert await names(filter=_clause("status", "not_in", ["Active"])) == {"Lookalike"}
    assert await _counts(
        ctx, entity="organization", field="status", filter=_clause("status", "ne", "active")
    ) == {"discovered": 1}
    assert await names(filter=_clause("status", "in", ["active", "discovered"])) == {
        "Larkfield",
        "Quillstack",
        "Brightforge",
        "Lookalike",
    }


# --------------------------------------------------------------------------- #
# Sort and paging                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("name", ["Dana", "Lena", "Marcus", "Miles", "Priya", "Tobias"]),
        ("-name", ["Tobias", "Priya", "Miles", "Marcus", "Lena", "Dana"]),
        # Rows with no value sort last in either direction; ties break by id.
        ("organization.headcount", ["Dana", "Miles", "Priya", "Tobias", "Lena", "Marcus"]),
        ("-organization.headcount", ["Tobias", "Priya", "Miles", "Dana", "Marcus", "Lena"]),
        ("-interactions.count", ["Dana", "Priya", "Marcus", "Lena", "Tobias", "Miles"]),
        ("-interactions.last_at", ["Priya", "Dana", "Marcus", "Lena", "Tobias", "Miles"]),
        ("met_on", ["Lena", "Dana", "Miles", "Priya", "Tobias", "Marcus"]),
    ],
)
async def test_sort_orders_by_any_single_valued_field(ctx, sort, expected) -> None:
    await _seed(ctx)
    assert await _names(ctx, sort=sort) == expected


@pytest.mark.parametrize("sort", ["name", "-organization.headcount", "-interactions.last_at"])
async def test_a_sorted_listing_walks_to_its_end_one_row_at_a_time(ctx, sort) -> None:
    """The keyset contract under a chosen sort, across the NULL band: every row once."""
    ids = await _seed(ctx)

    async def fetch(cursor: str | None):
        async with session_for(ctx) as conn:
            page = await store.page_contacts(conn, ctx, sort=sort, limit=1, cursor=cursor)
        return page.rows, page.next_cursor

    walked = await walk(fetch, ident=lambda c: c.id, expected={ids[name] for name in _PEOPLE})
    assert [c.name for c in walked] == await _names(ctx, sort=sort)


async def test_a_cursor_from_one_sort_is_refused_under_another(ctx) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, sort="name", limit=2)
        assert page.next_cursor is not None
        with pytest.raises(InvalidCursor):
            await store.page_contacts(conn, ctx, sort="-name", limit=2, cursor=page.next_cursor)
        with pytest.raises(InvalidCursor):
            await store.page_contacts(conn, ctx, limit=2, cursor=page.next_cursor)


async def test_a_field_with_many_values_per_row_cannot_be_sorted_on(ctx) -> None:
    await _seed(ctx)
    for sort in ("tags", "vip"):
        with pytest.raises(FieldError, match="cannot be sorted on"):
            await _names(ctx, sort=sort)


async def test_total_counts_the_filtered_rows_on_the_first_page_only(ctx) -> None:
    await _seed(ctx)
    founders = _clause("audience", "eq", "Founders")
    async with session_for(ctx) as conn:
        first = await store.page_contacts(conn, ctx, filter=founders, limit=1)
        assert (len(first.rows), first.total) == (1, 2)
        second = await store.page_contacts(
            conn, ctx, filter=founders, limit=1, cursor=first.next_cursor
        )
        assert (len(second.rows), second.total, second.next_cursor) == (1, None, None)
        everyone = await store.page_contacts(conn, ctx)
        assert everyone.total == 6


# --------------------------------------------------------------------------- #
# Columns                                                                      #
# --------------------------------------------------------------------------- #


async def test_columns_returns_only_the_paths_named(ctx) -> None:
    ids = await _seed(ctx)
    async with session_for(ctx) as conn:
        page = await store.page_contacts(
            conn,
            ctx,
            filter=_clause("name", "eq", "Dana"),
            columns=[
                "name",
                "audience",
                "tags",
                "vip",
                "organization.name",
                "organization.fit",
                "organization.headcount",
                "signals.active",
                "interactions.count",
            ],
        )
    assert page.rows == [
        {
            "id": ids["Dana"],
            "name": "Dana",
            "audience": "Sales leaders",
            "tags": ["warm", "event"],
            "vip": True,
            "organization.name": "Larkfield",
            "organization.fit": "Strong",
            "organization.headcount": 27,
            "signals.active": 1,
            "interactions.count": 2,
        }
    ]


async def test_columns_pages_under_the_default_and_a_chosen_order(ctx) -> None:
    ids = await _seed(ctx)
    for sort in (None, "-organization.headcount"):

        async def fetch(cursor: str | None, sort=sort):
            async with session_for(ctx) as conn:
                page = await store.page_contacts(
                    conn, ctx, columns=["name"], sort=sort, limit=2, cursor=cursor
                )
            return page.rows, page.next_cursor

        await walk(fetch, ident=lambda r: r["id"], expected={ids[name] for name in _PEOPLE})


async def test_an_unknown_column_is_refused(ctx) -> None:
    await _seed(ctx)
    with pytest.raises(FieldError, match="no contact field named 'dossier'"):
        async with session_for(ctx) as conn:
            await store.page_contacts(conn, ctx, columns=["name", "dossier"])


# --------------------------------------------------------------------------- #
# Organizations                                                                #
# --------------------------------------------------------------------------- #


async def test_organizations_take_the_same_grammar(ctx) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        page = await store.page_organizations(
            conn,
            ctx,
            filter={"and": [_clause("contacts.count", "gte", 2), _clause("fit", "is_set", True)]},
            sort="-headcount",
            columns=["name", "fit", "headcount", "contacts.count", "signals.active"],
        )
    assert page.total == 2
    assert [(r["name"], r["fit"], r["headcount"], r["contacts.count"]) for r in page.rows] == [
        ("Quillstack", "Moderate", 140, 2),
        ("Larkfield", "Strong", 27, 2),
    ]
    assert [r["signals.active"] for r in page.rows] == [0, 1]


async def test_a_shown_rollup_reads_the_same_as_a_joined_one(ctx) -> None:
    """A rollup that is only shown is counted per listed row; one the listing sorts on is
    joined. Both read the same numbers."""
    await _seed(ctx)
    columns = ["name", "contacts.count", "signals.active"]

    async def read(**kwargs: Any) -> list[dict[str, Any]]:
        async with session_for(ctx) as conn:
            page = await store.page_organizations(conn, ctx, columns=columns, **kwargs)
        return page.rows

    shown = {r["name"]: r for r in await read()}
    assert (shown["Larkfield"]["contacts.count"], shown["Larkfield"]["signals.active"]) == (2, 1)
    assert {r["signals.active"] for r in shown.values()} == {0, 1}
    for rollup in ("contacts.count", "signals.active"):
        for value in {r[rollup] for r in shown.values()}:
            matched = await read(filter=_clause(rollup, "eq", value))
            assert {r["name"] for r in matched} == {
                name for name, r in shown.items() if r[rollup] == value
            }
        for sort in (rollup, f"-{rollup}"):
            listed = [r[rollup] for r in await read(sort=sort)]
            assert listed == sorted(listed, reverse=sort.startswith("-"))
            assert sorted(listed) == sorted(r[rollup] for r in shown.values())


async def test_a_shown_rollup_joins_no_aggregate() -> None:
    resolver = query.Resolver(
        "organization", {**store._ORGANIZATION_REFS, **store._ORGANIZATION_ROLLUPS}
    )
    _selected, joins = query.projection(["name", "contacts.count", "signals.active"], resolver)
    assert joins == frozenset()


async def test_a_contact_path_is_not_an_organization_path(ctx) -> None:
    await _seed(ctx)
    with pytest.raises(FieldError, match="no organization field named 'audience'"):
        async with session_for(ctx) as conn:
            await store.page_organizations(conn, ctx, filter=_clause("audience", "eq", "Founders"))


# --------------------------------------------------------------------------- #
# count_by                                                                     #
# --------------------------------------------------------------------------- #


async def _counts(ctx, **kwargs: Any) -> dict[Any, int]:
    async with session_for(ctx) as conn:
        result = await store.count_by(conn, ctx, **kwargs)
    return {v["value"]: v["count"] for v in result["values"]}


async def test_count_by_divides_the_matching_rows_over_a_fields_values(ctx) -> None:
    await _seed(ctx)
    assert await _counts(ctx, entity="contact", field="audience") == {
        "Sales leaders": 2,
        "Founders": 2,
        "Marketing leaders": 1,
        None: 1,
    }
    # Under a filter, and over the organization's field.
    assert await _counts(
        ctx,
        entity="contact",
        field="organization.fit",
        filter=_clause("email_status", "eq", "valid"),
    ) == {"Strong": 1, "Moderate": 1}
    # A list field counts each item once per row that holds it.
    assert await _counts(ctx, entity="contact", field="tags") == {"warm": 2, "event": 2}
    # A number comes back as a number.
    assert await _counts(ctx, entity="organization", field="headcount") == {27: 1, 140: 1, None: 1}
    # A rollup is a field like any other.
    assert await _counts(ctx, entity="contact", field="signals.active") == {0: 4, 1: 2}
    assert await _counts(ctx, entity="organization", field="contacts.count") == {2: 2, 1: 1}
    # Text groups the way its filter matches, regardless of case, under one spelling.
    assert await _counts(ctx, entity="organization", field="industry") == {
        "Software Development": 2,
        "IT services": 1,
    }


async def test_count_by_reports_the_total_and_when_it_was_cut_short(ctx) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        result = await store.count_by(conn, ctx, entity="contact", field="name", limit=4)
    assert (len(result["values"]), result["total"], result["truncated"]) == (4, 6, True)


async def test_count_by_refuses_a_timestamp(ctx) -> None:
    await _seed(ctx)
    with pytest.raises(FieldError, match="timestamp") as caught:
        await _counts(ctx, entity="contact", field="created_at")
    assert "between" in caught.value.next_step


# --------------------------------------------------------------------------- #
# Isolation                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_filter_sees_only_its_own_workspace(ctx) -> None:
    """The organization join and the rollups are scoped to the caller's workspace:
    another workspace's rows neither match nor count."""
    await _seed(ctx)
    other = make_ctx(ctx.tenant_id, workspace_id="ws_2")
    await _seed(other)
    assert set(await _names(ctx, filter=_clause("organization.fit", "eq", "Strong"))) == {
        "Dana",
        "Miles",
    }
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, filter=_clause("signals.active", "gte", 1))
        counts = await store.count_by(conn, ctx, entity="contact", field="audience")
    assert page.total == 2
    assert counts["total"] == 6


# --------------------------------------------------------------------------- #
# Retired fields                                                               #
# --------------------------------------------------------------------------- #


async def _retire(ctx) -> dict[str, str]:
    """Seed, then retire the organization's ``fit`` for ``fit_grade`` and the
    contact's ``audience`` for ``segment``, with the stale values left on the rows."""
    ids = await _seed(ctx)
    async with session_for(ctx) as conn:
        await store.define_field(
            conn,
            ctx,
            entity="organization",
            key="fit_grade",
            label="Fit grade",
            field_type="select",
            options=["A", "B"],
        )
        await store.define_field(
            conn, ctx, entity="contact", key="segment", label="Segment", field_type="text"
        )
        for name, grade in (("Larkfield", "A"), ("Quillstack", "B")):
            await store.update_organization(
                conn, ctx, org_id=ids[name], fields={"fit_grade": grade}
            )
        await store.update_field(
            conn, ctx, entity="organization", key="fit", retired=True, replaced_by="fit_grade"
        )
        await store.update_field(
            conn, ctx, entity="contact", key="audience", retired=True, replaced_by="segment"
        )
    return ids


async def _ask(ctx, entity: str, how: str, path: str) -> None:
    """Name ``path`` on a listing of ``entity`` as a filter, sort, column or count."""
    if how == "count_records":
        async with session_for(ctx) as conn:
            await store.count_by(conn, ctx, entity=entity, field=path)
        return
    request: dict[str, Any] = {
        "filter": {"filter": _clause(path, "is_set", True)},
        "sort": {"sort": f"-{path}"},
        "columns": {"columns": [path]},
    }[how]
    page: Any = {
        "contact": store.page_contacts,
        "organization": store.page_organizations,
        "opportunity": store.page_opportunities,
    }[entity]
    async with session_for(ctx) as conn:
        await page(conn, ctx, **request)


_RETIRED_PATHS = [
    # listing, path naming a retired field, its replacement as a path on that listing
    ("contact", "audience", "'segment'"),
    ("contact", "organization.fit", "'organization.fit_grade'"),
    ("organization", "fit", "'fit_grade'"),
    ("opportunity", "organization.fit", "'organization.fit_grade'"),
    ("opportunity", "contact.audience", "'contact.segment'"),
]


@pytest.mark.parametrize("how", ["filter", "sort", "columns", "count_records"])
@pytest.mark.parametrize(("entity", "path", "replacement"), _RETIRED_PATHS)
async def test_a_retired_path_is_refused_with_its_replacement(
    ctx, entity, path, replacement, how
) -> None:
    await _retire(ctx)
    with pytest.raises(FieldError, match=f"{entity} field '{path}' is retired") as caught:
        await _ask(ctx, entity, how, path)
    assert f"Use {replacement} instead" in caught.value.next_step


async def test_a_retired_path_whose_chain_ends_retired_says_so(ctx) -> None:
    await _retire(ctx)
    async with session_for(ctx) as conn:
        await store.update_field(conn, ctx, entity="organization", key="fit_grade", retired=True)
    with pytest.raises(FieldError, match="contact field 'organization.fit' is retired") as caught:
        await _names(ctx, filter=_clause("organization.fit", "eq", "Strong"))
    assert "ends at the organization field 'fit_grade', which is retired too" in (
        caught.value.next_step
    )
    # The end of the chain is refused under its own name, as having no replacement.
    with pytest.raises(FieldError, match="'organization.fit_grade' is retired") as caught:
        await _names(ctx, filter=_clause("organization.fit_grade", "eq", "A"))
    assert "names no replacement" in caught.value.next_step


async def test_an_unknown_path_is_not_offered_a_retired_one(ctx) -> None:
    await _retire(ctx)
    with pytest.raises(FieldError, match="no contact field named 'nope'") as caught:
        await _names(ctx, filter=_clause("nope", "eq", "x"))
    offered = caught.value.next_step.split("Use one of: ")[1].split(". describe_fields")[0]
    paths = set(offered.split(", "))
    assert {"segment", "organization.fit_grade"} <= paths
    assert not {"audience", "organization.fit"} & paths


async def test_a_filter_on_the_replacement_still_returns_rows(ctx) -> None:
    await _retire(ctx)
    named = await _names(ctx, filter=_clause("organization.fit_grade", "eq", "A"))
    assert sorted(named) == ["Dana", "Miles"]
    assert await _counts(ctx, entity="organization", field="fit_grade") == {"A": 1, "B": 1, None: 1}


# --------------------------------------------------------------------------- #
# Parity                                                                       #
# --------------------------------------------------------------------------- #


async def test_every_rollup_path_is_described() -> None:
    """``describe_fields`` restates the rollup paths, so the two are held equal: a
    rollup an agent cannot read about is a path it will never filter on."""
    assert set(store._ROLLUP_HELP["contact"]) == {p for p in store._CONTACT_REFS if "." in p}
    assert set(store._ROLLUP_HELP["organization"]) == set(store._ORGANIZATION_ROLLUPS)
    for entity in ("contact", "organization"):
        assert all(r["description"] and r["type"] for r in store.rollups(entity))


async def test_every_core_field_but_the_dossier_is_a_filter_path() -> None:
    from mcp_people.fields import CORE_FIELDS

    core = {
        "contact": set(store._CONTACT_REFS),
        "organization": set(store._ORGANIZATION_REFS),
        "opportunity": set(store._OPPORTUNITY_REFS),
    }
    refs = {
        "contact": store._CONTACT_REFS,
        "organization": store._ORGANIZATION_REFS,
        "opportunity": store._OPPORTUNITY_REFS,
    }
    for entity, declared in CORE_FIELDS.items():
        assert {key for key, _t, _d in declared} - core[entity] == {"dossier"}
        # The described type is the kind the path filters as, so a client can build
        # a filter from `describe_fields` alone.
        for key, described, _description in declared:
            if key != "dossier":
                assert refs[entity][key].kind == described, key
