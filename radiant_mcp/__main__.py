"""
Radiant MCP Server — main entrypoint.

Wrapper for mcp-server-starrocks that adds a /health endpoint and mandatory
OAuth 2.1 authentication via Keycloak (OIDC).

KEYCLOAK_REALM_URL is required — the server refuses to boot without it. On
startup it:
  1. Configures the native fastmcp KeycloakAuthProvider — a pure JWT
     resource server that validates incoming Bearer tokens against
     Keycloak's JWKS. Self-discovering clients (Claude Desktop) obtain
     tokens via Keycloak's own Dynamic Client Registration + PKCE;
     portal clients (LibreChat) present a raw Keycloak token directly.
     Both are accepted by the same verifier.
  2. Wraps the DB client so each MCP tool call executes queries
     under the authenticated user's identity (JWT → StarRocks).

The component holds no StarRocks credentials: there is no static-credential
fallback and no DB-connection health check.
"""

import argparse
import os

import uvicorn

import mcp_server_starrocks.server as sr_server
from mcp_server_starrocks.server import mcp
from mcp_server_starrocks.db_client import get_db_client
from mcp_server_starrocks.db_summary_manager import DatabaseSummaryManager

from .app import create_app
from .jwt_db_client import JWTDBClient


async def main():
    parser = argparse.ArgumentParser(description='StarRocks MCP Server with Health Endpoint')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Server host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Server port (default: 8000)')

    args = parser.parse_args()

    # OAuth is mandatory — refuse to boot without a Keycloak realm.
    realm_url = os.getenv("KEYCLOAK_REALM_URL")
    if not realm_url:
        raise SystemExit("KEYCLOAK_REALM_URL is required")

    db_client = get_db_client()
    print(f"Starting server on {args.host}:{args.port}")
    print(f"Default database: {db_client.default_database or 'None'}")

    # ---- OAuth 2.1 configuration ----
    from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

    # audience is optional: StarRocks' authentication_jwt only checks `aud`
    # when its user is created with `required_audience`, which we don't set.
    # Leaving this None lets DCR-registered clients (each with their own
    # client_id and no per-client audience mapper) validate too.
    audience = os.getenv("KEYCLOAK_AUDIENCE", None)
    required_scopes = os.getenv("OAUTH_REQUIRED_SCOPES", "openid").split(",")

    # Native fastmcp Keycloak provider: a pure JWT resource server. It holds
    # no client secret and mints no tokens — it only validates incoming
    # Bearer JWTs against Keycloak's JWKS (RS256, issuer, exp, scopes, and
    # optionally aud). Self-discovering clients (Claude Desktop) get tokens
    # via Keycloak's own DCR + PKCE; portal clients (LibreChat) present a raw
    # Keycloak token. Both hit the same verifier, so no MultiAuth is needed.
    mcp.auth = KeycloakAuthProvider(
        realm_url=realm_url,
        base_url=os.getenv("MCP_BASE_URL", "http://localhost:8000"),
        audience=audience,
        required_scopes=required_scopes,
    )
    print("OAuth 2.1 enabled (Keycloak native provider — JWT verifier + DCR)")

    # Monkey-patch: wrap DB client with JWT-aware version. Every query runs
    # under the caller's identity — this component holds no StarRocks credential.
    jwt_client = JWTDBClient(db_client)
    sr_server.db_client = jwt_client
    sr_server.db_summary_manager = DatabaseSummaryManager(jwt_client)
    print("JWTDBClient installed — queries will run under user identity")

    # Get the MCP ASGI app for streamable-http transport
    mcp_app = mcp.http_app(path="/")

    # Create combined app
    app = create_app(mcp_app)

    # Run with uvicorn.
    # proxy_headers + forwarded_allow_ips let uvicorn honour the
    # X-Forwarded-Proto/-For headers set by the TLS-terminating proxy
    # (e.g. AWS ALB). Without this, uvicorn assumes http and Starlette's
    # trailing-slash redirects (and OAuth metadata URLs) are emitted as
    # http:// — which breaks HTTPS clients and the OAuth discovery flow.
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
    server = uvicorn.Server(config)
    await server.serve()
