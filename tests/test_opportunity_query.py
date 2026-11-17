"""The filter grammar on deals: `page_opportunities` and `count_by(entity="opportunity")`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from conftest import make_ctx, session_for

from mcp_people.db import store
from mcp_people.fields import FieldError

pytestmark = pytest.mark.asyncio


def _tid() -> str:
    return f"tenant_{uuid.uuid4().hex[:8]}"


def _clause(field: str, op: str, value: Any) -> dict[str, Any]:
    return {"field": field, "op": op, "value": value}


async def _seed(ctx) -> dict[str, str]:
    """Three accounts, two people, six deals across the pipeline, one custom field each."""
    ids: dict[str, str] = {}
    async with session_for(ctx) as conn:
        await store.define_field(
            conn,
            ctx,
            entity="organization",
            key="fit",
            label="Fit",
            field_type="select",
            options=["Strong", "Moderate"],
        )
        await store.define_field(
            conn, ctx, entity="contact", key="audience", label="Audience", field_type="text"
        )
        await store.define_field(
            conn,
            ctx,
            entity="opportunity",
            key="champion",
            label="Champion",
            field_type="select",
            options=["Yes", "No"],
        )
        for name, fit in (
            ("Larkfield", "Strong"),
            ("Quillstack", "Moderate"),
            ("Brightforge", None),
        ):
            org, _, _ = await store.find_or_create_organization(
                conn,
                ctx,
                name=name,
                domain=f"{name.lower()}.example",
                fields={"fit": fit} if fit else None,
            )
            ids[name] = org.id
        dana, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Dana",
            email="dana@larkfield.example",
            organization_id=ids["Larkfield"],
            fields={"audience": "Sales leaders"},
        )
        priya, _, _ = await store.find_or_create_contact(
            conn,
            ctx,
            name="Priya",
            email="priya@quillstack.example",
            organization_id=ids["Quillstack"],
        )
        ids["Dana"], ids["Priya"] = dana.id, priya.id
        deals = [
            # title, org, contact, stage, value, probability, close, champion
            ("Pilot", "Larkfield", "Dana", "lead", 1000, None, "2026-11-15", "Yes"),
            ("Program", "Larkfield", None, "proposal", 30000, None, "2026-10-31", None),
            ("Audit", "Quillstack", "Priya", "qualified", 5000, 10, "2026-10-20", "No"),
            ("Retainer", "Quillstack", None, "negotiation", 20000, 90, "2026-10-14", "Yes"),
            ("Won one", "Brightforge", None, "closed_won", 12000, None, "2026-09-28", None),
            ("Lost one", "Brightforge", None, "closed_lost", 8000, None, None, None),
        ]
        for title, org, who, stage, value, probability, close, champion in deals:
            from datetime import date

            opp, _ = await store.find_or_create_opportunity(
                conn,
                ctx,
                title=title,
                organization_id=ids[org],
                primary_contact_id=ids[who] if who else None,
                stage=stage,
                value=value,
                probability=probability,
                close_date=date.fromisoformat(close) if close else None,
                fields={"champion": champion} if champion else None,
            )
            ids[title] = opp.id
        await store.log_interaction(
            conn,
            ctx,
            contact_id=dana.id,
            kind="call",
            summary="Kickoff",
            opportunity_id=ids["Pilot"],
        )
    return ids


async def _titles(ctx, **kwargs: Any) -> list[str]:
    async with session_for(ctx) as conn:
        page = await store.page_opportunities(conn, ctx, **kwargs)
    return [row.title if hasattr(row, "title") else row["title"] for row in page.rows]


async def _counts(ctx, **kwargs: Any) -> dict[Any, int]:
    async with session_for(ctx) as conn:
        result = await store.count_by(conn, ctx, entity="opportunity", **kwargs)
    return {v["value"]: v["count"] for v in result["values"]}


async def test_open_deals_unless_the_request_speaks_to_openness(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    assert set(await _titles(ctx)) == {"Pilot", "Program", "Audit", "Retainer"}
    assert set(await _titles(ctx, filter=_clause("value", "gte", 10000))) == {"Program", "Retainer"}
    # Naming the outcome or the stage, by argument or by path, lifts the default.
    assert await _titles(ctx, outcome="won") == ["Won one"]
    assert await _titles(ctx, stage="closed_lost") == ["Lost one"]
    assert set(await _titles(ctx, filter=_clause("outcome", "ne", "open"))) == {
        "Won one",
        "Lost one",
    }
    assert set(await _titles(ctx, filter=_clause("stage", "in", ["lead", "closed_won"]))) == {
        "Pilot",
        "Won one",
    }
    assert set(await _titles(ctx, filter=_clause("outcome", "is_set", True))) == {
        "Pilot",
        "Program",
        "Audit",
        "Retainer",
        "Won one",
        "Lost one",
    }


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        (_clause("probability", "gte", 50), {"Program", "Retainer"}),
        (_clause("weighted_value", "gt", 10000), {"Program", "Retainer"}),
        (_clause("weighted_value", "lt", 1000), {"Pilot", "Audit"}),
        (_clause("close_date", "lte", "2026-10-20"), {"Audit", "Retainer"}),
        (
            _clause("close_date", "between", ["2026-10-01", "2026-10-31"]),
            {"Program", "Audit", "Retainer"},
        ),
        (_clause("champion", "eq", "yes"), {"Pilot", "Retainer"}),
        (_clause("champion", "is_set", False), {"Program"}),
        (_clause("organization.fit", "eq", "Strong"), {"Pilot", "Program"}),
        (_clause("organization.name", "starts_with", "Quill"), {"Audit", "Retainer"}),
        (_clause("contact.audience", "eq", "sales leaders"), {"Pilot"}),
        (_clause("contact.name", "is_set", False), {"Program", "Retainer"}),
        (_clause("interactions.count", "gte", 1), {"Pilot"}),
        (_clause("interactions.last_at", "is_set", False), {"Program", "Audit", "Retainer"}),
        (
            {
                "and": [
                    _clause("organization.fit", "eq", "Strong"),
                    _clause("champion", "eq", "Yes"),
                ]
            },
            {"Pilot"},
        ),
    ],
)
async def test_filters_on_the_deals_own_joined_and_rolled_up_paths(
    app_engine, clause, expected
) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    assert set(await _titles(ctx, filter=clause)) == expected


async def test_sort_columns_and_total(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    assert await _titles(ctx, sort="-weighted_value") == ["Retainer", "Program", "Audit", "Pilot"]
    assert await _titles(ctx, sort="close_date") == ["Retainer", "Audit", "Program", "Pilot"]
    async with session_for(ctx) as conn:
        page = await store.page_opportunities(
            conn,
            ctx,
            columns=[
                "title",
                "stage",
                "outcome",
                "probability",
                "weighted_value",
                "organization.name",
                "contact.name",
                "champion",
            ],
            sort="value",
            limit=2,
        )
        assert page.total == 4
        # A projected numeric is a number on the wire, not a Decimal that JSON would
        # carry as a string. (`Decimal("100.00") == 100`, so only the type says.)
        assert type(page.rows[0]["weighted_value"]) in (int, float)
        assert type(page.rows[0]["probability"]) is int
        assert page.rows[0] == {
            "id": page.rows[0]["id"],
            "title": "Pilot",
            "stage": "lead",
            "outcome": "open",
            "probability": 10,
            "weighted_value": 100,
            "organization.name": "Larkfield",
            "contact.name": "Dana",
            "champion": "Yes",
        }
        more = await store.page_opportunities(
            conn, ctx, columns=["title"], sort="value", limit=2, cursor=page.next_cursor
        )
        assert [row["title"] for row in more.rows] == ["Retainer", "Program"]
        assert more.total is None


async def test_a_paged_walk_under_a_stage_derived_sort_matches_the_unpaged_order(
    app_engine,
) -> None:
    """``weighted_value`` is a joined numeric expression the cursor carries as the
    ``num`` kind; walking it one row at a time must give the unpaged order."""
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    for sort in ("-weighted_value", "weighted_value", "probability", "-close_date"):
        unpaged = await _titles(ctx, sort=sort, filter=_clause("outcome", "is_set", True))
        walked: list[str] = []
        cursor = None
        async with session_for(ctx) as conn:
            while True:
                page = await store.page_opportunities(
                    conn,
                    ctx,
                    sort=sort,
                    columns=["title"],
                    limit=1,
                    cursor=cursor,
                    filter=_clause("outcome", "is_set", True),
                )
                walked.extend(row["title"] for row in page.rows)
                cursor = page.next_cursor
                if cursor is None:
                    break
        assert walked == unpaged, sort
        assert len(walked) == 6


async def test_count_by_counts_open_deals_per_stage_and_more(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    assert await _counts(ctx, field="stage") == {
        "lead": 1,
        "proposal": 1,
        "qualified": 1,
        "negotiation": 1,
    }
    assert await _counts(ctx, field="outcome", filter=_clause("outcome", "is_set", True)) == {
        "open": 4,
        "won": 1,
        "lost": 1,
    }
    assert await _counts(ctx, field="champion") == {"Yes": 2, "No": 1, None: 1}
    assert await _counts(ctx, field="organization.fit", filter=_clause("value", "gte", 5000)) == {
        "Strong": 1,
        "Moderate": 2,
    }


async def test_a_bad_deal_filter_names_what_would_have_worked(app_engine) -> None:
    ctx = make_ctx(_tid(), workspace_id="ws_1")
    await _seed(ctx)
    with pytest.raises(FieldError, match="no opportunity field named 'amount'") as caught:
        await _titles(ctx, filter=_clause("amount", "gt", 1))
    assert "weighted_value" in caught.value.next_step
    with pytest.raises(FieldError, match="does not take the operator"):
        await _titles(ctx, filter=_clause("outcome", "contains", "w"))
