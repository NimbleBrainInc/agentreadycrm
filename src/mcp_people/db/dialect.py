"""What the tables and store use where Postgres and SQLite differ.

The store runs on Postgres (with row-level security) and on SQLite (one principal). Each
helper here picks the dialect's form of one construct, so a store query reads the same on
both. The Postgres form of every helper is the native construct, byte for byte, so the plans,
GIN indexes and pinned cursors are those of a Postgres-only store
(``tests/test_store_dialects.py`` holds each to it).

A helper takes the dialect's name (``conn.dialect.name``), never a connection, so the filter
grammar (:mod:`mcp_people.query`) can build its expressions without one.

**Custom field values.** A row's ``fields`` column holds ``{key: {"v": value, "by", "at",
"why"}}``. On Postgres it is JSONB and the store reads it with ``->``,
``->>``, ``?``, ``@>`` and ``?|``. On SQLite it is JSON text and the same reads are
``json_extract``, ``json_type`` and ``json_each``. Field keys are validated identifiers
(:func:`mcp_people.fields.normalize_key`), so a key is quoted into a JSON path, never bound.

**Writes merge per top-level key.** Postgres ``a || b`` replaces each top-level key of ``a``
with ``b``'s whole value. SQLite's ``json_set`` per key does the same; ``json_patch`` would
not, because it merges nested objects, keeping a stale ``why`` or ``by`` from the envelope it
replaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ColumnElement,
    DateTime,
    Integer,
    Numeric,
    Select,
    Table,
    Text,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    literal_column,
    select,
    true,
    type_coerce,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = [
    "JSONType",
    "RowId",
    "UTCDateTime",
    "as_real",
    "binary_collation",
    "byte_length",
    "db_ago",
    "db_now",
    "field_boolean",
    "field_elements",
    "field_has_any",
    "field_has_key",
    "field_holds",
    "field_number",
    "field_text",
    "field_value",
    "fields_filled",
    "fields_written",
    "insert_for",
    "joined_text",
]

SQLITE = "sqlite"

#: JSONB on Postgres, JSON text on SQLite. JSONB is the base, not the variant, so a column's
#: expressions keep JSONB's operators and bind names (``fields_1``) as Postgres renders them;
#: the SQLite forms never use those operators (the field helpers below branch first).
JSONType = JSONB().with_variant(JSON(), "sqlite")

#: An auto-incrementing key: SQLite generates one only for an ``INTEGER PRIMARY KEY``.
RowId = BigInteger().with_variant(Integer(), "sqlite")


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware, in UTC, on every backend.

    ``timestamptz`` on Postgres. SQLite stores no zone, so the value is stored as UTC and the
    zone re-attached on read. A naive value bound to it is taken as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == SQLITE else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def insert_for(dialect: str, table: Table) -> Any:
    """The dialect's ``INSERT``, which takes ``on_conflict_do_nothing`` and
    ``on_conflict_do_update`` with an index predicate on both Postgres and SQLite."""
    module = sqlite if dialect == SQLITE else postgresql
    return module.insert(table)


# --------------------------------------------------------------------------- #
# Clock                                                                        #
# --------------------------------------------------------------------------- #


def db_now(dialect: str) -> ColumnElement[datetime]:
    """The time a write stamps, in the database where it can be.

        ``now()`` on Postgres: the transaction's start, which orders the signal change read
    . On SQLite, this process's UTC time, bound: ``CURRENT_TIMESTAMP``
        there has whole seconds only, so rows written in one second would lose their order.
        Writers on SQLite are serialized by the database lock, so the process clock orders
        them as the commit does. Every keyset still breaks ties on the id.
    """
    if dialect == SQLITE:
        return literal(datetime.now(UTC), UTCDateTime())
    return func.now()


def db_ago(dialect: str, delta: timedelta) -> ColumnElement[datetime]:
    """:func:`db_now` less ``delta``."""
    if dialect == SQLITE:
        return literal(datetime.now(UTC) - delta, UTCDateTime())
    return func.now() - literal(delta)


# --------------------------------------------------------------------------- #
# Text and numbers                                                             #
# --------------------------------------------------------------------------- #


def joined_text(dialect: str, sep: str, head: Any, tail: Any) -> ColumnElement[str]:
    """``head``, ``sep`` and ``tail``, or ``tail`` alone when ``head`` is NULL.

    ``concat_ws`` on Postgres; SQLite added it only in 3.44, so there it is
    ``coalesce(head || sep, '') || tail``.
    """
    if dialect == SQLITE:
        return func.coalesce(head.concat(sep), "").concat(tail)
    return func.concat_ws(sep, head, tail)


def byte_length(dialect: str, value: Any) -> ColumnElement[int]:
    """The UTF-8 byte length of ``value``: ``octet_length`` on Postgres, the length of the
    text as a blob on SQLite (whose ``octet_length`` is new in 3.43)."""
    if dialect == SQLITE:
        return func.length(cast(value, sqlite.BLOB()))
    return func.octet_length(value)


def binary_collation(dialect: str) -> str:
    """The collation that orders text by its bytes: ``C`` on Postgres, ``BINARY`` on SQLite."""
    return "BINARY" if dialect == SQLITE else "C"


class _Real(ColumnElement[Any]):
    """A number as a float on SQLite, and unchanged elsewhere; see :func:`as_real`."""

    inherit_cache = True
    _traverse_internals = [("expr", InternalTraversal.dp_clauseelement)]

    def __init__(self, expr: ColumnElement[Any]) -> None:
        self.expr = expr
        # Unscaled: a product of a numeric(18, 2) keeps more places than two, and SQLite's
        # result processing would round it to the column's scale.
        self.type = Numeric()


@compiles(_Real)
def _real_default(element: _Real, compiler: Any, **kw: Any) -> str:
    return compiler.process(element.expr, **kw)


@compiles(_Real, "sqlite")
def _real_sqlite(element: _Real, compiler: Any, **kw: Any) -> str:
    return f"CAST({compiler.process(element.expr, **kw)} AS REAL)"


def as_real(expr: ColumnElement[Any]) -> ColumnElement[Any]:
    """``expr`` (a ``numeric`` column) in arithmetic that keeps its fraction.

    Postgres computes ``numeric`` exactly, so the expression is unchanged there. SQLite gives
    a ``NUMERIC`` column integer affinity, storing ``10.00`` as the integer 10, and divides
    two integers as integers; the cast makes ``value * p / 100`` a float division. It
    compiles per dialect, so a module-level expression can hold it. Its type is an unscaled
    ``numeric``, so a result keeps the places the arithmetic gives it.
    """
    return _Real(expr)


# --------------------------------------------------------------------------- #
# Custom field values                                                          #
# --------------------------------------------------------------------------- #


def _path(key: str, *rest: str) -> str:
    """The JSON path of ``key`` (and below), for SQLite's JSON functions."""
    if '"' in key or "\\" in key:  # a validated key never holds either
        raise ValueError(f"not a field key: {key!r}")
    return "$." + ".".join([f'"{key}"', *rest])


