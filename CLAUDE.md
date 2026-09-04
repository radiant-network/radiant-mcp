# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A containerized wrapper around the upstream [`mcp-server-starrocks`](https://github.com/StarRocks/mcp-server-starrocks) PyPI package. The wrapper adds three things the upstream package lacks:

1. `/health` + `/ready` HTTP endpoints for container orchestration (liveness only — no StarRocks probe).
2. Mandatory OAuth 2.1 (Keycloak/OIDC) authentication in front of the MCP endpoint.
3. Per-request JWT pass-through so each MCP tool call executes StarRocks queries **under the authenticated user's identity**. This component holds **no** StarRocks credentials — there is no static-credential fallback.

The StarRocks MCP tools themselves (`read_query`, `write_query`, `table_overview`, etc.) come entirely from the upstream package — this repo does **not** define them. We only intercept how the DB connection is created.

The only tools defined here are the optional **Radiant API tools** (`radiant_mcp/radiant_api_tools.py`: `search_cases`, `get_case_context`, `list_tenants`), registered on the upstream FastMCP instance only when `RADIANT_API_URL` is set. They call the Radiant portal API through the generated `radiant_python` client, forwarding the caller's JWT as a Bearer token.

## Gotchas (read first)

- **StarRocks MCP tools are not defined in this repo.** `read_query`, `write_query`, etc. come from the upstream `mcp-server-starrocks` package. Don't search here for them.
- **Injection is via monkey-patch.** `__main__.py` overwrites `sr_server.db_client` (a module-level global the upstream tools read). Replacing that global is the only seam — there's no dependency-injection hook.
- **OAuth is mandatory.** `KEYCLOAK_REALM_URL` is required — `__main__.py` raises `SystemExit` at startup if it is unset. There is no non-OAuth / plain-upstream path anymore (removed 2026-07-24), and the container is never given StarRocks credentials.
- **One provider, both client paths.** `__main__.py` uses fastmcp's native `KeycloakAuthProvider` — a pure JWT resource server (holds no client secret, mints no tokens). It just validates incoming Bearer JWTs against Keycloak's JWKS. Self-discovering clients (Claude Desktop) get tokens via Keycloak's own DCR + PKCE; portal clients (LibreChat) present a raw Keycloak token directly. Both hit the same verifier, so there is **no** `MultiAuth`/fallback-verifier and no server-side token swap. (This replaced an earlier `OIDCProxy` + `MultiAuth` hand-rolled setup — see git history if you need the rationale.)
- **`KeycloakAuthProvider` needs Keycloak ≥ 26.6.0** for the DCR path (keycloak PR #45309). The portal/direct-token path works on any version. It fetches Keycloak's JWKS at startup, so the server hard-fails to boot if Keycloak isn't reachable — hence `radiant-mcp` depends on `keycloak: service_healthy` (see the healthcheck note below).
- **Audience is optional and intentionally unset.** StarRocks' `authentication_jwt` only checks `aud` when the user is created `WITH ... required_audience`, which we don't do. Setting `KEYCLOAK_AUDIENCE` would make the provider *require* that `aud` on every token — which DCR-registered clients (their own client_id, no audience mapper) don't carry — so leave it unset unless you also add a matching audience mapper on a default client scope.
- **JWTDBClient depends on upstream internals** (`_original._execute`, `db_client.ResultSet`, `remove_ansi_codes`). Floating deps (no lockfile) mean an upstream bump can silently break these.
- **StarRocks re-validates the JWT itself.** A working token isn't enough — a StarRocks user whose name is the token's `sub` (UUID) must exist as `IDENTIFIED WITH authentication_jwt` (with `principal_field: sub`), and StarRocks needs SSL enabled for JWT auth.
- **Radiant API tools are opt-in.** `register_tools()` is a no-op unless `RADIANT_API_URL` is set, so the compose stack (which has no Radiant API) still boots. The `radiant_python` client is a generated OpenAPI package installed from git and pinned to a commit SHA (`RADIANT_PORTAL_SHA` build arg in the Dockerfile) — bumping it can change model fields/method names. It is a sync urllib3 client, so tool bodies run in `anyio.to_thread.run_sync`; the JWT is read from the contextvar *before* hopping threads.
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

### Request flow

```
MCP client → KeycloakAuthProvider (validates JWT via Keycloak JWKS) → MCP tool
          → JWTDBClient.execute() → per-request MySQL conn to StarRocks (JWT auth)
StarRocks re-validates the same JWT against Keycloak's JWKS and runs the query as that user.
```

Keycloak is the IdP for **both** hops: it issues the token to the client, and StarRocks independently verifies that same token. The `radiant-mcp` service holds **no** StarRocks credential — a tool call without a JWT is rejected, not run as a shared user. `/health` is liveness-only and never touches StarRocks.

### Module layout (`radiant_mcp/`)

- `__main__.py` — entrypoint. Parses args, **requires** `KEYCLOAK_REALM_URL` (raises `SystemExit` if unset), configures `KeycloakAuthProvider` (set as `mcp.auth`), and (critically) **monkey-patches** the upstream module: `sr_server.db_client = JWTDBClient(...)` and rebuilds `sr_server.db_summary_manager`. This is how our client gets injected — the upstream tools read `db_client` as a module-level global, so replacing that global is the only injection seam. Then starts uvicorn.
- `app.py` — Starlette factory. Mounts the upstream MCP ASGI app at `/mcp`, adds `/health`, `/ready`, and the `.well-known` OAuth routes; wide-open CORS. Uses the MCP app's own `lifespan`.
- `jwt_db_client.py` — `JWTDBClient`, a drop-in wrapper for the upstream `DBClient`. See below. Also exposes `execute_with_token(token, sql, ...)` for callers that already hold the JWT (used by `radiant_api_tools`).
- `oauth.py` — hand-rolled RFC 9728 protected-resource metadata endpoint (workaround, see below). Advertises the Keycloak realm as the authorization server.
- `health.py` — unauthenticated liveness/readiness handlers (no StarRocks probe).
- `radiant_api_tools.py` — optional tools backed by the Radiant portal API. `get_case_context(case_id, tenant=None)` composes `CasesApi.case_entity` + `case_tasks_with_occurrences` (one call per non-deprecated `data_type`, chosen from `case_type`) into a single payload ending with `occurrence_keys` = `(case_id, seq_id, task_id, data_type)` tuples for follow-up variant queries. Each key also carries `part` / `variant_part` (= `part // 10`, mirroring `compute_part` in the pipeline's `import_part.py`): the StarRocks occurrence tables, `exomiser` and `radiant.snv__consequence_filter_partitioned` are `PARTITION BY (part)`, `snv__variant_partitioned` by `variant_part`. **Temporary:** `part` is resolved by `_lookup_parts()` with a StarRocks query on `<RADIANT_SHARED_DB>.staging_sequencing_experiment` through `sr_server.db_client.execute_with_token(token, sql)` (the caller's JWT, passed explicitly because the body runs on a worker thread) — to be removed once the Radiant API returns `part` per task. A failed lookup adds a `warnings` entry, it never fails the tool. Tenant discovery uses `AuthApi.get_me()`: one membership → auto-selected, several → error listing them. `list_tenants` exposes the same list. `search_cases(filters, query, ...)` wraps `CasesApi.search_cases` (criteria are AND-ed, default operator `in`; the filterable/sortable aliases are hard-coded in `CASE_SEARCH_FILTER_FIELDS` / `CASE_SEARCH_SORT_FIELDS`, mirroring `CanBeFiltered`/`CanBeSorted` in the backend's `CasesFields`) and, for `query`, resolves the prefix via `autocomplete_cases` then runs one search per matched id type and merges — the API has no OR across fields. Autocomplete types `case_id` / `patient_id` / `sequencing_experiment_id` map to the same-named filters; any other type is a `submitter_patient_id_type` (e.g. `mrn`) and goes through the `mrn` filter (alias of `patient.submitter_patient_id`). Sample ids (`submitter_sample_id`, `aliquot`) are not searchable through the API.
- `server.py` (repo root) — thin shim (`asyncio.run(main())`) kept as the Docker `CMD`.

### JWTDBClient — the core mechanism

`jwt_db_client.py` wraps the upstream client and overrides `execute()` and `collect_perf_analysis_input()`. On each call it:

1. Pulls the raw JWT from the MCP auth context via `fastmcp.server.dependencies.get_access_token()`.
2. If **no token** → returns a failed `ResultSet` / error dict (`"Authentication required: no JWT in request context"`). There is **no** static-credential fallback — the wrapper never delegates back to the pooled client.
3. If a token exists → writes it to a `0o600` temp file, opens a **one-shot, non-pooled** `mysql.connector` connection using `auth_plugin='authentication_openid_connect_client'` + `openid_token_file`, runs the statement via the original client's private `_execute(conn, ...)`, then closes the connection and deletes the temp file in `finally`. Host/port/db/timeouts still come from `self._original.connection_params` (`STARROCKS_HOST`/`PORT`/`DB` — connection targets, not credentials).

Key constraints when editing this file:
- `__getattr__` forwards everything not explicitly overridden to `self._original` — attributes read directly by upstream tools (`default_database`, `enable_arrow_flight_sql`, etc.) are copied in `__init__`.
- It reuses upstream internals: `self._original._execute(conn, ...)` and `mcp_server_starrocks.db_client.ResultSet` / `remove_ansi_codes`. These are imported lazily inside methods. Upstream version bumps can break these — pinned indirectly by the Dockerfile.
- The username sent to StarRocks is decoded from the JWT's `sub` claim **without signature verification** — verification already happened in `KeycloakAuthProvider`. A StarRocks user named that `sub` (UUID) must exist and be `IDENTIFIED WITH authentication_jwt` (`principal_field: sub`).

### OAuth is mandatory

`__main__.py` requires `KEYCLOAK_REALM_URL` and raises `SystemExit("KEYCLOAK_REALM_URL is required")` if it is unset — there is no plain-upstream / static-credential path. `mcp.auth` is always a single `KeycloakAuthProvider(realm_url=..., base_url=..., audience=None, required_scopes=[...])`. It is a `RemoteAuthProvider` subclass that builds a JWKS `JWTVerifier` internally (issuer = `realm_url`, `jwks_uri = {realm_url}/protocol/openid-connect/certs`, RS256). It validates any valid Keycloak JWT regardless of how the client obtained it:
- **Self-discovering clients** (Claude Desktop) run OIDC discovery, register via Keycloak's DCR, and do the auth-code + PKCE browser flow — all against Keycloak directly. The server only advertises Keycloak as the authorization server.
- **Portal clients** (LibreChat) already hold a Keycloak token and present it as a Bearer.

In **both** paths the resulting `AccessToken.token` is a real Keycloak JWT, which `JWTDBClient` forwards to StarRocks — so the per-user pass-through works regardless of how the client authenticated. No `MultiAuth`, no token swap, no server-held client secret.

### Why oauth.py exists (workaround)

FastMCP serves protected-resource metadata under `/mcp/.well-known/...` and Pydantic's URL serialization appends trailing slashes that some MCP clients reject on strict comparison. `oauth.py` + the extra routes in `app.py` re-serve RFC 9728 protected-resource metadata at the canonical root paths with clean URLs, advertising the Keycloak realm as the authorization server. Authorization-server metadata (RFC 8414) is **not** served by us — the native provider points clients straight at Keycloak for that. If clients start failing OAuth discovery, this is the first place to look.

## Docker notes

- Multi-stage build: `uv`-built venv in the builder stage is copied into a slim runtime; deps are `mcp-server-starrocks` + `fastmcp[auth]`. No local `requirements`/lockfile — versions float at build time.
- `COPY src/ dest/` with multiple sources flattens files. Copy files and directories on **separate** `COPY` lines (see `Dockerfile`).
- The compose `starrocks` service appends `fe-ssl.conf` to `fe.conf` before boot and mounts a JKS keystore to enable TLS — required because StarRocks JWT auth needs SSL. `starrocks-init` then seeds `test_db` and creates the `testuser` JWT user.
- `keycloak` has a healthcheck that probes `:9000/health/ready` over a raw bash `/dev/tcp` socket (the keycloak:26 image has bash but no curl/wget) and matches the HTTP `200` status line. `radiant-mcp` waits on `keycloak: service_healthy` because OIDCProxy contacts Keycloak at startup (above).
- Run the tests with `docker compose --profile test run --rm test-runner`, **not** `up --abort-on-container-exit` — the one-shot `starrocks-init` exits 0 on success, which trips `--abort-on-container-exit` and tears the stack down before the test runs.

## Environment variables

Connection **target** (no credentials): `STARROCKS_HOST`, `STARROCKS_PORT` (9030), `STARROCKS_DB`. There is intentionally **no** `STARROCKS_USER` / `STARROCKS_PASSWORD` / `STARROCKS_URL` — access is per-user via JWT. (Upstream `DBClient` still defaults user→`root`, password→`""`, but with no fallback and no health checker, the pooled connection is never opened, so those defaults are never used.)

Radiant API (optional): `RADIANT_API_URL` — base URL of the Radiant portal API; enables `search_cases` / `get_case_context` / `list_tenants`. No API credential: the caller's JWT is forwarded. `RADIANT_SHARED_DB` (default `radiant`) — StarRocks database holding the shared tables, used by `get_case_context` to read `staging_sequencing_experiment` for the per-task `part`.

OAuth (required): `KEYCLOAK_REALM_URL` (bare realm URL, e.g. `http://keycloak:8080/realms/radiant`) — the server refuses to boot without it; `MCP_BASE_URL` (server **root**, not `/mcp`), `OAUTH_REQUIRED_SCOPES` (comma-separated, default `openid`), `KEYCLOAK_AUDIENCE` (optional; leave unset — see the audience gotcha above). The server needs no client id/secret.

## Other

- `docs/oauth-jwt-auth-plan.md` is the original design doc for the OAuth/JWT work; useful background but predates the `radiant_mcp/` package refactor (it references a monolithic `server.py`).
- CI (`.github/workflows/build_and_push.yml`) only builds and pushes the image to `ghcr.io/radiant-network/radiant-mcp` on push to `main` / `v*` tags — it does **not** run the integration tests.
