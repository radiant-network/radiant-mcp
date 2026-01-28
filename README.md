# StarRocks MCP Server Docker Image

A containerized [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for [StarRocks](https://www.starrocks.io/), enabling AI assistants to interact with StarRocks databases.

## Overview

This Docker image packages the [mcp-server-starrocks](https://github.com/StarRocks/mcp-server-starrocks) Python package in a multi-stage build for production deployment. It runs in **streamable-http** mode, exposing an HTTP endpoint for MCP clients.

## Quick Start

```bash
docker run -d \
  -p 8000:8000 \
  -e STARROCKS_HOST=your-starrocks-host \
  -e STARROCKS_PORT=9030 \
  -e STARROCKS_USER=root \
  -e STARROCKS_PASSWORD=your-password \
  mcp-server-starrocks:latest
```

The MCP endpoint will be available at `http://localhost:8000/mcp`

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
  mcp-server-starrocks:latest
```

## Building the Image

```bash
docker build -t mcp-server-starrocks:latest .
```

## Running Modes

### HTTP Mode (Default)

The container runs in `streamable-http` mode by default on port 8000:

```bash
docker run -d -p 8000:8000 \
  -e STARROCKS_HOST=localhost \
  -e STARROCKS_USER=root \
  -e STARROCKS_PASSWORD=secret \
  mcp-server-starrocks:latest
```

### Custom Port

```bash
docker run -d -p 3000:3000 \
  -e STARROCKS_HOST=localhost \
  -e STARROCKS_USER=root \
  -e STARROCKS_PASSWORD=secret \
  mcp-server-starrocks:latest \
  mcp-server-starrocks --mode streamable-http --port 3000
```

### Stdio Mode (for local MCP hosts)

```bash
docker run -i \
  -e STARROCKS_HOST=localhost \
  -e STARROCKS_USER=root \
  -e STARROCKS_PASSWORD=secret \
  mcp-server-starrocks:latest \
  mcp-server-starrocks --mode stdio
```

### Task Definition Considerations

- **Port mapping**: Container port 8000
- **Health check**: `curl -f http://localhost:8000/mcp || exit 1`
- **Secrets**: Store `STARROCKS_PASSWORD` in AWS Secrets Manager
- **Memory**: Recommend at least 1GB due to pandas/pyarrow dependencies

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

The server exposes tools for:
- Executing SQL queries against StarRocks
- Listing databases and tables
- Describing table schemas
- Getting database overview and statistics

## License

See the [upstream repository](https://github.com/StarRocks/mcp-server-starrocks) for license information.
