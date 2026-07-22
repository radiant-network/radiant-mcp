# StarRocks MCP Server Docker Image

A containerized [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for [StarRocks](https://www.starrocks.io/), enabling AI assistants to interact with StarRocks databases.

## Overview

This Docker image packages the [mcp-server-starrocks](https://github.com/StarRocks/mcp-server-starrocks) Python package with a custom wrapper that adds:

- **Health check endpoints** (`/health`, `/ready`) for container orchestration.
- **Optional OAuth 2.1 authentication** via Keycloak (OIDC), in front of the MCP endpoint.
- **Per-request JWT pass-through** — each MCP tool call runs its StarRocks queries under the authenticated user's identity instead of a shared static credential.

It runs in **streamable-http** mode, exposing an HTTP endpoint for MCP clients. With no OAuth environment variables set, it behaves exactly like the upstream package with static credentials.

## Quick Start

```bash
docker run -d \
  -p 8000:8000 \
  -e STARROCKS_HOST=your-starrocks-host \
  -e STARROCKS_PORT=9030 \
  -e STARROCKS_USER=root \
  -e STARROCKS_PASSWORD=your-password \
  ghcr.io/radiant-network/radiant-mcp:latest
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/mcp` | POST | MCP protocol endpoint (JSON-RPC over HTTP) |
| `/health` | GET | Health check - returns database connection status |
| `/ready` | GET | Readiness check - returns server status |

### Health Check Response

```json
{"status": "healthy", "database": "connected"}
```

Returns HTTP 200 when healthy, HTTP 503 when database is disconnected.

### Readiness Check Response

```json
{"status": "ready"}
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STARROCKS_HOST` | Yes | - | StarRocks FE (Frontend) hostname or IP |
| `STARROCKS_PORT` | No | `9030` | StarRocks FE query port |
| `STARROCKS_USER` | Yes | - | Database username |
| `STARROCKS_PASSWORD` | Yes | - | Database password |
| `STARROCKS_DB` | No | - | Default database to use |
| `STARROCKS_OVERVIEW_LIMIT` | No | - | Limit for overview queries (memory management) |

### Alternative: Connection URL

Instead of individual variables, you can use a single connection URL:

```bash
docker run -d \
  -p 8000:8000 \
  -e STARROCKS_URL="user:password@host:9030/database" \
  ghcr.io/radiant-network/radiant-mcp:latest
```

## Authentication (OAuth 2.1 + Keycloak)

Authentication is **optional and opt-in**: it activates only when `KEYCLOAK_OIDC_CONFIG_URL` is set. When enabled, the server supports two kinds of clients simultaneously:

- **Self-discovering MCP clients** (e.g. Claude Desktop) — complete the full OAuth 2.1 + PKCE flow, with dynamic client registration, discovered automatically from the server's `.well-known` metadata.
- **Clients that already hold a Keycloak token** (e.g. a web portal) — present the existing token directly as a `Bearer` header.

In both cases the user's Keycloak JWT is forwarded to StarRocks, which validates it against Keycloak's JWKS (the `authentication_jwt` plugin, v3.5.0+) and executes queries under that user's identity. The static `STARROCKS_*` credentials are used only for health checks and as a fallback when no token is present. The `STARROCKS_USER` referenced by a JWT must exist in StarRocks as a user `IDENTIFIED WITH authentication_jwt`, and StarRocks must have SSL enabled (JWT auth requires it).

### OAuth Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KEYCLOAK_OIDC_CONFIG_URL` | Yes (to enable auth) | - | Keycloak OIDC discovery URL, e.g. `https://kc.example.com/realms/radiant/.well-known/openid-configuration` |
| `KEYCLOAK_CLIENT_ID` | Yes | - | OAuth client ID registered in Keycloak |
| `KEYCLOAK_CLIENT_SECRET` | Yes | - | OAuth client secret |
| `KEYCLOAK_AUDIENCE` | No | - | Expected JWT `aud` claim |
| `MCP_BASE_URL` | No | `http://localhost:8000/mcp` | Public URL of the MCP endpoint (used in OAuth metadata) |
| `OAUTH_REQUIRED_SCOPES` | No | `openid` | Comma-separated scopes required on tokens |

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
      STARROCKS_USER: root
      STARROCKS_PASSWORD: ${STARROCKS_PASSWORD}
      STARROCKS_DB: my_database
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### Deployment Considerations

- **Memory**: Recommend at least 1GB due to pandas/pyarrow dependencies
- **Secrets**: Store `STARROCKS_PASSWORD` securely (e.g., AWS Secrets Manager, Kubernetes Secrets)

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
