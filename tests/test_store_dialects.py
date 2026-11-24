"""What must hold the same on Postgres and SQLite.

These run on SQLite; each construct's Postgres form is held to the native construct by
rendering it, so the two spellings answer alike.

Each construct :mod:`mcp_people.db.dialect` spells two ways is held here to one answer,
computed in Python from the values the test wrote, so a backend that answers differently
fails rather than agreeing with itself:

* every operator of the filter grammar on every custom field type matches the same rows;
* a field write replaces each key's envelope whole (Postgres ``||``, SQLite ``json_set``),
  removes a cleared key, and a find-or-create only fills absent keys;
* a count by an array field counts each item once per row holding it;
* deal values keep their cents through ``value × probability / 100`` and the sums;
* writes stamped by the database clock keep their order within one second;
* every ON CONFLICT target (partial, plain, and the expression on the evidence edge) converges;
* every timestamp comes back timezone-aware in UTC;
* each helper's Postgres form renders the native Postgres construct;
* a keyset walk meets every row once, in order, over ties and NULLs;
* a value Postgres cannot store is refused on both.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from conftest import define_signal_types, make_ctx, session_for
from pgkeyset.testing import walk
from sqlalchemy import Boolean, Numeric, Text, cast, create_engine, func, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array

from mcp_people.db import dialect, store
from mcp_people.db.tables import contact, opportunity, signal_evidence_for
from mcp_people.fields import MAX_FIELDS_PER_ENTITY

pytestmark = pytest.mark.asyncio

_TAGS = ["warm", "event", "board"]
_TIERS = ["Gold", "Silver", "Bronze"]

# name: {key: value}; an absent key is a field the contact does not hold.
_VALUES: dict[str, dict[str, Any]] = {
    "Ada": {
        "note": "Met at Halvorsen",
        "score": 87.5,
        "vip": True,
        "met_on": "2026-09-04",
        "tier": "Gold",
        "badges": ["warm", "event"],
        "tags": ["alpha", "beta"],
    },
    "Bram": {
        "note": "cold lead",
        "score": 12,
        "vip": False,
        "met_on": "2026-08-01",
        "tier": "Silver",
        "badges": ["warm"],
        "tags": ["beta"],
    },
    "Cleo": {"score": 50, "tier": "Gold", "badges": ["board", "event", "warm"]},
    "Dov": {"note": "Halvorsen intro", "vip": True},
    "Edda": {},
}


@pytest.fixture
def ctx(app_engine):
    return make_ctx(f"tenant_{uuid.uuid4().hex[:8]}", workspace_id="ws_1")


async def _seed(ctx) -> dict[str, str]:
    ids: dict[str, str] = {}
    async with session_for(ctx) as conn:
        for key, field_type, options in [
            ("note", "text", None),
            ("score", "number", None),
            ("vip", "boolean", None),
            ("met_on", "date", None),
            ("tier", "select", _TIERS),
            ("badges", "multi_select", _TAGS),
            ("tags", "list", None),
        ]:
            await store.define_field(
                conn,
                ctx,
                entity="contact",
                key=key,
                label=key,
                field_type=field_type,
                options=options,
            )
        for name, values in _VALUES.items():
            person, _a, _p = await store.find_or_create_contact(
                conn, ctx, name=name, email=f"{name.lower()}@dialect.example", fields=values
            )
            ids[name] = person.id
    return ids


def _expect(predicate) -> set[str]:
    return {name for name, values in _VALUES.items() if predicate(values)}


def _has(key: str):
    return lambda v: key in v


# (filter, expected names). Each predicate reads the values above as the grammar reads them.
_CASES: list[tuple[dict[str, Any], set[str]]] = [
    # text
    (
        {"field": "note", "op": "eq", "value": "COLD LEAD"},
        _expect(lambda v: v.get("note") == "cold lead"),
    ),
    (
        {"field": "note", "op": "ne", "value": "cold lead"},
        _expect(lambda v: v.get("note") != "cold lead"),
    ),
    ({"field": "note", "op": "in", "value": ["cold lead", "Halvorsen intro"]}, {"Bram", "Dov"}),
    (
        {"field": "note", "op": "not_in", "value": ["cold lead"]},
        _expect(lambda v: v.get("note") != "cold lead"),
    ),
    ({"field": "note", "op": "contains", "value": "halvorsen"}, {"Ada", "Dov"}),
    ({"field": "note", "op": "starts_with", "value": "met"}, {"Ada"}),
    ({"field": "note", "op": "is_set", "value": True}, _expect(_has("note"))),
    ({"field": "note", "op": "is_set", "value": False}, _expect(lambda v: "note" not in v)),
    # number
    ({"field": "score", "op": "eq", "value": 87.5}, {"Ada"}),
    ({"field": "score", "op": "ne", "value": 50}, _expect(lambda v: v.get("score") != 50)),
    ({"field": "score", "op": "gt", "value": 12}, {"Ada", "Cleo"}),
    ({"field": "score", "op": "gte", "value": 12}, {"Ada", "Bram", "Cleo"}),
    ({"field": "score", "op": "lt", "value": "50"}, {"Bram"}),
    ({"field": "score", "op": "lte", "value": 50}, {"Bram", "Cleo"}),
    ({"field": "score", "op": "between", "value": [12, 87.4]}, {"Bram", "Cleo"}),
    ({"field": "score", "op": "is_set", "value": True}, _expect(_has("score"))),
    # date
    ({"field": "met_on", "op": "eq", "value": "2026-09-04"}, {"Ada"}),
    ({"field": "met_on", "op": "lt", "value": "2026-09-01"}, {"Bram"}),
    ({"field": "met_on", "op": "between", "value": ["2026-08-01", "2026-09-04"]}, {"Ada", "Bram"}),
    ({"field": "met_on", "op": "is_set", "value": False}, _expect(lambda v: "met_on" not in v)),
    # boolean
    ({"field": "vip", "op": "eq", "value": True}, {"Ada", "Dov"}),
    ({"field": "vip", "op": "eq", "value": False}, {"Bram"}),
    ({"field": "vip", "op": "is_set", "value": True}, _expect(_has("vip"))),
    # select
    ({"field": "tier", "op": "eq", "value": "gold"}, {"Ada", "Cleo"}),
    ({"field": "tier", "op": "ne", "value": "Gold"}, _expect(lambda v: v.get("tier") != "Gold")),
    ({"field": "tier", "op": "in", "value": ["Silver", "Bronze"]}, {"Bram"}),
    (
        {"field": "tier", "op": "not_in", "value": ["Gold"]},
        _expect(lambda v: v.get("tier") != "Gold"),
    ),
    ({"field": "tier", "op": "is_set", "value": False}, _expect(lambda v: "tier" not in v)),
    # multi_select
    ({"field": "badges", "op": "has", "value": "event"}, {"Ada", "Cleo"}),
    ({"field": "badges", "op": "has", "value": ["board", "event"]}, {"Ada", "Cleo"}),
    ({"field": "badges", "op": "has_all", "value": ["warm", "event"]}, {"Ada", "Cleo"}),
    ({"field": "badges", "op": "has_all", "value": ["warm", "board"]}, {"Cleo"}),
    ({"field": "badges", "op": "is_set", "value": True}, _expect(_has("badges"))),
    # list
    ({"field": "tags", "op": "has", "value": "beta"}, {"Ada", "Bram"}),
    ({"field": "tags", "op": "has_all", "value": ["alpha", "beta"]}, {"Ada"}),
    ({"field": "tags", "op": "has", "value": "gamma"}, set()),
    # and / or nesting
    (
        {
            "or": [
                {
                    "and": [
                        {"field": "tier", "op": "eq", "value": "Gold"},
                        {"field": "score", "op": "gt", "value": 60},
                    ]
                },
                {"field": "vip", "op": "eq", "value": False},
            ]
        },
        {"Ada", "Bram"},
    ),
]


@pytest.mark.parametrize(("filter_", "expected"), _CASES, ids=lambda c: str(c)[:60])
async def test_every_operator_matches_the_same_rows(ctx, filter_, expected) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, filter=filter_, limit=50)
    assert {row.name for row in page.rows} == expected


async def test_a_projection_returns_each_value_as_stored(ctx) -> None:
    """A column projection reads the stored JSON: a boolean stays a boolean, a list a list."""
    ids = await _seed(ctx)
    columns = ["note", "score", "vip", "met_on", "tier", "badges", "tags"]
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, columns=columns, limit=50)
    by_id = {row["id"]: row for row in page.rows}
    for name, values in _VALUES.items():
        row = by_id[ids[name]]
        assert {k: row[k] for k in columns} == {k: values.get(k) for k in columns}
        assert all(type(row[k]) is type(v) for k, v in values.items())


async def test_a_sort_on_a_number_field_orders_by_value_with_absent_last(ctx) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        page = await store.page_contacts(conn, ctx, sort="-score", limit=50)
    names = [row.name for row in page.rows]
    assert names[:3] == ["Ada", "Cleo", "Bram"]
    assert set(names[3:]) == {"Dov", "Edda"}


async def test_count_by_an_array_field_counts_each_item_per_row(ctx) -> None:
    await _seed(ctx)
    async with session_for(ctx) as conn:
        counted = await store.count_by(conn, ctx, entity="contact", field="badges")
        tiers = await store.count_by(conn, ctx, entity="contact", field="tier")
    assert {v["value"]: v["count"] for v in counted["values"]} == {
        "warm": 3,
        "event": 2,
        "board": 1,
    }
    assert counted["total"] == len(_VALUES)
    assert {v["value"]: v["count"] for v in tiers["values"]} == {"Gold": 2, "Silver": 1, None: 2}


async def _stored_fields(ctx, contact_id: str) -> dict[str, Any]:
    async with session_for(ctx) as conn:
        row = (await conn.execute(select(contact.c.fields).where(contact.c.id == contact_id))).one()
    return row.fields


async def test_a_field_write_replaces_each_envelope_whole(ctx) -> None:
    """``||`` replaces a top-level key's whole value. A merge that recursed into the
    envelope (``json_patch``) would keep the first write's ``why`` and ``by``."""
    ids = await _seed(ctx)
    async with session_for(ctx) as conn:
        await store.update_contact(
            conn,
            ctx,
            contact_id=ids["Bram"],
            fields={"note": {"value": "warming", "why": "replied"}},
            fields_by="agent-a",
        )
    first = await _stored_fields(ctx, ids["Bram"])
    assert first["note"]["why"] == "replied" and first["note"]["by"] == "agent-a"

    async with session_for(ctx) as conn:
        await store.update_contact(
            conn, ctx, contact_id=ids["Bram"], fields={"note": "warm now", "score": None}
        )
    stored = await _stored_fields(ctx, ids["Bram"])
    assert stored["note"]["v"] == "warm now"
    assert "why" not in stored["note"] and "by" not in stored["note"]
    assert "score" not in stored  # a null clears the key
    assert stored["tier"] == first["tier"]  # a key the write did not name is untouched


