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

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# The app runs from /app/.venv; the base image's setuptools/wheel are unused and
# carry vendored CVEs (wheel, jaraco.context), so drop them along with pip.
RUN pip uninstall -y setuptools wheel pip
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY server.py .
COPY radiant_mcp/ radiant_mcp/

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

USER 10001

CMD ["python", "server.py"]
