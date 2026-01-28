"""
Wrapper for mcp-server-starrocks that adds a /health endpoint.
"""
import argparse
import asyncio
import os
import sys

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
import uvicorn

# Import the MCP server components
from mcp_server_starrocks.server import mcp
from mcp_server_starrocks.connection_health_checker import (
    start_connection_health_checker,
    stop_connection_health_checker,
    check_connection_health
)
from mcp_server_starrocks.db_client import get_db_client


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


def create_app(mcp_app):
    """Create the combined Starlette app with health endpoints and MCP."""
    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/ready", ready_check, methods=["GET"]),
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


if __name__ == "__main__":
    asyncio.run(main())