async def test_a_find_or_create_match_fills_only_absent_fields(ctx) -> None:
    ids = await _seed(ctx)
    before = await _stored_fields(ctx, ids["Dov"])
    async with session_for(ctx) as conn:
        _c, action, _p = await store.find_or_create_contact(
            conn,
            ctx,
            name="Dov",
            email="dov@dialect.example",
            fields={"note": "should not replace", "tier": "Bronze"},
        )
    assert action == "matched"
    stored = await _stored_fields(ctx, ids["Dov"])
    assert stored["note"] == before["note"]
    assert stored["tier"]["v"] == "Bronze"


async def test_writes_at_the_field_limit_set_and_fill_every_key(ctx) -> None:
    """A write naming every field an entity may define: SQLite's ``json_set`` and
    ``json_insert`` take at most 127 arguments, two per key."""
    keys = [f"f{i}" for i in range(MAX_FIELDS_PER_ENTITY)]
    held = keys[::2]
    async with session_for(ctx) as conn:
        for key in keys:
            await store.define_field(
                conn, ctx, entity="contact", key=key, label=key, field_type="text"
            )
        person, _a, _p = await store.find_or_create_contact(
            conn, ctx, name="Wide", email="wide@dialect.example", fields=dict.fromkeys(held, "held")
        )
        _c, action, _p = await store.find_or_create_contact(
            conn, ctx, name="Wide", email="wide@dialect.example", fields=dict.fromkeys(keys, "fill")
        )
    assert action == "matched"
    filled = await _stored_fields(ctx, person.id)
    assert {k: v["v"] for k, v in filled.items()} == {
        k: "held" if k in held else "fill" for k in keys
    }

    async with session_for(ctx) as conn:
        await store.update_contact(
            conn, ctx, contact_id=person.id, fields=dict.fromkeys(keys, "set")
        )
    written = await _stored_fields(ctx, person.id)
    assert {k: v["v"] for k, v in written.items()} == dict.fromkeys(keys, "set")


