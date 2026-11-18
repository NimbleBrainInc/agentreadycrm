"""The filter grammar, sort, and column projection of the list reads.

One grammar serves the list view and the agent: a filter a person builds by
clicking is a filter an agent can read, and the reverse. This module turns that
grammar into SQL and knows nothing about which tables exist. The store hands it
the paths a listing can name (:class:`Ref`), built from its core columns, its
rollups, and the workspace's field definitions; this module validates a request
against them and returns the predicate, the order, or the projection.

Every refusal is a :class:`~mcp_people.fields.FieldError` whose ``next_step``
names what would have worked — the fields that exist, the operators a field
takes, the options a select holds — so a caller that guessed is corrected in one
turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pgkeyset import Listing, Sort
from sqlalchemy import ColumnElement, Numeric, Select, and_, func, not_, or_

from mcp_people.db import dialect as sql
from mcp_people.fields import FieldError

__all__ = [
    "MAX_COLUMNS",
    "Ref",
    "Resolver",
    "custom_ref",
    "parse_sort",
    "projection",
    "sorted_listing",
    "where",
]

MAX_COLUMNS = 30
_MAX_CLAUSES = 50
_MAX_DEPTH = 4
_MAX_IN = 200

# The operators each kind of field takes. A kind is how a field compares, which
# is coarser than its type: a core text column and a custom text field are both
# `text`.
_OPS: dict[str, tuple[str, ...]] = {
    "text": ("eq", "ne", "in", "not_in", "contains", "starts_with", "is_set"),
    "select": ("eq", "ne", "in", "not_in", "is_set"),
    "number": ("eq", "ne", "gt", "gte", "lt", "lte", "between", "is_set"),
    "date": ("eq", "ne", "gt", "gte", "lt", "lte", "between", "is_set"),
    "timestamp": ("gt", "gte", "lt", "lte", "between", "is_set"),
    "boolean": ("eq", "is_set"),
    "multi_select": ("has", "has_all", "is_set"),
    "list": ("has", "has_all", "is_set"),
}

# Kinds with one comparable value per row, so a listing can order by them.
_SORTABLE = frozenset({"text", "select", "number", "date", "timestamp"})


@dataclass(frozen=True)
class Ref:
    """One field path a listing can name, as SQL.

    ``scalar`` is the typed expression a comparison and a sort use; an array kind
    has none. ``value`` is what a column projection returns. ``holds`` tests JSONB
    containment of one value, which is the form a GIN index answers; a core
    column has none and compares ``scalar`` directly. ``present`` is true when the
    row has a value at all. ``joins`` names the related table the expression reads,
    so the store joins it only when a request names such a path. ``key`` is a text
    column's stored match key and ``fold`` the function that computed it
    (:mod:`mcp_people.keys`): an equality or a substring match compares ``fold(value)``
    with the key, so a filter matches exactly the rows the dedupe treats as that value,
    and a plain ``=`` on a stored column is leakproof, which row-level security needs to
    use an index. ``projected`` is a
    rollup's value read for one listed row at a time: a column projection returns it
    instead of joining the aggregate, so a page reads its own rows' counts rather than
    grouping the whole related table. A filter or sort on the path still joins.
    ``elements`` reads an array kind's items, one row each, from a source and its
    conditions, for a count by value.
    """

    path: str
    kind: str
    value: ColumnElement[Any]
    present: ColumnElement[bool]
    scalar: ColumnElement[Any] | None = None
    options: tuple[str, ...] = ()
    holds: Callable[[Any], ColumnElement[bool]] | None = None
    joins: str | None = None
    key: ColumnElement[Any] | None = None
    fold: Callable[[str], str | None] | None = None
    projected: ColumnElement[Any] | None = None
    elements: Callable[[Any, Sequence[Any]], Select[Any]] | None = None


def custom_ref(
    path: str,
    *,
    column: Any,
    key: str,
    field_type: str,
    options: Sequence[str] = (),
    joins: str | None = None,
    dialect: str = "postgresql",
) -> Ref:
    """The :class:`Ref` for a custom field stored under ``key`` in a ``fields`` column,
    in ``dialect``'s JSON operators (:mod:`mcp_people.db.dialect`). The store passes its
    connection's dialect."""
    scalar: ColumnElement[Any] | None
    elements = None
    if field_type == "number":
        scalar = sql.field_number(dialect, column, key, Numeric())
    elif field_type == "boolean":
        scalar = sql.field_boolean(dialect, column, key)
    elif field_type in ("multi_select", "list"):
        scalar = None

        def elements(source: Any, conditions: Sequence[Any]) -> Select[Any]:
            return sql.field_elements(dialect, column, key, source, conditions)
    else:
        scalar = sql.field_text(dialect, column, key)
    return Ref(
        path=path,
        kind=field_type,
        value=sql.field_value(dialect, column, key),
        # A contact with no organization has no `fields` to test, and NULL is not "unset".
        present=func.coalesce(sql.field_has_key(dialect, column, key), False),
        scalar=scalar,
        options=tuple(options),
        holds=lambda value: sql.field_holds(dialect, column, key, value),
        joins=joins,
        elements=elements,
    )


