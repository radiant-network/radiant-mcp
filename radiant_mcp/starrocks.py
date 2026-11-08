"""StarRocks access under the caller's JWT.

Every statement opens a one-shot MySQL connection authenticated with the
caller's Keycloak JWT through the ``authentication_openid_connect_client``
plugin, so StarRocks applies that user's own grants. There is no pool, no
service account and no static-credential fallback: a call without a JWT
fails. ``STARROCKS_HOST``, ``STARROCKS_PORT`` and ``STARROCKS_DB`` only say
where to connect.

The connection is always TLS (StarRocks requires it for JWT logins). With
``STARROCKS_SSL_VERIFY`` the FE certificate chain and hostname are checked
against ``STARROCKS_SSL_CA``, or the system CA bundle when it is unset.

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

from .settings import get_settings

# Extra seconds the socket waits beyond query_timeout, so StarRocks' own
# timeout error normally arrives first.
_READ_TIMEOUT_MARGIN = 5

_DB_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")

AUTH_REQUIRED = "Authentication required: no JWT in request context"


# MySQL error numbers for access denied, and for an unknown database, column or
# table. StarRocks also reports many unrelated errors, timeouts included, as 1064
# (HY000), so a statement it cannot parse or analyse is told by its message.
_DENIED_ERRNOS = {1044, 1045, 1142, 1143, 1227}
_INVALID_ERRNOS = {1049, 1054, 1146}
_INVALID_MESSAGE = re.compile(
    r"getting syntax error|getting analyzing error|cannot be resolved|unknown (database|table|column)"
    r"|no viable alternative|mismatched input|syntax error",
    re.IGNORECASE,
)


class StarRocksError(RuntimeError):
    """The connection or the statement failed.

    ``kind`` says whose fault it is: ``denied`` (the caller's grants),
    ``invalid`` (the statement or a name), or ``failed`` (StarRocks itself: down,
    timed out, internal error).
    """

    def __init__(self, message: str, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def _error(exc: MySQLError) -> StarRocksError:
    message = str(exc)
    if exc.errno in _DENIED_ERRNOS or "access denied" in message.lower():
        kind = "denied"
    elif (exc.errno in _INVALID_ERRNOS or _INVALID_MESSAGE.search(message)
          or str(exc.sqlstate or "").startswith("42")):
        kind = "invalid"
    else:
        kind = "failed"
    return StarRocksError(message, kind)


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False


def default_database() -> str | None:
    return get_settings().starrocks_db


def query_timeout() -> int:
    return get_settings().starrocks_query_timeout


def database_name(value: str) -> str:
    """A database or table name that is safe to write into a statement."""
    if not isinstance(value, str) or not _DB_NAME.match(value):
        raise StarRocksError(f"invalid name: {value!r}", "invalid")
    return value


def _username(token: str) -> str:
    """The JWT ``sub`` claim, which is the StarRocks user name.

    The token is not verified here: the auth provider already checked it
    against Keycloak's JWKS, and StarRocks checks it again.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("sub", "unknown")


def _tls(ca_file: str | None) -> dict:
    """Connector TLS arguments: verify chain and hostname against ``ca_file``, or nothing."""
    if ca_file is None:
        return {"ssl_verify_cert": False, "ssl_verify_identity": False}
    return {"ssl_ca": ca_file, "ssl_verify_cert": True, "ssl_verify_identity": True}


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

    settings = get_settings()
    timeout = settings.starrocks_query_timeout
    try:
        conn = mysql.connector.connect(
            host=settings.starrocks_host,
            port=settings.starrocks_port,
            user=_username(token),
            auth_plugin="authentication_openid_connect_client",
            openid_token_file=token_path,
            database=database or settings.starrocks_db or "",
            autocommit=True,
            **_tls(settings.starrocks_ca_file),
            connect_timeout=settings.starrocks_connection_timeout,
            read_timeout=timeout + _READ_TIMEOUT_MARGIN,
            init_command=f"SET query_timeout = {int(timeout)}",
        )
    except MySQLError as exc:
        raise _error(exc) from exc
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
            raise _error(exc) from exc
        finally:
            # Unread rows of a truncated result can make the close fail.
            with suppress(Exception):
                cursor.close()
