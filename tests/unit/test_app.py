"""``app.create_app``: the HTTP routes around the MCP endpoint."""

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.testclient import TestClient

from radiant_mcp.app import create_app


@asynccontextmanager
async def _no_lifespan(_app):
    yield


def test_cors_allows_any_origin_but_no_credentials(configure):
    mcp_app = Starlette()
    mcp_app.lifespan = _no_lifespan
    client = TestClient(create_app(mcp_app))
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    # Bearer tokens only: a browser must never send cookies cross-origin to this server.
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
