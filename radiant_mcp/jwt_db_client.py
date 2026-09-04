"""JWTDBClient — per-request JWT-authenticated connections to StarRocks."""

import json
import os
import tempfile
import time
from typing import Literal, Optional

import mysql.connector
from mysql.connector.errors import Error as MySQLError


class JWTDBClient:
    """Wrapper around DBClient that uses the caller's JWT token to authenticate
    against StarRocks via the ``authentication_openid_connect_client`` plugin.

    Every query runs under the authenticated user's identity: a direct,
    one-shot MySQL connection is opened with the caller's JWT (bypassing the
    pool). There is no static-credential fallback — a call with no JWT in the
    request context fails rather than running as a shared user.
    """

    def __init__(self, original_client):
        self._original = original_client
        # Expose attributes that tools read directly
        self.default_database = original_client.default_database
        self.enable_dummy_test = original_client.enable_dummy_test
        self.enable_arrow_flight_sql = original_client.enable_arrow_flight_sql

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _get_jwt_token() -> Optional[str]:
        """Retrieve the raw JWT string from the current MCP auth context."""
        try:
            from fastmcp.server.dependencies import get_access_token
            access_token = get_access_token()
            if access_token is not None:
                return access_token.token
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_username(token_str: str) -> str:
        """Decode the JWT payload (without verification — already verified by
        the KeycloakAuthProvider against Keycloak's JWKS) and return the ``sub``
        claim, which is the StarRocks username (matched against the user's
        ``principal_field: sub``)."""
        import base64
        # JWT = header.payload.signature
        payload_b64 = token_str.split('.')[1]
        # Fix padding
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get('sub', 'unknown')

    def _create_jwt_connection(self, token_str: str):
        """Create a one-shot MySQL connection authenticated with a JWT.

        mysql-connector-python ≥ 9.1.0 supports the
        ``authentication_openid_connect_client`` auth plugin which reads the
        token from a file specified via ``openid_token_file``.

        """
        username = self._extract_username(token_str)

        # Write token to a secure temp file
        fd, token_path = tempfile.mkstemp(suffix='.jwt')
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, token_str.encode('utf-8'))
        finally:
            os.close(fd)

        try:
            conn = mysql.connector.connect(
                host=self._original.connection_params.get('host', 'localhost'),
                port=int(self._original.connection_params.get('port', 9030)),
                user=username,
                auth_plugin='authentication_openid_connect_client',
                openid_token_file=token_path,
                database=self._original.default_database or '',
                autocommit=True,
                ssl_verify_cert=False,
                ssl_verify_identity=False,
                connection_timeout=int(
                    self._original.connection_params.get('connection_timeout', 10)
                ),
                connect_timeout=int(
                    self._original.connection_params.get('connect_timeout', 10)
                ),
            )
        finally:
            # Remove the temp file immediately after the connection is established
            try:
                os.unlink(token_path)
            except OSError:
                pass

        return conn

    # -- public API (same interface as DBClient) -----------------------------

    def execute(
        self,
        statement: str,
        db: Optional[str] = None,
        return_format: Literal["raw", "pandas"] = "raw",
    ):
        return self.execute_with_token(
            self._get_jwt_token(), statement, db=db, return_format=return_format
        )

    def execute_with_token(
        self,
        token: Optional[str],
        statement: str,
        db: Optional[str] = None,
        return_format: Literal["raw", "pandas"] = "raw",
    ):
        """Same as ``execute`` but with an explicitly supplied JWT.

        Use this from code that already pulled the token out of the MCP auth
        context before hopping to a worker thread (e.g. the Radiant API tools,
        which run their sync bodies in ``anyio.to_thread.run_sync``), so the
        query does not depend on contextvar propagation across threads.
        """
        if token is None:
            from mcp_server_starrocks.db_client import ResultSet
            return ResultSet(
                success=False,
                error_message="Authentication required: no JWT in request context",
            )

        conn = None
        try:
            conn = self._create_jwt_connection(token)
            # Switch database if specified
            if db and db != self.default_database:
                cursor_temp = conn.cursor()
                try:
                    cursor_temp.execute(f"USE `{db}`")
                except MySQLError as db_err:
                    cursor_temp.close()
                    from mcp_server_starrocks.db_client import ResultSet
                    return ResultSet(
                        success=False,
                        error_message=f"Error switching to database '{db}': {str(db_err)}",
                        execution_time=0,
                    )
                cursor_temp.close()
            return self._original._execute(conn, statement, None, return_format)
        except MySQLError as e:
            from mcp_server_starrocks.db_client import ResultSet
            return ResultSet(
                success=False,
                error_message=f"Error executing statement '{statement}': {str(e)}",
            )
        except Exception as e:
            from mcp_server_starrocks.db_client import ResultSet
            return ResultSet(
                success=False,
                error_message=f"Unexpected error executing statement '{statement}': {str(e)}",
            )
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def collect_perf_analysis_input(self, query: str, db: Optional[str] = None):
        token = self._get_jwt_token()
        if token is None:
            return {"error_message": "Authentication required: no JWT in request context"}

        conn = None
        try:
            conn = self._create_jwt_connection(token)
            # Switch database if specified
            if db and db != self.default_database:
                cursor_temp = conn.cursor()
                try:
                    cursor_temp.execute(f"USE `{db}`")
                except MySQLError as db_err:
                    return {"error_message": str(db_err)}
                finally:
                    cursor_temp.close()

            query_dump_result = self._original._execute(
                conn, "select get_query_dump(%s, %s)", (query, False)
            )
            if not query_dump_result.success:
                return {"error_message": query_dump_result.error_message}
            ret = {"query_dump": json.loads(query_dump_result.rows[0][0])}

            start_ts = time.time()
            profile_query = "/*+ SET_VAR (enable_profile='true') */ " + query
            query_result = self._original._execute(conn, profile_query)
            duration = time.time() - start_ts
            ret["duration"] = duration
            if not query_result.success:
                ret["error_message"] = query_result.error_message
                return ret
            ret["rows_returned"] = len(query_result.rows) if query_result.rows else 0

            query_id_result = self._original._execute(conn, "select last_query_id()")
            if not query_id_result.success:
                ret["error_message"] = query_id_result.error_message
                return ret
            ret["query_id"] = query_id_result.rows[0][0]

            query_profile = ''
            retry_count = 0
            while not query_profile and retry_count < 3:
                time.sleep(1 + retry_count)
                qp_result = self._original._execute(
                    conn, "select get_query_profile(%s)", (ret["query_id"],)
                )
                if qp_result.success:
                    query_profile = qp_result.rows[0][0]
                retry_count += 1
            if not query_profile:
                ret['error_message'] = "Failed to get query profile after 3 retries"
                return ret
            ret['profile'] = query_profile

            analyze_result = self._original._execute(
                conn, "ANALYZE PROFILE FROM %s", (ret["query_id"],)
            )
            if not analyze_result.success:
                ret["error_message"] = analyze_result.error_message
                return ret
            from mcp_server_starrocks.db_client import remove_ansi_codes
            analyze_text = '\n'.join(row[0] for row in analyze_result.rows)
            ret['analyze_profile'] = remove_ansi_codes(analyze_text)
            return ret

        except MySQLError as e:
            return {"error_message": str(e)}
        except Exception as e:
            return {"error_message": str(e)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def reset_connections(self):
        self._original.reset_connections()

    # Forward attribute access to the original client for anything we
    # don't explicitly override (e.g. connection_params).
    def __getattr__(self, name):
        return getattr(self._original, name)
