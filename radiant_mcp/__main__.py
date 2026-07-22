"""
Radiant MCP Server — main entrypoint.

Wrapper for mcp-server-starrocks that adds a /health endpoint
and optional OAuth 2.1 authentication via Keycloak (OIDC).

When KEYCLOAK_OIDC_CONFIG_URL is set, the server:
  1. Configures OIDCProxy for OAuth 2.1 + PKCE flow via Keycloak
  2. Wraps the DB client so each MCP tool call executes queries
     under the authenticated user's identity (JWT → StarRocks)
  3. Falls back to the original static-credential client when no
     token is present (health checks, startup, etc.)

Without KEYCLOAK_OIDC_CONFIG_URL, the server behaves exactly as before.
"""

import argparse
import os

import uvicorn

import mcp_server_starrocks.server as sr_server
from mcp_server_starrocks.server import mcp
from mcp_server_starrocks.connection_health_checker import (
    start_connection_health_checker,
    stop_connection_health_checker,
)
from mcp_server_starrocks.db_client import get_db_client
from mcp_server_starrocks.db_summary_manager import DatabaseSummaryManager

from .app import create_app
from .jwt_db_client import JWTDBClient

OAUTH_ENABLED = bool(os.getenv('KEYCLOAK_OIDC_CONFIG_URL'))


async def main():
    parser = argparse.ArgumentParser(description='StarRocks MCP Server with Health Endpoint')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Server host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Server port (default: 8000)')

    args = parser.parse_args()

    # Verify database connection on startup
    db_client = get_db_client()
    print(f"Starting server on {args.host}:{args.port}")
    print(f"Default database: {db_client.default_database or 'None'}")

    # ---- OAuth 2.1 configuration (optional) ----
    if OAUTH_ENABLED:
        from fastmcp.server.auth import MultiAuth
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        audience = os.getenv("KEYCLOAK_AUDIENCE", None)
        required_scopes = os.getenv("OAUTH_REQUIRED_SCOPES", "openid").split(",")

        oidc = OIDCProxy(
            config_url=os.getenv("KEYCLOAK_OIDC_CONFIG_URL"),
            client_id=os.getenv("KEYCLOAK_CLIENT_ID"),
            client_secret=os.getenv("KEYCLOAK_CLIENT_SECRET"),
            audience=audience,
            base_url=os.getenv("MCP_BASE_URL", "http://localhost:8000/mcp"),
            required_scopes=required_scopes,
        )

        # fastmcp 3.x OIDCProxy only accepts tokens minted through its own
        # OAuth/DCR flow (external self-discovering clients like Claude Desktop).
        # Portal clients already hold a Keycloak token and present it directly,
        # so add a JWKS verifier as a fallback source. MultiAuth tries the proxy
        # first, then this verifier; routes/metadata still come from the proxy.
        direct_verifier = oidc.get_token_verifier(
            audience=audience,
            required_scopes=required_scopes,
        )
        mcp.auth = MultiAuth(server=oidc, verifiers=[direct_verifier])
        print("OAuth 2.1 enabled (Keycloak OIDC) + direct-JWT fallback for portal clients")

        # Monkey-patch: wrap DB client with JWT-aware version
        jwt_client = JWTDBClient(db_client)
        sr_server.db_client = jwt_client
        sr_server.db_summary_manager = DatabaseSummaryManager(jwt_client)
        print("JWTDBClient installed — queries will run under user identity")
    else:
        print("OAuth disabled (no KEYCLOAK_OIDC_CONFIG_URL set)")

    # Start connection health checker
    start_connection_health_checker()

    try:
        # Get the MCP ASGI app for streamable-http transport
        mcp_app = mcp.http_app(path="/")

        # Create combined app
        app = create_app(mcp_app)

        # Run with uvicorn
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        stop_connection_health_checker()
