# Build stage - install dependencies
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

WORKDIR /app
RUN uv venv /app/.venv
# fastmcp 3.x bundles OAuth/OIDC support (authlib) by default — the old
# [auth] extra was removed. mcp-server-starrocks pulls in mysql-connector-python.
RUN uv pip install --python /app/.venv/bin/python mcp-server-starrocks fastmcp
# Radiant portal API client (generated, not on PyPI) — pinned to a commit SHA
# because the generated code floats with the upstream OpenAPI spec.
ARG RADIANT_PORTAL_SHA=3fe07d86bbfe1539c6e2f9db21cdd0969c1eaf48
RUN uv pip install --python /app/.venv/bin/python \
    "radiant-python-cli @ git+https://github.com/radiant-network/radiant-portal.git@${RADIANT_PORTAL_SHA}#subdirectory=cli/python"

# Runtime stage - minimal image
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY server.py .
COPY radiant_mcp/ radiant_mcp/

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["python", "server.py"]
