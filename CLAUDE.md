# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A containerized wrapper around the upstream [`mcp-server-starrocks`](https://github.com/StarRocks/mcp-server-starrocks) PyPI package. The wrapper adds three things the upstream package lacks:

1. `/health` + `/ready` HTTP endpoints for container orchestration.
2. Optional OAuth 2.1 (Keycloak/OIDC) authentication in front of the MCP endpoint.
3. Per-request JWT pass-through so each MCP tool call executes StarRocks queries **under the authenticated user's identity** instead of a shared static credential.

The MCP tools themselves (`read_query`, `write_query`, `table_overview`, etc.) come entirely from the upstream package — this repo does **not** define them. We only intercept how the DB connection is created.

## Gotchas (read first)

- **MCP tools are not defined in this repo.** `read_query`, `write_query`, etc. come from the upstream `mcp-server-starrocks` package. Don't search here for them.
- **Injection is via monkey-patch.** `__main__.py` overwrites `sr_server.db_client` (a module-level global the upstream tools read). Replacing that global is the only seam — there's no dependency-injection hook.
- **OAuth is gated on one env var.** Unset `KEYCLOAK_OIDC_CONFIG_URL` = plain upstream behavior, no auth code runs. Preserve this backward-compat path.
- **Two client auth paths, one `MultiAuth`.** External self-discovering clients (Claude Desktop) use the full OIDCProxy OAuth/DCR flow; the portal presents an existing Keycloak token directly. fastmcp 3's OIDCProxy alone rejects the latter (it only accepts tokens it minted), so `__main__.py` wraps it in `MultiAuth(server=oidc, verifiers=[direct_verifier])`. Don't drop the fallback verifier — the portal path breaks silently (401) if you do.
- **fastmcp 3.x OIDCProxy contacts Keycloak at startup.** Construction eagerly fetches the OIDC discovery doc, so the server hard-fails to boot if Keycloak isn't reachable. In compose, `radiant-mcp` therefore depends on `keycloak: service_healthy` (see the healthcheck note below).
- **JWTDBClient depends on upstream internals** (`_original._execute`, `db_client.ResultSet`, `remove_ansi_codes`). Floating deps (no lockfile) mean an upstream bump can silently break these.
- **StarRocks re-validates the JWT itself.** A working token isn't enough — a StarRocks user whose name is the token's `sub` (UUID) must exist as `IDENTIFIED WITH authentication_jwt` (with `principal_field: sub`), and StarRocks needs SSL enabled for JWT auth.
- **No unit tests / linter.** Only the shell integration suite exists; CI builds the image but does not run it.

## Commands

```bash
# Build the image
docker build -t radiant-mcp:latest .

# Bring up the full stack (keycloak + starrocks + radiant-mcp)
docker compose up --build

# Run the integration test suite (6 tests, requires Docker)
docker compose --profile test run --rm test-runner

# Run the integration test against an already-running stack, from the host
KEYCLOAK_URL=http://localhost:8080 MCP_URL=http://localhost:8000 \
  ./tests/integration/test-oauth-flow.sh

# Regenerate the self-signed StarRocks FE keystore (only if tls/starrocks.jks is missing)
./tests/integration/gen-starrocks-keystore.sh
```

There is no unit-test suite, linter, or `pyproject.toml` — the only tests are the shell-based integration tests. To run a single assertion, edit/comment blocks in `tests/integration/test-oauth-flow.sh` (tests are numbered sequential `curl` blocks, not independently selectable).

## Architecture

### Request flow (OAuth enabled)

```
MCP client → OIDCProxy (validates JWT via Keycloak JWKS) → MCP tool
          → JWTDBClient.execute() → per-request MySQL conn to StarRocks (JWT auth)
StarRocks re-validates the same JWT against Keycloak's JWKS and runs the query as that user.
```

Keycloak is the IdP for **both** hops: it issues the token to the client, and StarRocks independently verifies that same token. The `radiant-mcp` service holds a static `root` credential too, but that is used **only** for health checks and the no-token fallback path.

### Module layout (`radiant_mcp/`)

- `__main__.py` — entrypoint. Parses args, optionally configures `OIDCProxy`, and (critically) **monkey-patches** the upstream module: `sr_server.db_client = JWTDBClient(...)` and rebuilds `sr_server.db_summary_manager`. This is how our client gets injected — the upstream tools read `db_client` as a module-level global, so replacing that global is the only injection seam. Then starts uvicorn.
- `app.py` — Starlette factory. Mounts the upstream MCP ASGI app at `/mcp`, adds `/health`, `/ready`, and the `.well-known` OAuth routes; wide-open CORS. Uses the MCP app's own `lifespan`.
- `jwt_db_client.py` — `JWTDBClient`, a drop-in wrapper for the upstream `DBClient`. See below.
- `oauth.py` — hand-rolled `.well-known` metadata endpoints (workaround, see below).
- `health.py` — unauthenticated health/ready handlers.
- `server.py` (repo root) — thin shim (`asyncio.run(main())`) kept as the Docker `CMD` and for backward compat.

### JWTDBClient — the core mechanism

