"""StarRocks access under the caller's JWT.

Every statement opens a one-shot MySQL connection authenticated with the
caller's Keycloak JWT through the ``authentication_openid_connect_client``
plugin, so StarRocks applies that user's own grants. There is no pool, no
service account and no static-credential fallback: a call without a JWT
fails. ``STARROCKS_HOST``, ``STARROCKS_PORT`` and ``STARROCKS_DB`` only say
where to connect.

Each session gets a ``query_timeout``, and the socket a read timeout a little
longer, so one slow query cannot hold a worker thread forever. ``max_rows``
reads at most that many rows plus one, to flag a truncated result without
loading the rest.
"""

import base64
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import mysql.connector
from mysql.connector.errors import Error as MySQLError

_HOST_ENV = "STARROCKS_HOST"
_PORT_ENV = "STARROCKS_PORT"
_DB_ENV = "STARROCKS_DB"
_CONNECT_TIMEOUT_ENV = "STARROCKS_CONNECTION_TIMEOUT"
_QUERY_TIMEOUT_ENV = "STARROCKS_QUERY_TIMEOUT"

_QUERY_TIMEOUT_DEFAULT = 60
# Extra seconds the socket waits beyond query_timeout, so StarRocks' own
# timeout error normally arrives first.
_READ_TIMEOUT_MARGIN = 5

_DB_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")

AUTH_REQUIRED = "Authentication required: no JWT in request context"


class StarRocksError(RuntimeError):
    """The connection or the statement failed."""


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def default_database() -> str | None:
    return os.getenv(_DB_ENV) or None


def query_timeout() -> int:
    return _int_env(_QUERY_TIMEOUT_ENV, _QUERY_TIMEOUT_DEFAULT)


def database_name(value: str) -> str:
    """A database or table name that is safe to write into a statement."""
    if not isinstance(value, str) or not _DB_NAME.match(value):
        raise StarRocksError(f"invalid name: {value!r}")
    return value


def _username(token: str) -> str:
    """The JWT ``sub`` claim, which is the StarRocks user name.

    The token is not verified here: the auth provider already checked it
    against Keycloak's JWKS, and StarRocks checks it again.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("sub", "unknown")


@contextmanager
def connect(token: str | None, database: str | None = None) -> Iterator[Any]:
    """Open a connection as the caller; it is closed on exit."""
    if not token:
        raise StarRocksError(AUTH_REQUIRED)
    if database is not None:
        database_name(database)

    # The plugin reads the token from a file: 0600, removed once connected.
    fd, token_path = tempfile.mkstemp(suffix=".jwt")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)

    timeout = query_timeout()
    try:
        conn = mysql.connector.connect(
            host=os.getenv(_HOST_ENV, "localhost"),
            port=_int_env(_PORT_ENV, 9030),
            user=_username(token),
            auth_plugin="authentication_openid_connect_client",
            openid_token_file=token_path,
            database=database or default_database() or "",
            autocommit=True,
            ssl_verify_cert=False,
            ssl_verify_identity=False,
            connect_timeout=_int_env(_CONNECT_TIMEOUT_ENV, 10),
            read_timeout=timeout + _READ_TIMEOUT_MARGIN,
            init_command=f"SET query_timeout = {int(timeout)}",
        )
    except MySQLError as exc:
        raise StarRocksError(str(exc)) from exc
    finally:
        with suppress(OSError):
            os.unlink(token_path)

    try:
        yield conn
    finally:
        # A close failure must not hide the result.
        with suppress(Exception):
            conn.close()


def query(
    token: str | None,
    sql: str,
    params: tuple | dict | None = None,
    *,
    database: str | None = None,
    max_rows: int | None = None,
) -> Result:
    """Run one statement as the caller and return its first result set.

    ``params`` are bound by the MySQL connector: ``%s`` placeholders with a
    tuple, ``%(name)s`` with a dict. A literal ``%`` must then be ``%%``.
    """
    with connect(token, database) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            if not cursor.description:
                return Result()
            columns = [d[0] for d in cursor.description]
            if max_rows is None:
                return Result(columns, list(cursor.fetchall()))
            rows = list(cursor.fetchmany(max_rows + 1))
            return Result(columns, rows[:max_rows], truncated=len(rows) > max_rows)
        except MySQLError as exc:
            raise StarRocksError(str(exc)) from exc
        finally:
            # Unread rows of a truncated result can make the close fail.
            with suppress(Exception):
                cursor.close()