def field_value(dialect: str, column: Any, key: str) -> ColumnElement[Any]:
    """The value stored under ``key``, as JSON (decoded on read); NULL when absent.

    SQLite's ``json_extract`` returns a JSON ``true`` as the integer 1, so the two booleans
    are spelled out as JSON text; every other value is re-quoted as JSON.
    """
    if dialect == SQLITE:
        path = _path(key, "v")
        kind = func.json_type(column, path)
        text = case(
            (kind == "true", literal_column("'true'")),
            (kind == "false", literal_column("'false'")),
            else_=func.json_quote(func.json_extract(column, path)),
        )
        return type_coerce(text, JSONType)
    return column[key]["v"]


def field_text(dialect: str, column: Any, key: str) -> ColumnElement[str]:
    """The value under ``key`` as text: ``->>`` on Postgres, ``json_extract`` on SQLite."""
    if dialect == SQLITE:
        return type_coerce(func.json_extract(column, _path(key, "v")), Text())
    return column[key]["v"].astext


def field_number(dialect: str, column: Any, key: str, type_: TypeEngine[Any]) -> Any:
    """The value under ``key`` cast to the number type ``type_``."""
    return cast(field_text(dialect, column, key), type_)


def field_boolean(dialect: str, column: Any, key: str) -> ColumnElement[bool]:
    """The value under ``key`` as a boolean.

    ``CAST(->> AS boolean)`` on Postgres. On SQLite ``json_extract`` already gives 1 or 0 for a
    JSON boolean; ``CAST('true' AS BOOLEAN)`` there would be 0.
    """
    if dialect == SQLITE:
        return type_coerce(func.json_extract(column, _path(key, "v")), Boolean())
    return cast(field_text(dialect, column, key), Boolean)


def field_has_key(dialect: str, column: Any, key: str) -> ColumnElement[bool]:
    """Whether the row holds ``key`` at all: ``?`` on Postgres, ``json_type`` on SQLite."""
    if dialect == SQLITE:
        return func.json_type(column, _path(key)).is_not(None)
    return column.has_key(key)


def _items(column: Any, key: str) -> Any:
    return func.json_each(column, _path(key, "v")).table_valued("value", "type")


def _holds_text(column: Any, key: str, value: str) -> ColumnElement[bool]:
    each = _items(column, key)
    return exists(select(1).select_from(each).where(each.c.type == "text", each.c.value == value))