`jwt_db_client.py` wraps the upstream client and overrides `execute()` and `collect_perf_analysis_input()`. On each call it:

1. Pulls the raw JWT from the MCP auth context via `fastmcp.server.dependencies.get_access_token()`.
2. If **no token** (startup, health checks) OR Arrow Flight SQL is enabled → delegates to the original pooled client unchanged. This is the backward-compat path.
3. If a token exists → writes it to a `0o600` temp file, opens a **one-shot, non-pooled** `mysql.connector` connection using `auth_plugin='authentication_openid_connect_client'` + `openid_token_file`, runs the statement via the original client's private `_execute(conn, ...)`, then closes the connection and deletes the temp file in `finally`.

Key constraints when editing this file:
- `__getattr__` forwards everything not explicitly overridden to `self._original` — attributes read directly by upstream tools (`default_database`, `enable_arrow_flight_sql`, etc.) are copied in `__init__`.
- It reuses upstream internals: `self._original._execute(conn, ...)` and `mcp_server_starrocks.db_client.ResultSet` / `remove_ansi_codes`. These are imported lazily inside methods. Upstream version bumps can break these — pinned indirectly by the Dockerfile.
- The username sent to StarRocks is decoded from the JWT's `sub` claim **without signature verification** — verification already happened in OIDCProxy. A StarRocks user named that `sub` (UUID) must exist and be `IDENTIFIED WITH authentication_jwt` (`principal_field: sub`).

### OAuth is optional and gated on one env var

`OAUTH_ENABLED = bool(os.getenv('KEYCLOAK_OIDC_CONFIG_URL'))`. With it unset, none of the auth code runs and the server behaves exactly like plain upstream `mcp-server-starrocks`. Preserve this backward-compat guarantee when changing `__main__.py`.

When enabled, `mcp.auth` is a `MultiAuth` composing two sources:
- `server=OIDCProxy(...)` — owns the routes and OAuth metadata (self-discovery + DCR + PKCE) for external clients, and accepts the reference tokens it mints through that flow.
- `verifiers=[oidc.get_token_verifier(...)]` — a JWKS `JWTVerifier` that validates raw Keycloak tokens presented directly (portal clients).

`MultiAuth` tries the proxy first, then the verifier, returning the first success. In **both** paths the resulting `AccessToken.token` is a real Keycloak JWT, which is what `JWTDBClient` forwards to StarRocks — so the per-user pass-through works regardless of how the client authenticated. This hybrid exists because fastmcp 3.x changed `OIDCProxy` from a transparent JWT verifier (2.x) into a token-swap proxy that rejects any token it didn't issue.

### Why oauth.py exists (workaround)

FastMCP serves OAuth metadata under `/mcp/.well-known/...` and Pydantic's URL serialization appends trailing slashes that some MCP clients reject on strict comparison. `oauth.py` + the extra routes in `app.py` re-serve RFC 9728 / RFC 8414 metadata at the canonical root paths with clean URLs (`auth_server_metadata` proxies the inner FastMCP endpoint). If clients start failing OAuth discovery, this is the first place to look.

## Docker notes

- Multi-stage build: `uv`-built venv in the builder stage is copied into a slim runtime; deps are `mcp-server-starrocks` + `fastmcp[auth]`. No local `requirements`/lockfile — versions float at build time.
- `COPY src/ dest/` with multiple sources flattens files. Copy files and directories on **separate** `COPY` lines (see `Dockerfile`).
- The compose `starrocks` service appends `fe-ssl.conf` to `fe.conf` before boot and mounts a JKS keystore to enable TLS — required because StarRocks JWT auth needs SSL. `starrocks-init` then seeds `test_db` and creates the `testuser` JWT user.
- `keycloak` has a healthcheck that probes `:9000/health/ready` over a raw bash `/dev/tcp` socket (the keycloak:26 image has bash but no curl/wget) and matches the HTTP `200` status line. `radiant-mcp` waits on `keycloak: service_healthy` because OIDCProxy contacts Keycloak at startup (above).
- Run the tests with `docker compose --profile test run --rm test-runner`, **not** `up --abort-on-container-exit` — the one-shot `starrocks-init` exits 0 on success, which trips `--abort-on-container-exit` and tears the stack down before the test runs.

## Environment variables

Static-credential connection (always): `STARROCKS_HOST`, `STARROCKS_PORT` (9030), `STARROCKS_USER`, `STARROCKS_PASSWORD`, `STARROCKS_DB`, or a single `STARROCKS_URL`.

OAuth (all optional; presence of the first enables OAuth): `KEYCLOAK_OIDC_CONFIG_URL`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`, `KEYCLOAK_AUDIENCE`, `MCP_BASE_URL`, `OAUTH_REQUIRED_SCOPES` (comma-separated, default `openid`).

## Other

- `docs/oauth-jwt-auth-plan.md` is the original design doc for the OAuth/JWT work; useful background but predates the `radiant_mcp/` package refactor (it references a monolithic `server.py`).
- CI (`.github/workflows/build_and_push.yml`) only builds and pushes the image to `ghcr.io/radiant-network/radiant-mcp` on push to `main` / `v*` tags — it does **not** run the integration tests.