async def test_weighted_value_keeps_its_cents(ctx) -> None:
    """``value × probability / 100``: SQLite stores 10.00 as the integer 10 and would divide
    as integers without the REAL cast."""
    deals = [(Decimal("10.00"), 33), (Decimal("1234.56"), 15), (Decimal("99.99"), 7)]
    async with session_for(ctx) as conn:
        made = [
            (
                await store.find_or_create_opportunity(
                    conn, ctx, title=f"Deal {i}", value=float(v), probability=p
                )
            )[0]
            for i, (v, p) in enumerate(deals)
        ]
        summary = await store.pipeline_summary(conn, ctx)
    weighted = [v * p / 100 for v, p in deals]
    for deal, (value, _p), want in zip(made, deals, weighted, strict=True):
        assert Decimal(str(deal.value)) == value
        assert deal.weighted_value == pytest.approx(float(want), abs=1e-9)
    assert summary["by_stage"]["lead"]["value_sum"] == pytest.approx(
        float(sum(v for v, _ in deals)), abs=1e-9
    )
    assert summary["by_stage"]["lead"]["weighted_sum"] == pytest.approx(
        float(sum(weighted)), abs=1e-9
    )


async def _org(ctx, name: str) -> str:
    async with session_for(ctx) as conn:
        org, _a, _p = await store.find_or_create_organization(conn, ctx, name=name)
    return org.id


