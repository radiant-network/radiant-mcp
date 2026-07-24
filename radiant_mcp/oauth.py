"""OAuth .well-known metadata workaround endpoints.

With the native KeycloakAuthProvider the MCP server is a pure resource server:
authorization-server metadata lives on Keycloak, so we only serve RFC 9728
protected-resource metadata here. It advertises Keycloak's realm as the
authorization server, which is where clients then run OIDC discovery + DCR.
"""

import json
import os

from starlette.responses import Response


async def protected_resource_metadata(request):
    """RFC 9728: OAuth protected resource metadata with normalised URLs.

    We serve this by hand (rather than relying on the provider's built-in
    route) for two reasons the framework's version doesn't handle cleanly:
      * Pydantic's AnyHttpUrl serialisation appends trailing slashes that
        strict MCP clients reject on exact comparison.
      * We register it at both the RFC canonical root path
        (/.well-known/oauth-protected-resource/mcp) and the mount-relative
        path (/mcp/.well-known/oauth-protected-resource) — see app.py.

    ``authorization_servers`` points at the Keycloak realm, so clients fetch
    authorization-server metadata straight from Keycloak.
    """
    # MCP_BASE_URL is the server root (e.g. http://localhost:8000); the MCP
    # resource itself lives under /mcp.
    resource = os.getenv("MCP_BASE_URL", "http://localhost:8000").rstrip("/") + "/mcp"
    realm = os.getenv("KEYCLOAK_REALM_URL", "").rstrip("/")
    body = json.dumps({
        "resource": resource,
        "authorization_servers": [realm],
        "scopes_supported": ["openid"],
        "bearer_methods_supported": ["header"],
    })
    return Response(body, media_type="application/json")
