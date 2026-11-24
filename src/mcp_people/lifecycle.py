"""Contact lifecycle status: the stage ladder and the read-time floor projection.

People stores an open-string ``status`` (the engine's authoritative value) and
**never mutates it**. For display and worklist filtering it derives an
``effective_status`` — a *floor* computed from facts People already owns — so a
contact reads at the right stage even when no consumer wrote the transition. A
projection is a view, not a write.

Floor rules (a fact People owns → the minimum stage it implies):

* an **outbound** touch interaction  ⇒ at least ``contacted``
* an **inbound** touch interaction   ⇒ at least ``engaged``

A "touch" is an ``email`` / ``call`` / ``meeting`` — ``note`` / ``research`` are
internal, not contact. The floor only ever *raises* a stored stage, never lowers
it. A stage People does not rank (engine-invented, e.g. ``nurturing``) passes
through untouched. Archiving is ``archived_at``, not a stage.

The derivation lives **once**, in SQL (:func:`effective_status_sql`): it is the
``effective_status`` column every contact read SELECTs *and* the expression the
``list_contacts`` status filter compares against — the *same* expression object, so
the returned field and the filter cannot diverge. :func:`effective_status` is the
pure-Python **oracle**: not used in production, it re-derives the floor
independently so ``tests/test_lifecycle.py`` can assert the SQL matches it across
the full input matrix.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, case

# Progress ladder (rank order). States People does not rank (engine-invented, e.g.
# ``nurturing``) pass through untouched.
LADDER: dict[str, int] = {"sourced": 0, "enriched": 1, "contacted": 2, "engaged": 3}


def effective_status(
    status: str,
    *,
    has_outbound: bool,
    has_inbound: bool,
) -> str:
    """Pure oracle for the read-time floor; mirrors the SQL below.

    Raises a stored rung *below* the highest applicable floor to it, checked in
    descending priority. Unranked (engine-invented) states pass through untouched.
    """
    rank = LADDER.get(status)
    if rank is None:
        return status  # engine-invented / unranked → passthrough
    if has_inbound and rank < LADDER["engaged"]:
        return "engaged"
    if has_outbound and rank < LADDER["contacted"]:
        return "contacted"
    return status


def effective_status_sql(
    status_col: ColumnElement[str],
    has_outbound: ColumnElement[bool],
    has_inbound: ColumnElement[bool],
) -> ColumnElement[str]:
    """SQL ``CASE`` — the production derivation (SELECT column *and* filter).

    Mirrors :func:`effective_status` branch-for-branch; the equivalence is asserted
    by ``test_lifecycle``. ``has_*`` are boolean column expressions — in production
    interaction ``EXISTS`` probes, under test literals —
    each referenced exactly once here, so the ``EXISTS`` are evaluated once per row.
    """
    rank = case(*[(status_col == stage, r) for stage, r in LADDER.items()], else_=None)
    return case(
        (and_(has_inbound, rank < LADDER["engaged"]), "engaged"),
        (and_(has_outbound, rank < LADDER["contacted"]), "contacted"),
        else_=status_col,
    )
