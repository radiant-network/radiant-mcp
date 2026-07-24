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
        # RFC 9728: serve protected-resource metadata at the root-level paths.
        # The MCP framework serves this under /mcp/.well-known/... but clients
        # expect the RFC canonical path at the root. Authorization-server
        # metadata is NOT served here — the native KeycloakAuthProvider points
        # clients directly at Keycloak's realm for that.
        #
        # Register BOTH the slashless and trailing-slash variants explicitly.
        # fastmcp's WWW-Authenticate header advertises the resource-metadata
        # URL *with* a trailing slash (".../mcp/"); if only the slashless route
        # exists, Starlette 307-redirects the slashed request, and strict MCP
        # clients (e.g. Claude Desktop) that don't follow redirects on the
        # metadata fetch fail OAuth discovery ("not a valid MCP server").
        Route("/.well-known/oauth-protected-resource/mcp",
              protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp/",
              protected_resource_metadata, methods=["GET"]),
        # Also override the mount-relative path (trailing-slash fix).
        Route("/mcp/.well-known/oauth-protected-resource",
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