class Resolver:
    """The field paths one listing can name.

    ``retired`` holds the paths of retired fields apart from the live ones, each
    with the sentence naming its replacement, so a filter, sort, column or count
    naming one is refused with where to go instead, and the paths offered for an
    unknown one never include it.
    """

    def __init__(
        self, entity: str, refs: Mapping[str, Ref], *, retired: Mapping[str, str] | None = None
    ) -> None:
        self.entity = entity
        self._refs = refs
        self._retired = retired or {}

    def ref(self, path: Any) -> Ref:
        if isinstance(path, str) and path not in self._refs and path in self._retired:
            raise FieldError(
                f"{self.entity} field {path!r} is retired", next_step=self._retired[path]
            )
        if not isinstance(path, str) or path not in self._refs:
            raise FieldError(
                f"no {self.entity} field named {path!r}",
                next_step=(
                    f"Use one of: {', '.join(sorted(self._refs))}. list_fields("
                    f"entity={self.entity!r}) explains each; a custom field is added with "
                    "define_field."
                ),
            )
        return self._refs[path]


# --------------------------------------------------------------------------- #
# Value coercion                                                               #
# --------------------------------------------------------------------------- #


def _refuse(ref: Ref, what: str) -> FieldError:
    return FieldError(
        f"field {ref.path!r} {what}",
        next_step=f"Correct the value for {ref.path!r} ({ref.kind}) and retry.",
    )


def _option(ref: Ref, value: Any) -> str:
    if isinstance(value, str):
        folded = value.strip().casefold()
        if not ref.options:
            return value.strip()
        for option in ref.options:
            if option.casefold() == folded:
                return option
    raise FieldError(
        f"field {ref.path!r} has no option {value!r}",
        next_step=f"Use one of: {', '.join(ref.options)}.",
    )


