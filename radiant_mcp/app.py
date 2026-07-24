"""Starlette application factory — route wiring and CORS middleware."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route, Mount

from .health import health_check, ready_check
from .oauth import protected_resource_metadata


def create_app(mcp_app):
    """Create the combined Starlette app with health endpoints and MCP."""
    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/ready", ready_check, methods=["GET"]),
        # RFC 9728 protected-resource metadata. AS metadata is NOT served here —
        # the native KeycloakAuthProvider points clients at Keycloak's realm.
        #
        # MCP_BASE_URL is the server root and the MCP app is mounted at /mcp, so
        # the provider advertises this metadata (in its 401 WWW-Authenticate) at
        # the BARE root path. That's the URL clients fetch; serve it there. If it
        # 404s, the client can't find the authorization server and falls back to
        # <origin>/authorize → 404.
        Route("/.well-known/oauth-protected-resource",
              protected_resource_metadata, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    return Starlette(routes=routes, middleware=middleware, lifespan=mcp_app.lifespan)
