"""OAuth .well-known metadata workaround endpoints."""

import json
import os

from starlette.responses import Response


async def protected_resource_metadata(request):
    """RFC 9728: OAuth protected resource metadata with normalised URLs.

    The MCP framework's built-in endpoint adds a trailing slash to URLs
    via Pydantic's AnyHttpUrl serialisation.  MCP clients do a strict
    comparison, so we serve our own version with clean URLs.
    """
    base = os.getenv("MCP_BASE_URL", "http://localhost:8000/mcp").rstrip("/")
    body = json.dumps({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["openid"],
        "bearer_methods_supported": ["header"],
    })
    return Response(body, media_type="application/json")


async def auth_server_metadata(request):
    """RFC 8414: OAuth authorization server metadata.

    Proxies the MCP framework's own metadata from
    ``/mcp/.well-known/oauth-authorization-server`` so it is also
    available at the RFC 8414 canonical path.
    """
    import httpx

    inner_url = "http://localhost:8000/mcp/.well-known/oauth-authorization-server"
    async with httpx.AsyncClient() as client:
        resp = await client.get(inner_url)
    return Response(resp.content, status_code=resp.status_code,
                    media_type="application/json")
