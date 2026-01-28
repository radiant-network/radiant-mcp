# Build stage - install dependencies
FROM python:3.11-slim AS builder

RUN pip install uv

WORKDIR /app
RUN uv venv /app/.venv
RUN uv pip install --python /app/.venv/bin/python mcp-server-starrocks

# Runtime stage - minimal image
FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY server.py .

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["python", "server.py"]