def _number(ref: Ref, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _refuse(ref, "compares against a number")
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError:
        raise _refuse(ref, "compares against a number") from None
    if not number.is_finite():
        raise _refuse(ref, "compares against a finite number")
    return number


def _date(ref: Ref, value: Any) -> str:
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except (AttributeError, ValueError):
        raise _refuse(ref, "compares against a date as YYYY-MM-DD") from None


def _timestamp(ref: Ref, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise _refuse(ref, "compares against an ISO-8601 date or timestamp") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _text(ref: Ref, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _refuse(ref, "compares against a non-empty string")
    return value.strip()


_COERCE: dict[str, Callable[[Ref, Any], Any]] = {
    "text": _text,
    "select": _option,
    "number": _number,
    "date": _date,
    "timestamp": _timestamp,
    "multi_select": _option,
    "list": _text,
}


def _many(ref: Ref, value: Any, *, exactly: int | None = None) -> list[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise _refuse(ref, "takes a non-empty list here")
    if exactly is not None and len(value) != exactly:
        raise _refuse(ref, f"takes exactly {exactly} values here")
    if len(value) > _MAX_IN:
        raise _refuse(ref, f"takes at most {_MAX_IN} values")
    return [_COERCE[ref.kind](ref, item) for item in value]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- #
# Predicates                                                                   #
# --------------------------------------------------------------------------- #


def _folded(ref: Ref, value: str) -> str:
    """``value`` under the key function of ``ref``'s key column.

    Empty when the fold leaves nothing, which no stored key equals: a key is never
    the empty string.
    """
    assert ref.fold is not None
    return ref.fold(value) or ""


def _equals(ref: Ref, value: Any) -> ColumnElement[bool]:
    """``ref`` equals one coerced value, in the form an index can answer."""
    if ref.kind == "select" and ref.holds is not None:
        return ref.holds(value)
    assert ref.scalar is not None
    if ref.kind == "text":
        # An open string column holds the same value in several casings.
        if ref.key is not None:
            return ref.key == _folded(ref, value)
        return func.lower(ref.scalar) == value.lower()
    return ref.scalar == value


def _clause(ref: Ref, op: str, value: Any) -> ColumnElement[bool]:
    if op not in _OPS[ref.kind]:
        raise FieldError(
            f"field {ref.path!r} ({ref.kind}) does not take the operator {op!r}",
            next_step=f"Use one of: {', '.join(_OPS[ref.kind])}.",
        )
    if op == "is_set":
        if not isinstance(value, bool):
            raise _refuse(ref, "takes true or false with is_set")
        return ref.present if value else not_(ref.present)
    if ref.kind == "boolean":
        if not isinstance(value, bool):
            raise _refuse(ref, "compares against true or false")
        assert ref.scalar is not None
        return ref.scalar.is_(value)
    if op in ("has", "has_all"):
        assert ref.holds is not None
        wanted = _many(ref, value if isinstance(value, list) else [value])
        if op == "has_all":
            return ref.holds(wanted)
        return or_(*(ref.holds([item]) for item in wanted))
    if op in ("in", "not_in"):
        any_of = or_(*(_equals(ref, item) for item in _many(ref, value)))
        # A row with no value is not in the list, so it passes `not_in`.
        return any_of if op == "in" else not_(func.coalesce(any_of, False))
    if op == "between":
        low, high = _many(ref, value, exactly=2)
        assert ref.scalar is not None
        return and_(ref.scalar >= low, ref.scalar <= high)
    one = _COERCE[ref.kind](ref, value)
    if op == "eq":
        return _equals(ref, one)
    if op == "ne":
        # "Is not X" includes the rows with no value, which is how a person reads it.
        return not_(func.coalesce(_equals(ref, one), False))
    assert ref.scalar is not None
    if op in ("contains", "starts_with"):
        # A stored match key matches case-blind with a plain LIKE on the folded value,
        # and reading the key lets a covering index answer the match without the row.
        wildcard = "%" if op == "contains" else ""
        if ref.key is not None:
            return ref.key.like(wildcard + _escape_like(_folded(ref, one)) + "%", escape="\\")
        return ref.scalar.ilike(wildcard + _escape_like(one) + "%", escape="\\")
    compare = {
        "gt": ref.scalar.__gt__,
        "gte": ref.scalar.__ge__,
        "lt": ref.scalar.__lt__,
        "lte": ref.scalar.__le__,
    }[op]
    return compare(one)


_SHAPE_HELP = (
    'A filter is {"field": ..., "op": ..., "value": ...}, or {"and": [...]} / {"or": [...]} '
    "of filters."
)


def where(
    filter_: Any, resolver: Resolver
) -> tuple[
    ColumnElement[bool] | None,
    Mapping[str, frozenset[Any]],
    Mapping[str, frozenset[Any]],
    frozenset[str],
]:
    """Compile a filter: ``(predicate, values asked for, values excluded, tables to join)``.

    ``None`` or an empty filter compiles to no predicate. The second element
    holds every path the filter names; under it, the values an ``eq`` or ``in``
    clause asks for by name, so the store can tell a request for the rows its
    defaults hide from one that merely mentions the field (``ne``, a branch of
    an ``or``) and would have those rows back by accident. The third holds the
    values a ``ne`` or ``not_in`` clause excludes, so the store can tell when a
    filter rules out the very value its default keeps.
    """
    if not filter_:
        return None, {}, {}, frozenset()
    asked: dict[str, set[Any]] = {}
    excluded: dict[str, set[Any]] = {}
    joins: set[str] = set()
    count = [0]

    def compile_(node: Any, depth: int) -> ColumnElement[bool]:
        if not isinstance(node, Mapping):
            raise FieldError("a filter must be an object", next_step=_SHAPE_HELP)
        if depth > _MAX_DEPTH:
            raise FieldError(
                f"a filter nests at most {_MAX_DEPTH} deep", next_step="Flatten the filter."
            )
        keys = set(node)
        if keys in ({"and"}, {"or"}):
            (combinator,) = keys
            parts = node[combinator]
            if isinstance(parts, (str, Mapping)) or not isinstance(parts, Sequence) or not parts:
                raise FieldError(
                    f"{combinator!r} takes a non-empty list of filters", next_step=_SHAPE_HELP
                )
            compiled = [compile_(part, depth + 1) for part in parts]
            return and_(*compiled) if combinator == "and" else or_(*compiled)
        if "field" not in keys or not keys <= {"field", "op", "value"}:
            raise FieldError("a filter clause needs a field", next_step=_SHAPE_HELP)
        count[0] += 1
        if count[0] > _MAX_CLAUSES:
            raise FieldError(
                f"a filter holds at most {_MAX_CLAUSES} clauses",
                next_step="Narrow the filter, or use `in` for a list of values.",
            )
        ref = resolver.ref(node["field"])
        op, value = str(node.get("op", "eq")), node.get("value")
        if ref.joins:
            joins.add(ref.joins)
        compiled = _clause(ref, op, value)
        values = asked.setdefault(ref.path, set())
        if op == "eq":
            values.add(value)
        elif op == "in":
            values.update(value)
        elif op == "ne":
            excluded.setdefault(ref.path, set()).add(value)
        elif op == "not_in":
            excluded.setdefault(ref.path, set()).update(value)
        return compiled

    return (
        compile_(filter_, 1),
        {path: frozenset(values) for path, values in asked.items()},
        {path: frozenset(values) for path, values in excluded.items()},
        frozenset(joins),
    )


# --------------------------------------------------------------------------- #
# Sort and projection                                                          #
# --------------------------------------------------------------------------- #


def parse_sort(sort: Any, resolver: Resolver) -> tuple[Ref, bool]:
    """``"-path"`` → ``(ref, descending)``. The field must hold one value per row."""
    if not isinstance(sort, str) or not sort.strip("-+ "):
        raise FieldError(
            "sort must be a field path",
            next_step='Pass a field path, with a leading "-" for descending, e.g. "-created_at".',
        )
    text = sort.strip()
    descending = text.startswith("-")
    ref = resolver.ref(text.lstrip("-+"))
    if ref.kind not in _SORTABLE or ref.scalar is None:
        raise FieldError(
            f"field {ref.path!r} ({ref.kind}) cannot be sorted on",
            next_step="Sort on a text, select, number, date or timestamp field.",
        )
    return ref, descending


def sorted_listing(
    name: str, ref: Ref, descending: bool, id_column: ColumnElement[Any]
) -> tuple[Listing, ColumnElement[Any]]:
    """The keyset :class:`Listing` for one sort, and the column its cursor reads.

    The listing's name carries the path and direction, so a cursor taken under one
    sort is refused under another. Rows with no value sort last in either
    direction: a person sorting by a field is looking for the rows that have it.
    The id tiebreak runs the same way as the field.
    """
    assert ref.scalar is not None
    key = ref.scalar.label("_sort")
    make = Sort.desc if descending else Sort.asc
    listing = Listing(
        f"{name}:{'-' if descending else ''}{ref.path}",
        make(ref.scalar, nulls="last", value=lambda row: row["_sort"]),
        make(id_column),
    )
    return listing, key


def projection(
    columns: Any, resolver: Resolver
) -> tuple[list[tuple[str, str, ColumnElement[Any]]], frozenset[str]]:
    """``(path, label, expression)`` for each of ``columns``, and the tables to join.

    Each expression is labelled by position, since a path is not a legal SQL
    label; the store reads the row by label and answers ``{path: value}``. A rollup
    reads its ``projected`` form, so a projection joins only one-row tables.
    """
    if isinstance(columns, str) or not isinstance(columns, Sequence) or not columns:
        raise FieldError(
            "columns must be a non-empty list of field paths",
            next_step='Pass the paths to return, e.g. ["name", "title", "organization.name"].',
        )
    if len(columns) > MAX_COLUMNS:
        raise FieldError(
            f"columns holds at most {MAX_COLUMNS} paths", next_step="Ask for fewer columns."
        )
    selected: list[tuple[str, str, ColumnElement[Any]]] = []
    joins: set[str] = set()
    for index, path in enumerate(dict.fromkeys(columns)):
        ref = resolver.ref(path)
        if ref.projected is not None:
            selected.append((ref.path, f"_c{index}", ref.projected.label(f"_c{index}")))
            continue
        selected.append((ref.path, f"_c{index}", ref.value.label(f"_c{index}")))
        if ref.joins:
            joins.add(ref.joins)
    return selected, frozenset(joins)
