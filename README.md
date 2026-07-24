# StarRocks MCP Server Docker Image

A containerized [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for [StarRocks](https://www.starrocks.io/), enabling AI assistants to interact with StarRocks databases.

## Overview

This Docker image packages the [mcp-server-starrocks](https://github.com/StarRocks/mcp-server-starrocks) Python package with a custom wrapper that adds:

- **Health check endpoints** (`/health`, `/ready`) for container orchestration.
- **Mandatory OAuth 2.1 authentication** via Keycloak (OIDC), in front of the MCP endpoint.
- **Per-request JWT pass-through** — each MCP tool call runs its StarRocks queries under the authenticated user's identity. This component holds **no** StarRocks credentials.

It runs in **streamable-http** mode, exposing an HTTP endpoint for MCP clients. `KEYCLOAK_REALM_URL` is required — the server refuses to boot without it.

## Quick Start

```bash
docker run -d \
  -p 8000:8000 \
  -e STARROCKS_HOST=your-starrocks-host \
  -e STARROCKS_PORT=9030 \
  -e STARROCKS_DB=your-database \
  -e KEYCLOAK_REALM_URL=https://kc.example.com/realms/radiant \
  -e MCP_BASE_URL=https://mcp.example.com \
  ghcr.io/radiant-network/radiant-mcp:latest
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/mcp` | POST | MCP protocol endpoint (JSON-RPC over HTTP) |
| `/health` | GET | Liveness check — the process is up (does not touch StarRocks) |
| `/ready` | GET | Readiness check - returns server status |

### Health Check Response

```json
{"status": "healthy"}
```

Returns HTTP 200 while the process is running. It does **not** probe StarRocks —
this component holds no StarRocks credentials.

### Readiness Check Response

```json
{"status": "ready"}
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STARROCKS_HOST` | Yes | - | StarRocks FE (Frontend) hostname or IP (connection target only) |
| `STARROCKS_PORT` | No | `9030` | StarRocks FE query port |
| `STARROCKS_DB` | No | - | Default database to use |
| `STARROCKS_OVERVIEW_LIMIT` | No | - | Limit for overview queries (memory management) |

> No `STARROCKS_USER` / `STARROCKS_PASSWORD`: this component authenticates to
> StarRocks with each caller's JWT, never a static credential. `STARROCKS_HOST`
> and `STARROCKS_PORT` only tell it *where* to connect.

## Authentication (OAuth 2.1 + Keycloak)

Authentication is **mandatory**: the server requires `KEYCLOAK_REALM_URL` and refuses to boot without it. It supports two kinds of clients simultaneously:

- **Self-discovering MCP clients** (e.g. Claude Desktop) — complete the full OAuth 2.1 + PKCE flow, with dynamic client registration, discovered automatically from the server's `.well-known` metadata.
- **Clients that already hold a Keycloak token** (e.g. a web portal) — present the existing token directly as a `Bearer` header.

In both cases the user's Keycloak JWT is forwarded to StarRocks, which validates it against Keycloak's JWKS (the `authentication_jwt` plugin, v3.5.0+) and executes queries under that user's identity. There is no static-credential fallback: a request without a valid JWT is rejected. The user (the JWT `sub`) must exist in StarRocks as `IDENTIFIED WITH authentication_jwt` (`principal_field: sub`), and StarRocks must have SSL enabled (JWT auth requires it).

The server uses fastmcp's native `KeycloakAuthProvider` — a pure JWT resource server. It holds no client id/secret and mints no tokens; it only validates incoming Bearer JWTs against the realm's JWKS. Requires Keycloak ≥ 26.6.0 for the DCR path.

### OAuth Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KEYCLOAK_REALM_URL` | Yes | - | Bare realm URL, e.g. `https://kc.example.com/realms/radiant` |
| `MCP_BASE_URL` | No | `http://localhost:8000` | Server **root** URL (not `/mcp`); the provider derives the resource + metadata URLs from it |
| `OAUTH_REQUIRED_SCOPES` | No | `openid` | Comma-separated scopes required on tokens |
| `KEYCLOAK_AUDIENCE` | No | - | Expected JWT `aud` claim (leave unset unless you add a matching audience mapper) |

## Building the Image

```bash
docker build -t radiant-mcp:latest .
```

## Container Configuration

### Port Mapping

The server listens on port 8000 inside the container.

### Health Check

For container orchestration (Docker Compose, Kubernetes, ECS):

```bash
curl -f http://localhost:8000/health
```

### Example Docker Compose

```yaml
services:
  radiant-mcp:
    image: ghcr.io/radiant-network/radiant-mcp:latest
    ports:
      - "8000:8000"
    environment:
      STARROCKS_HOST: starrocks-fe
      STARROCKS_PORT: 9030
      STARROCKS_DB: my_database
      KEYCLOAK_REALM_URL: https://kc.example.com/realms/radiant
      MCP_BASE_URL: https://mcp.example.com
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### Deployment Considerations

- **Memory**: Recommend at least 1GB due to pandas/pyarrow dependencies
- **Credentials**: None to store — the component holds no StarRocks user/password. Access is governed entirely by the caller's Keycloak JWT and the matching StarRocks JWT user.

## MCP Client Configuration

Configure your MCP client to connect to the server:

```json
{
  "mcpServers": {
    "starrocks": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Available MCP Tools

| Tool | Description |
|------|-------------|
| `read_query` | Execute SELECT queries |
| `write_query` | Execute DDL/DML commands |
| `analyze_query` | Analyze query performance |
| `table_overview` | Get table schema and sample data |
| `db_summary` | Get database summary with table schemas |
| `query_and_plotly_chart` | Execute query and generate Plotly chart |

## Local Development

A `docker-compose.yml` brings up a full local stack — Keycloak (IdP), StarRocks (with TLS + a JWT-authenticated `testuser`), and `radiant-mcp` wired to both:

```bash
docker compose up --build
```

The MCP server is then at `http://localhost:8000/mcp` and Keycloak at `http://localhost:8080` (realm `radiant`).

### Integration Tests

An end-to-end test exercises the OAuth 2.1 + JWT → StarRocks flow (health, OAuth metadata, token issuance, authenticated `read_query`, and the unauthenticated → 401 case):

```bash
docker compose --profile test run --rm test-runner
```

> Use `run --rm test-runner`, not `up --abort-on-container-exit` — the one-shot `starrocks-init` container exits 0 on success, which would abort the whole stack before the test runs.

## License

See the [upstream repository](https://github.com/StarRocks/mcp-server-starrocks) for license information.
