# OAuth 2.1 + StarRocks JWT Authentication for Radiant MCP

## Context

Currently, the MCP server (`server.py`) has no authentication layer. MCP requests are processed directly and StarRocks connections use static credentials (env vars). We want to:

1. Implement the full OAuth 2.1 flow (MCP spec) with **Keycloak** as IdP
2. Pass the user's JWT token to **StarRocks** which supports native JWT auth (v3.5.0+)
3. Each MCP request executes StarRocks queries under the authenticated user's identity

## Target Architecture

```
MCP Client (Claude Desktop, etc.)
    │
    │ 1. Discovers OAuth metadata (/.well-known/oauth-protected-resource)
    │ 2. OAuth 2.1 + PKCE flow via Keycloak (authorize → callback → token)
    │ 3. MCP request with Authorization: Bearer <jwt>
    ▼
server.py (Starlette + FastMCP)
    │
    │ FastMCP OIDCProxy validates JWT (Keycloak JWKS)
    │ AuthContextMiddleware populates auth_context_var
    │
    ▼
JWTDBClient (DBClient wrapper)
    │
    │ get_access_token() → retrieves JWT from context
    │ Creates per-request MySQL connection with JWT auth
    │
    ▼
StarRocks (authentication_jwt)
    │ Validates JWT against Keycloak JWKS
    │ Executes query under user's identity
```

## Files to Modify

| File | Action |
|------|--------|
| `server.py` | Modify — add OAuth config, JWTDBClient wrapper, monkey-patch |
| `Dockerfile` | Modify — ensure `authlib` is installed |

## Existing Dependencies (already installed via fastmcp)

- `fastmcp` 2.12.4 — contains `OIDCProxy`, `JWTVerifier`, `AccessToken`
- `authlib` — used by OIDCProxy for OAuth
- `mysql-connector-python` 9.5.0 — supports `authentication_openid_connect_client` (since 9.1.0)

No new dependencies required.

## Implementation Steps

### Step 1: OAuth Configuration (OIDCProxy → Keycloak)

In `server.py`, configure `mcp.auth` with `OIDCProxy` before calling `mcp.http_app()`:

```python
from fastmcp.server.auth.oidc_proxy import OIDCProxy

OAUTH_ENABLED = bool(os.getenv('KEYCLOAK_OIDC_CONFIG_URL'))

if OAUTH_ENABLED:
    auth = OIDCProxy(
        config_url=os.getenv("KEYCLOAK_OIDC_CONFIG_URL"),
        client_id=os.getenv("KEYCLOAK_CLIENT_ID"),
        client_secret=os.getenv("KEYCLOAK_CLIENT_SECRET"),
        audience=os.getenv("KEYCLOAK_AUDIENCE", None),
        base_url=os.getenv("MCP_BASE_URL", "http://localhost:8000/mcp"),
        required_scopes=os.getenv("OAUTH_REQUIRED_SCOPES", "openid").split(","),
    )
    mcp.auth = auth
```

This automatically adds (under `/mcp/`):
- `/.well-known/oauth-authorization-server` — AS metadata
- `/.well-known/oauth-protected-resource` — RFC 9728
- `/authorize` — proxy to Keycloak
- `/token` — proxy to Keycloak
- `/register` — local DCR

The `/health` and `/ready` endpoints remain unauthenticated (mounted outside `/mcp`).

### Step 2: JWTDBClient — Per-Request Connections with JWT

Create a wrapper class in `server.py` that intercepts calls to `execute()`:

```python
from fastmcp.server.dependencies import get_access_token
```

**`JWTDBClient` class**:
- Wraps the original `DBClient`
- In `execute()`: calls `get_access_token()` to retrieve the JWT
- If a token is present: creates a direct MySQL connection (not from the pool) with:
  - `user` = JWT claim `preferred_username` (or `sub`)
  - `auth_plugin` = `authentication_openid_connect_client`
  - `openid_token_file` = temporary file containing the JWT