async def test_database_clock_writes_keep_their_order_within_a_second(ctx) -> None:
    """Signals take the database's clock. SQLite's CURRENT_TIMESTAMP has whole seconds, so
    these would tie; the bound UTC time keeps them apart and in order."""
    org_id = await _org(ctx, "Clockwork")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring")
    stamps = []
    for i in range(5):
        async with session_for(ctx) as conn:
            made, _a, _s = await store.record_signal(
                conn,
                ctx,
                organization_id=org_id,
                claim=f"Claim {i}.",
                evidence_quote=f"Quote {i}.",
                source_url=f"https://clock.example/{i}",
                signal_type="hiring",
            )
        stamps.append(made.created_at)
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert stamps[-1] - stamps[0] < timedelta(seconds=5)


async def test_every_conflict_target_converges(ctx) -> None:
    org_id = await _org(ctx, "Converge")
    async with session_for(ctx) as conn:
        await define_signal_types(conn, ctx, "hiring", "hiring")  # plain unique target
        types = await store.describe_signal_types(conn, ctx)
    assert [t.key for t in types].count("hiring") == 1

    async with session_for(ctx) as conn:
        a, *_ = await store.find_or_create_organization(conn, ctx, name="Domainless Co")
        b, action, _ = await store.find_or_create_organization(conn, ctx, name="domainless co")
        c, *_ = await store.find_or_create_contact(conn, ctx, name="Mo", email="mo@x.example")
        d, action_d, _ = await store.find_or_create_contact(
            conn, ctx, name="Mo", email="MO@x.example"
        )
        i1 = await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="note", summary="hi", idempotency_key="k1"
        )
        i2 = await store.log_interaction(
            conn, ctx, contact_id=c.id, kind="note", summary="hi", idempotency_key="k1"
        )
        o1, _ = await store.find_or_create_opportunity(conn, ctx, title="T", idempotency_key="k")
        o2, action_o = await store.find_or_create_opportunity(
            conn, ctx, title="T", idempotency_key="k"
        )
        p1, _ = await store.record_proof_point(conn, ctx, claim="We did it.", idempotency_key="k")
        p2, action_p = await store.record_proof_point(
            conn, ctx, claim="We did it.", idempotency_key="k"
        )
    assert (a.id, action) == (b.id, "matched")
    assert (c.id, action_d) == (d.id, "matched")
    assert i1.id == i2.id
    assert (o1.id, action_o) == (o2.id, "matched")
    assert (p1.id, action_p) == (p2.id, "matched")

    # The evidence edge's target is an expression: coalesce(for_organization_id, '').
    async with session_for(ctx) as conn:
        sig, _a, _s = await store.record_signal(
            conn,
            ctx,
            organization_id=org_id,
            claim="Converge hired.",
            evidence_quote="We hired.",
            source_url="https://converge.example/hire",
            signal_type="hiring",
            evidence_for=["workspace"],
        )
        _s1, added = await store.add_signal_for(
            conn, ctx, signal_id=sig.id, evidence_for=["workspace", org_id]
        )
        _s2, again = await store.add_signal_for(
            conn, ctx, signal_id=sig.id, evidence_for=["workspace"]
        )
        _s3, removed = await store.remove_signal_for(
            conn, ctx, signal_id=sig.id, evidence_for=["workspace"]
        )
        revived, back = await store.add_signal_for(
            conn, ctx, signal_id=sig.id, evidence_for=["workspace"]
        )
        edges = (
            await conn.execute(
                select(signal_evidence_for.c.for_organization_id).where(
                    signal_evidence_for.c.signal_id == sig.id
                )
            )
        ).all()
    assert (added, again, removed, back) == ([org_id], [], ["workspace"], ["workspace"])
    assert sorted(revived.evidence_for) == sorted(["workspace", org_id])
    assert len(edges) == 2


