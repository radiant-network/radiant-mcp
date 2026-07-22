"""Health and readiness endpoints (unauthenticated)."""

from starlette.responses import JSONResponse

from mcp_server_starrocks.connection_health_checker import check_connection_health


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
