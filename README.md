# StarRocks MCP Server Docker Image

A containerized [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for [StarRocks](https://www.starrocks.io/), enabling AI assistants to interact with StarRocks databases.

## Overview

This Docker image packages the [mcp-server-starrocks](https://github.com/StarRocks/mcp-server-starrocks) Python package with a custom wrapper that adds health check endpoints. It runs in **streamable-http** mode, exposing an HTTP endpoint for MCP clients.

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

## License

See the [upstream repository](https://github.com/StarRocks/mcp-server-starrocks) for license information.