def field_holds(dialect: str, column: Any, key: str, value: Any) -> ColumnElement[bool]:
    """Whether the row's value under ``key`` contains ``value``, as JSONB ``@>`` reads it.

    ``fields @> {key: {"v": value}}`` on Postgres, which a GIN ``jsonb_path_ops`` index
    answers. A string is contained by an equal string; a list by an array holding every one
    of its strings. On SQLite the same, through ``json_type`` and ``json_each``.
    """
    if dialect != SQLITE:
        return column.contains(cast({key: {"v": value}}, JSONB))
    path = _path(key, "v")
    if isinstance(value, list):
        return and_(
            func.json_type(column, path) == "array",
            *(_holds_text(column, key, item) for item in value),
        )
    return and_(func.json_type(column, path) == "text", func.json_extract(column, path) == value)


def field_has_any(
    dialect: str, column: Any, key: str, values: Sequence[str]
) -> ColumnElement[bool]:
    """Whether the value under ``key`` is, or is an array holding, one of ``values``.

    JSONB ``?|`` on Postgres; ``json_each`` on SQLite, which reads a string as itself.
    """
    if dialect != SQLITE:
        return column[key]["v"].has_any(array(list(values), type_=Text))
    each = _items(column, key)
    return exists(
        select(1).select_from(each).where(each.c.type == "text", each.c.value.in_(list(values)))
    )


def field_elements(
    dialect: str, column: Any, key: str, source: Any, conditions: Sequence[Any]
) -> Select[Any]:
    """One row per item of the array under ``key``, for the rows of ``source`` matching
    ``conditions``, as the column ``value``.

    ``jsonb_array_elements_text`` on Postgres; a join to ``json_each`` on SQLite. A row without
    the key contributes no item on either.
    """
    if dialect == SQLITE:
        each = _items(column, key)
        return (
            select(each.c.value.label("value"))
            .select_from(source)
            .join(each, true())
            .where(*conditions)
        )
    return (
        select(func.jsonb_array_elements_text(column[key]["v"]).label("value"))
        .select_from(source)
        .where(*conditions)
    )


def _jsonb(value: Any) -> Any:
    return cast(literal(value, JSONB), JSONB)


def _json(value: Any) -> Any:
    """``value`` bound as JSON text and read by SQLite as JSON, not as a string."""
    return func.json(literal(value, JSON()))


# Path/value pairs per ``json_set``/``json_insert`` call. SQLite caps a function at 127
# arguments by default (``SQLITE_MAX_FUNCTION_ARG``), and a call takes ``1 + 2 × pairs``.
_PAIRS_PER_CALL = 50
# Paths per ``json_remove`` call, under the same cap: a call takes ``1 + paths``.
_PATHS_PER_CALL = 100


def _per_key(function: Any, column: Any, patch: Mapping[str, Any]) -> Any:
    """``function(column, path, value, ...)`` over every key of ``patch``, nested one call
    per ``_PAIRS_PER_CALL`` keys so no call passes SQLite's argument limit."""
    items = list(patch.items())
    merged = column
    for start in range(0, len(items), _PAIRS_PER_CALL):
        args: list[Any] = [merged]
        for key, value in items[start : start + _PAIRS_PER_CALL]:
            args += [_path(key), _json(value)]
        merged = function(*args, type_=JSONType)
    return merged


def fields_written(
    dialect: str, column: Any, patch: Mapping[str, Any], removed: Sequence[str]
) -> Any:
    """``column`` with ``patch``'s keys replaced whole and ``removed``'s keys dropped.

    ``(fields || patch) - removed`` on Postgres; ``json_remove(json_set(...))`` on SQLite.
    """
    if dialect == SQLITE:
        merged = _per_key(func.json_set, column, patch) if patch else column
        paths = [_path(k) for k in removed]
        for start in range(0, len(paths), _PATHS_PER_CALL):
            merged = func.json_remove(
                merged, *paths[start : start + _PATHS_PER_CALL], type_=JSONType
            )
        return merged
    merged = column.op("||")(_jsonb(dict(patch))).self_group()
    if removed:
        merged = merged.op("-")(cast(literal(list(removed), ARRAY(Text)), ARRAY(Text)))
    return merged


def fields_filled(dialect: str, column: Any, patch: Mapping[str, Any]) -> Any:
    """``column`` with ``patch``'s keys added where absent; a key already held wins.

    ``patch || fields`` on Postgres; ``json_insert``, which sets only a missing path, on SQLite.
    """
    if dialect == SQLITE:
        return _per_key(func.json_insert, column, patch) if patch else column
    return _jsonb(dict(patch)).op("||")(column)
