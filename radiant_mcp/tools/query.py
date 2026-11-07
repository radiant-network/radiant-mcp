"""The raw SQL tools: ``read_query`` and ``table_overview``.

They are the agents' last resort, when no Radiant tool answers. Both run
under the caller's JWT (``starrocks``), so StarRocks applies the caller's own
grants on every call: nothing is cached across calls. ``read_query`` returns
at most ``STARROCKS_MAX_ROWS`` rows and says when it cut the result.
"""

import os
from typing import Callable, Optional

import anyio

from .. import starrocks
from . import _api
from ._sql import plain

_MAX_ROWS_ENV = "STARROCKS_MAX_ROWS"
_MAX_ROWS_DEFAULT = 500
_SAMPLE_ROWS = 3


def _max_rows() -> int:
    try:
        return max(1, int(os.getenv(_MAX_ROWS_ENV, _MAX_ROWS_DEFAULT)))
    except ValueError:
        return _MAX_ROWS_DEFAULT


def _rows(result: starrocks.Result) -> list[list]:
    return [[plain(v) for v in row] for row in result.rows]


async def _run(fn: Callable[..., dict], *args) -> dict:
    """Run ``fn(token, *args)`` on a worker thread; failures become ``{"error"}``.

    The JWT is read on the event loop: the auth context does not follow the
    thread.
    """
    token = _api.get_jwt_token()
    if not token:
        return {"error": starrocks.AUTH_REQUIRED}
    try:
        return await anyio.to_thread.run_sync(lambda: fn(token, *args))
    except starrocks.StarRocksError as exc:
        return {"error": f"StarRocks error: {exc}"}


def _read_query(token: str, query: str, db: Optional[str]) -> dict:
    max_rows = _max_rows()
    result = starrocks.query(token, query, database=db, max_rows=max_rows)
    out = {"columns": result.columns, "rows": _rows(result), "returned": len(result.rows)}
    if result.truncated:
        out["truncated"] = True
        out["note"] = (f"Only the first {max_rows} rows are returned. Aggregate (COUNT, GROUP BY) "
                       "or add a narrower WHERE / LIMIT instead of paging.")
    return out


async def read_query(query: str, db: Optional[str] = None) -> dict:
    """Run one read-only SQL statement in StarRocks, as the caller.

    Last resort: call ``describe_schema`` first and prefer the Radiant tools.
    One statement per call. The result holds at most a few hundred rows; when
    more exist, ``truncated`` is true, so aggregate or filter instead.

    Args:
        query: The SQL statement, e.g. a SELECT with a LIMIT.
        db: Database to run it in (e.g. ``cbtn_tenant``); table names may
            also be prefixed with their database instead.
    """
    return await _run(_read_query, query, db)


def _table_overview(token: str, table: str) -> dict:
    db, _, name = table.rpartition(".")
    db = db or starrocks.default_database()
    if not db:
        raise starrocks.StarRocksError(f"no database given for table {table!r} (use db.table)")
    full = f"`{starrocks.database_name(db)}`.`{starrocks.database_name(name)}`"
    count = starrocks.query(token, f"SELECT COUNT(*) FROM {full}").rows[0][0]
    columns = starrocks.query(token, f"DESCRIBE {full}")
    out = {
        "table": f"{db}.{name}",
        "row_count": plain(count),
        "columns": [dict(zip(columns.columns, row)) for row in _rows(columns)],
    }
    if count:
        sample = starrocks.query(token, f"SELECT * FROM {full} LIMIT {_SAMPLE_ROWS}")
        out["sample"] = {"columns": sample.columns, "rows": _rows(sample)}
    return out


async def table_overview(table: str) -> dict:
    """Get a table's columns, total row count and up to 3 sample rows, as the caller.

    Prefer ``describe_schema``, which documents the tables to use, their
    partitions and stored enum values.

    Args:
        table: ``db.table`` (e.g. ``cbtn_tenant.germline__snv__occurrence``).
    """
    return await _run(_table_overview, table)
