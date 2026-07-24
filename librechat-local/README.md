# Local LibreChat → remote radiant-mcp (Keycloak JWT pass-through)

A self-contained local test harness for driving the **deployed** radiant-mcp
server from a LibreChat UI running on your machine.

- **LibreChat + MongoDB** run locally (this `docker compose` stack).
- **Keycloak** and **radiant-mcp** are **remote** (already deployed).
- The **LLM** is served by **Ollama on your host**.

LibreChat logs the user in against remote Keycloak, then forwards *that same*
Keycloak access token to radiant-mcp as `Authorization: Bearer <jwt>`. radiant-mcp
accepts it via its direct-JWT verifier (no MCP OAuth discovery), and runs each
StarRocks query under the token's `sub` identity.

## How the token flows

```
Browser ──login──▶ remote Keycloak
   │                    │ issues access token (JWT) to LibreChat client
   ▼                    ▼
LibreChat (local) ──Authorization: Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}──▶ remote radiant-mcp /mcp
   │ (OPENID_REUSE_TOKENS refreshes the token so it stays valid)              │ MultiAuth direct verifier
   │                                                                          ▼
Ollama (host) ◀──model calls──┘                                    JWTDBClient → StarRocks (as `sub`)
```

## Prerequisites

### 1. Ollama on the host
```bash
ollama pull llama3.1                 # a tool-calling capable model
OLLAMA_HOST=0.0.0.0:11434 ollama serve
curl http://localhost:11434/v1/models   # sanity check
```
> Default Ollama binds `127.0.0.1`, which the LibreChat container can't reach.
> `OLLAMA_HOST=0.0.0.0:11434` is required.

### 2. Remote Keycloak (one-time)
On the LibreChat client (confidential, authorization-code flow), in the **same
realm** radiant-mcp/StarRocks validate against:
- Add redirect URI: `http://localhost:3080/oauth/openid/callback`
- Ensure `offline_access` is an allowed/optional scope (usually a realm default).
- If radiant-mcp runs with `KEYCLOAK_AUDIENCE` set (or StarRocks requires a
  specific `aud`), add an **audience mapper** so tokens carry that audience.
  Otherwise no mapper needed.

### 3. Remote StarRocks (the real gate)
Each Keycloak user's `sub` (UUID) must already exist as a StarRocks user
`IDENTIFIED WITH authentication_jwt` (`principal_field: sub`) — the same
requirement as the existing portal path. Test with an already-provisioned user.

## Run

```bash
cp .env.example .env
# edit .env: OPENID_ISSUER / OPENID_CLIENT_ID / OPENID_CLIENT_SECRET / OPENID_SESSION_SECRET
# edit librechat.yaml: replace <remote-mcp-host> in the `radiant` mcpServers url
docker compose up -d
open http://localhost:3080
```

## Verify (end-to-end)

1. Open http://localhost:3080 and log in via the Keycloak button.
2. Create an **Agent** using the **Ollama** endpoint/model, and enable the
   **radiant** MCP server's tools on it.
3. Ask the agent to run a query (e.g. "list the tables" → `db_summary`, or a
   `read_query`). Confirm StarRocks data comes back.
4. Check the **remote** radiant-mcp logs: the token is accepted by the direct
   verifier (no DCR/discovery), and `JWTDBClient` runs under the user identity
   ("queries will run under user identity").
5. **Negative check:** a Keycloak user with no matching StarRocks user should
   fail at the query step — proving the per-user pass-through is live and it is
   not falling back to the static `root` credential.

## Gotchas

- **Apple Silicon:** MongoDB is pinned to `mongo:4.4` in `docker-compose.yml`;
  Mongo 5+ needs AVX and crashes on M-series chips.
- **Ollama + MCP tools:** tool calls only work with a model that supports
  function calling (llama3.1, qwen2.5, mistral-nemo). Small/older models may
  silently ignore MCP tools.
- **Placeholder name:** `{{LIBRECHAT_OPENID_ACCESS_TOKEN}}` is used here; some
  LibreChat versions expose it as `{{LIBRECHAT_OPENID_TOKEN}}`. If the header
  arrives empty, check your version's placeholder name and confirm
  `OPENID_REUSE_TOKENS=true`.
- **Token expiry:** `offline_access` + `OPENID_REUSE_TOKENS=true` keep the
  forwarded access token fresh across the session. Without them, MCP calls
  start returning 401 once the short-lived access token expires.

## Cleanup

```bash
docker compose down            # keeps chat history in ./data-node
docker compose down -v         # or remove ./data-node to wipe everything
```