- If no token (health check, startup): delegates to the original `DBClient`
- Cleanup of the temporary file in `finally`

**Token file management**:
- `tempfile.mkstemp(suffix='.jwt')` with permissions 0o600
- Writes raw JWT, closes the fd
- Deleted in `finally` after each request

**For `collect_perf_analysis_input`**:
- Same pattern: creates a JWT connection, then calls `self._original._execute(conn, ...)` for each sub-query on the same connection

### Step 3: Monkey-Patch of `mcp_server_starrocks` Module

The `mcp_server_starrocks.server` module imports `db_client` at module level (line 59):
```python
db_client = get_db_client()  # singleton
```

All tools use this module-level variable. The monkey-patch:
```python
import mcp_server_starrocks.server as sr_server

original_client = get_db_client()
jwt_client = JWTDBClient(original_client)
sr_server.db_client = jwt_client
```

The `db_summary_manager` (line 61) also uses `db_client` — it needs patching too:
```python
sr_server.db_summary_manager = get_db_summary_manager(jwt_client)
```

### Step 4: Environment Variables

New variables (optional — if absent, auth is disabled = backward compatible):

| Variable | Description |
|----------|-------------|
| `KEYCLOAK_OIDC_CONFIG_URL` | Keycloak OIDC discovery URL (e.g., `https://keycloak.example.com/realms/radiant/.well-known/openid-configuration`) |
| `KEYCLOAK_CLIENT_ID` | OAuth Client ID registered in Keycloak |
| `KEYCLOAK_CLIENT_SECRET` | Client secret |
| `KEYCLOAK_AUDIENCE` | Expected JWT audience (optional) |
| `MCP_BASE_URL` | Public URL of the MCP server (e.g., `https://mcp.example.com/mcp`) |
| `OAUTH_REQUIRED_SCOPES` | Required scopes, comma-separated (default: `openid`) |

### Step 5: Keycloak Configuration (Setup Guide)

- Create a realm (e.g., `radiant`)
- Create a client `radiant-mcp-server` (Confidential, Authorization Code + PKCE)
- Redirect URI: `{MCP_BASE_URL}/auth/callback`
- Map the `preferred_username` claim in tokens

### Step 6: StarRocks Configuration (Setup Guide)

```sql
CREATE USER 'alice' IDENTIFIED WITH authentication_jwt AS '{
  "jwks_url": "https://keycloak.example.com/realms/radiant/protocol/openid-connect/certs",
  "principal_field": "preferred_username",
  "required_issuer": "https://keycloak.example.com/realms/radiant",
  "required_audience": "radiant-mcp-server"
}';
GRANT SELECT ON *.* TO 'alice';
```

## Security Considerations

1. **Token files**: Created with 0o600 permissions, deleted immediately after use in `finally`. In production, use a tmpfs mount (`/dev/shm`)
2. **No pool for JWT**: Each request opens/closes a connection. Acceptable because MCP tool calls are infrequent (AI-driven)
3. **Fallback**: Without `KEYCLOAK_OIDC_CONFIG_URL`, the server works exactly as before (backward compatible)
4. **HTTPS**: Required in production for both Keycloak and MCP_BASE_URL
5. **Arrow Flight SQL**: JWTDBClient only supports MySQL protocol for JWT auth. If `enable_arrow_flight_sql` is enabled, falls back to the original client

## Verification / Testing

1. **Unit test**: Mock `get_access_token()`, verify `JWTDBClient` creates connections with correct params
2. **Integration test** with docker-compose:
   - Keycloak (realm + client + user)
   - StarRocks FE (JWT user configured)
   - radiant-mcp (with OAuth env vars)
3. **Manual test** with MCP Inspector:
   - Point to `http://localhost:8000/mcp`
   - Inspector discovers OAuth metadata and initiates the flow
   - After auth, tool calls execute queries under the user's identity
4. **Backward-compat test**: Start without Keycloak env vars → server works as before
