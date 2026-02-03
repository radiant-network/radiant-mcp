"""
Wrapper for MCP servers with health endpoints.

Endpoints:
- /mcp: Direct StarRocks SQL access (mcp-server-starrocks)
- /mcp2: Cube.dev semantic layer access (cube_mcp)
- /health: Health check for database connectivity
- /ready: Readiness check
"""
import argparse
import asyncio
import contextlib
import os
import sys

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
import uvicorn

# Import the StarRocks MCP server components
from mcp_server_starrocks.server import mcp
from mcp_server_starrocks.connection_health_checker import (
    start_connection_health_checker,
    stop_connection_health_checker,
    check_connection_health
)
from mcp_server_starrocks.db_client import get_db_client

# Import the Cube.dev MCP server
from cube_mcp import cube_mcp


async def health_check(request):
    """Health check endpoint that verifies database connectivity."""
    db_healthy = check_connection_health()
    status = "healthy" if db_healthy else "unhealthy"
    status_code = 200 if db_healthy else 503

    return JSONResponse(
        {"status": status, "database": "connected" if db_healthy else "disconnected"},
        status_code=status_code
    )


async def ready_check(request):
    """Readiness check - returns ok if server is ready to accept requests."""
    return JSONResponse({"status": "ready"})


def create_app(mcp_app, cube_mcp_app):
    """Create the combined Starlette app with health endpoints and MCP servers.

    Args:
        mcp_app: StarRocks direct SQL MCP ASGI app (mounted at /mcp)
        cube_mcp_app: Cube.dev semantic layer MCP ASGI app (mounted at /mcp2)
    """
    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/ready", ready_check, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
        Mount("/mcp2", app=cube_mcp_app),
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

    @contextlib.asynccontextmanager
    async def combined_lifespan(app):
        """Combine lifespans from both MCP apps."""
        async with mcp_app.lifespan(app):
            async with cube_mcp_app.lifespan(app):
                yield

    return Starlette(routes=routes, middleware=middleware, lifespan=combined_lifespan)


async def main():
    parser = argparse.ArgumentParser(description='Radiant MCP Server (StarRocks + Cube.dev)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Server host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Server port (default: 8000)')

    args = parser.parse_args()

    # Verify database connection on startup
    db_client = get_db_client()
    cube_api_url = os.environ.get("CUBE_API_URL", "http://localhost:4000/cubejs-api/v1")

    print(f"Starting server on {args.host}:{args.port}")
    print(f"  /mcp  - StarRocks direct SQL (database: {db_client.default_database or 'None'})")
    print(f"  /mcp2 - Cube.dev semantic layer (API: {cube_api_url})")

    # Start connection health checker
    start_connection_health_checker()

    try:
        # Get the StarRocks MCP ASGI app for streamable-http transport
        mcp_app = mcp.http_app(path="/")

        # Get the Cube.dev MCP ASGI app
        cube_mcp_app = cube_mcp.http_app(path="/")

        # Create combined app with both MCP servers
        app = create_app(mcp_app, cube_mcp_app)

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


if __name__ == "__main__":
    asyncio.run(main())
