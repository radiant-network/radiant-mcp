# Build stage - install dependencies
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

WORKDIR /app
# All dependencies (including version-pin rationale and the radiant-portal git
# SHA) live in pyproject.toml; uv.lock makes every image build reproducible —
# versions only change when someone runs `uv lock` and commits the diff.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

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