def _is_utc(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() == timedelta(0)


async def test_every_returned_timestamp_is_utc_aware(ctx) -> None:
    hst = timezone(timedelta(hours=-10))
    when = datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=hst)
    naive = datetime(2030, 6, 1, 12, 0, 0)
    async with session_for(ctx) as conn:
        person, *_ = await store.find_or_create_contact(
            conn, ctx, name="Tz", email="tz@x.example", email_status="valid", email_checked_at=naive
        )
        touch = await store.log_interaction(
            conn, ctx, contact_id=person.id, kind="email", summary="s", occurred_at=when
        )
        fresh, _counts = await store.get_contact(conn, ctx, contact_id=person.id)
    assert touch.occurred_at == when and _is_utc(touch.occurred_at)
    assert _is_utc(touch.created_at)
    assert fresh.email_checked_at == naive.replace(tzinfo=UTC)
    for value in (
        fresh.created_at,
        fresh.updated_at,
        fresh.email_checked_at,
    ):
        assert _is_utc(value)


async def test_a_walk_meets_every_row_once_in_order(ctx) -> None:
    """Newest first over a created_at many rows share (one transaction), and a custom-field
    sort with ties and absent values: only the id orders the ties."""
    async with session_for(ctx) as conn:
        await store.define_field(
            conn, ctx, entity="contact", key="rank", label="rank", field_type="number"
        )
        for i in range(17):
            fields = {"rank": i % 3} if i % 4 else {}
            await store.find_or_create_contact(
                conn, ctx, name=f"P{i:02d}", email=f"p{i}@walk.example", fields=fields
            )
    async with session_for(ctx) as conn:
        everyone = (await store.page_contacts(conn, ctx, limit=100)).rows

    for sort in (None, "rank", "-rank", "name"):

        async def fetch(cursor: str | None, sort=sort):
            async with session_for(ctx) as conn:
                page = await store.page_contacts(conn, ctx, sort=sort, limit=4, cursor=cursor)
            return page.rows, page.next_cursor

        walked = await walk(fetch, ident=lambda row: row.id, expected={r.id for r in everyone})
        if sort is None:
            assert [r.id for r in walked] == [
                r.id for r in sorted(everyone, key=lambda r: (r.created_at, r.id), reverse=True)
            ]


async def test_a_value_postgres_cannot_store_is_refused_on_both(ctx) -> None:
    """A NUL inside a field value: Postgres's jsonb refuses it, so the store refuses it first,
    and an import skips the row the same way on SQLite."""
    async with session_for(ctx) as conn:
        await store.define_field(
            conn, ctx, entity="organization", key="tags", label="tags", field_type="list"
        )
        result = await store.import_organizations(
            conn,
            ctx,
            rows=[
                {"name": "Kept", "fields": {"tags": ["ok"]}},
                {"name": "Nul", "fields": {"tags": ["a\x00b"]}},
            ],
        )
    assert result["created"] == 1
    [skipped] = result["skipped"]
    assert skipped["rows"] == [1] and "NUL" in skipped["reason"]


def _pg(expr: Any) -> str:
    return str(expr.compile(dialect=postgresql.dialect()))


def test_each_postgres_form_is_the_native_construct() -> None:
    """Each helper's Postgres branch renders exactly the native expression, so the plans,
    the GIN index use and the pinned cursors are a Postgres-only store's."""
    fields = contact.c.fields
    patch = {"k": {"v": 1, "at": "2026-01-01T00:00:00+00:00"}}
    pairs = [
        (dialect.field_value("postgresql", fields, "k"), fields["k"]["v"]),
        (dialect.field_text("postgresql", fields, "k"), fields["k"]["v"].astext),
        (
            dialect.field_number("postgresql", fields, "k", Numeric()),
            cast(fields["k"]["v"].astext, Numeric),
        ),
        (dialect.field_boolean("postgresql", fields, "k"), cast(fields["k"]["v"].astext, Boolean)),
        (dialect.field_has_key("postgresql", fields, "k"), fields.has_key("k")),
        (
            dialect.field_holds("postgresql", fields, "k", ["a", "b"]),
            fields.contains(cast({"k": {"v": ["a", "b"]}}, JSONB)),
        ),
        (
            dialect.field_has_any("postgresql", fields, "k", ["a"]),
            fields["k"]["v"].has_any(array(["a"], type_=Text)),
        ),
        (
            dialect.fields_written("postgresql", fields, patch, ["gone"]),
            fields.op("||")(cast(literal(patch, JSONB), JSONB))
            .self_group()
            .op("-")(cast(literal(["gone"], ARRAY(Text)), ARRAY(Text))),
        ),
        (
            dialect.fields_filled("postgresql", fields, patch),
            cast(literal(patch, JSONB), JSONB).op("||")(fields),
        ),
        (dialect.db_now("postgresql"), func.now()),
        (
            dialect.db_ago("postgresql", timedelta(seconds=5)),
            func.now() - literal(timedelta(seconds=5)),
        ),
        (
            store._WEIGHTED_VALUE,
            opportunity.c.value * store._EFFECTIVE_PROBABILITY / 100,
        ),
    ]
    for new, old in pairs:
        assert _pg(new) == _pg(old)
    elements = dialect.field_elements("postgresql", fields, "k", contact, [contact.c.id == "x"])
    assert _pg(elements) == _pg(
        select(func.jsonb_array_elements_text(fields["k"]["v"]).label("value"))
        .select_from(contact)
        .where(contact.c.id == "x")
    )


def test_sqlite_field_writes_stay_under_the_argument_cap() -> None:
    """Any number of keys set, filled or removed in one write: SQLite caps a function at 127
    arguments, so each helper nests its calls. Retired keys are not counted by the field
    cap, so a write may clear more keys than an entity holds live."""
    keys = [f"k{i}" for i in range(300)]
    start = {k: {"v": k} for k in keys}
    patch = {k: {"v": "new"} for k in keys}
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            column = literal(start, dialect.JSONType)
            written = conn.execute(
                select(dialect.fields_written("sqlite", column, patch, keys[:250]))
            ).scalar_one()
            filled = conn.execute(
                select(dialect.fields_filled("sqlite", literal({}, dialect.JSONType), patch))
            ).scalar_one()
    finally:
        engine.dispose()
    assert written == {k: {"v": "new"} for k in keys[250:]}
    assert filled == patch
