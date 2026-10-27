# hub-graphql-mcp: MCP server

Part of the [hub-graphql-mcp](../README.md) project; the example agent that uses it is in
[`../agent`](../agent). Commands below run from this folder.

Read-only MCP server (FastAPI + FastMCP, Streamable HTTP at `/mcp`) that answers Cloud Foundry
inventory and service-health questions from the **Tanzu Hub GraphQL API** (`/hub/graphql`)
instead of the CF v3 API. One Hub service account covers every foundation attached to Hub, and
no CF or Vault credentials are needed.

| Tool | Hub source |
|---|---|
| `list_organizations` | `Tanzu.TAS.Organization` entities with their foundation; optional foundation filter |
| `list_spaces` | `Tanzu.TAS.Space` entities with org and foundation; org filter runs in Hub |
| `list_users` | `authQuery.queryAllRoleBindings`: Cloud Foundry roles (provider `TP4CF`) grouped by user, group or client |
| `list_service_instances` | `Tanzu.TAS.ServiceInstance` with org, space, foundation, offering, plan, last operation |
| `get_service_instance` | One instance by CF GUID, Hub `entity_id` (`vrn/...`) or exact name, with properties and neighbours |
| `list_services_status` | Normalized health for every instance, worst first, with a summary count |
| `get_mysql_service_instance_status` | Instance health + its BOSH deployment's VMs (`Tanzu.TAS.BoshVM`), plus optional live proxy topology |

REST mirrors the tools under `/api/*` (same service layer). `/health` is open; `/mcp` and
`/api/*` require the API key, as an `x-api-key` header or `Authorization: Bearer <key>` (what
the agent buildpack sends for a binding's `api_key`).

All query shapes have been validated against a live Hub (`schema_check.py`).

## Health model

Statuses are `healthy`, `in_progress`, `degraded`, `failed` and `unknown`.

- **Service instances** use Hub's `LastOperationState` property (`succeeded` is healthy,
  `in progress` is in progress, `failed` is failed). When `graphql/insights.graphql` is
  configured, firing Hub alerts make an instance degraded, or failed for critical/availability
  alerts.
- **MySQL nodes** use each BOSH VM's `State` (`started` is healthy) and agent `Status`. If some
  VMs are down the cluster is degraded; if all are down it is failed.
- The MySQL tool's overall status is the worst of the instance health, the BOSH node rollup
  and, when enabled, the live proxy topology.

## Read-only guarantees

- Every outgoing GraphQL document is parsed and rejected unless all its operations are `query`
  (`hub_client.assert_query_only`). Authentication is a separate OAuth token request (or, in
  `graphql` mode, the single auth mutation from its own code path).
- User input is only ever rendered as escaped GraphQL string literals (no injection).
- Property values whose names look like passwords, secrets, tokens or keys are redacted.
- TLS verification is always on (`CA_BUNDLE` for a corporate CA). The service account's
  read-only Hub role is the real boundary.

## Setup

1. **Create a service account.** In Hub: Administration > Access > Service Accounts > Create.
   - **Scope type: Global.** The tools answer across all foundations, and the scope cannot
     be changed later.
   - **Access Token TTL:** the default 30 minutes is fine; the server refreshes a minute early.
   - Copy the **Service Account ID** and the **Secret** (shown only once). The server does not
     use the Client Assertion; don't store it with the other credentials.
   - Then Edit > Role Bindings > Add: scope **Global**, role **Viewer**. This is enough for
     every tool, including `list_users`.

   Service accounts are OAuth clients, so the server exchanges the ID and secret at
   `https://<hub-host>/auth/oauth/token` (`client_credentials`). You can check them with:

   ```bash
   curl -s -X POST https://<hub-host>/auth/oauth/token \
     -H 'content-type: application/x-www-form-urlencoded' \
     -d grant_type=client_credentials -d client_id=<id> -d client_secret=<secret>
   ```

   A JSON response with an `access_token` means the account works.

2. **Validate against the live schema.** This checks every query the server sends and prints
   entity counts, the filter input shape and related type names:

   ```bash
   cp .env.example .env   # fill in HUB_URL, HUB_CLIENT_ID, HUB_CLIENT_SECRET
   set -a && source .env && set +a
   uv run python schema_check.py
   ```

   Every document should report `ok`. The check also notes a couple of spec defects in Hub's
   own schema (deprecated interface fields); they are ignored and harmless. If entity counts
   come back as 0 or queries return permission errors, check the account's role binding.

3. **Optional alerts.** From Altair (`https://<hub>/hub/altair`) > "Entity queries" >
   "Entity Details with Insights", paste the `insights { ... }` selection into
   `graphql/insights.graphql` to add alert-based health to `list_services_status`.

## Run locally

```bash
uv sync
set -a && source .env && set +a
uv run uvicorn server:create_app --factory --port 8000
```

Smoke test:

```bash
curl -s localhost:8000/health
curl -s -H "x-api-key: $MCP_API_KEY" localhost:8000/api/organizations
curl -s -H "x-api-key: $MCP_API_KEY" "localhost:8000/api/services-status?only_unhealthy=true"
```

Tests and lint:

```bash
uv run pytest
uv run ruff check .
```

## Deploy to Cloud Foundry

`HUB_URL` and the route are set in `manifest.yml`; adjust them for another Hub.

`../scripts/deploy.sh server` creates the credentials service and pushes the app (it keeps
an existing API key). By hand:

```bash
cf cups hub-graphql-mcp-creds -p '{"client_id":"<sa-id>","client_secret":"<sa-secret>","mcp_api_key":"'"$(openssl rand -hex 24)"'"}'
cf push -f manifest.yml
cf logs hub-graphql-mcp --recent
```

Read the generated key back with `cf env hub-graphql-mcp` (under `VCAP_SERVICES`) to configure
clients.

For a Tanzu agent, bind the server as a user-provided service tagged `mcp-server` (the agent
reaches the internal route over plain HTTP on port 8080 and needs a network policy):

```bash
cf cups hub-graphql-mcp-tools -t mcp-server \
  -p '{"url":"http://hub-graphql-mcp.apps.internal:8080/mcp","api_key":"<mcp_api_key>"}'
cf add-network-policy <agent-app> hub-graphql-mcp --protocol tcp --port 8080
```

The app is labelled `ai.tanzu.com/type: mcp-server` so Hub can detect it and it can be
published to the Marketplace. The default route is `hub-graphql-mcp.apps.internal` (for MCP
Gateway or agents); add a public route only for direct client access.

MCP client configuration:

```json
{
  "mcpServers": {
    "tanzu-hub-readonly": {
      "url": "https://<route>/mcp",
      "transport": "streamable-http",
      "headers": { "x-api-key": "<mcp_api_key>" }
    }
  }
}
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HUB_URL` | required | Hub base URL ending in `/hub` (https only) |
| `HUB_AUTH_MODE` | `client_credentials` | `client_credentials`; `graphql` (Altair mutation in `graphql/auth.graphql`); `static` (pre-issued token, for bootstrapping) |
| `HUB_TOKEN_URL` | `https://<hub-host>/auth/oauth/token` | Hub UAA token endpoint |
| `HUB_AUTH_VARIABLES` | unset | `graphql` mode: JSON variables using `$client_id` / `$client_secret` |
| `HUB_AUTH_TOKEN_PATH` / `HUB_AUTH_TTL_PATH` | auto-detect | `graphql` mode: dotted paths under `data` if detection fails |
| `HUB_FILTER_TEMPLATE` | `EQ` + `values` | Equality filter literal shape (the default matches Hub's `QueryFilter`) |
| `HUB_GUID_FIELD` | `properties.GUID` | Filter field for CF GUID lookups |
| `CA_BUNDLE` | unset (manifest: `/etc/ssl/certs/ca-certificates.crt`) | CA file for Hub TLS; the default trust store is used when unset or missing |
| `REQUEST_TIMEOUT_SECONDS` / `MAX_RETRIES` / `MAX_CONCURRENCY` | `20` / `3` / `8` | Hub client limits |
| `MAX_ITEMS` / `PAGE_SIZE` | `1000` / `100` | Pagination caps (results report `truncated`) |
| `ENABLE_MYSQL_DEEP_STATUS` | `false` | Enable live MySQL topology |
| `MYSQL_DEEP_STATUS_URL_TEMPLATE` | unset | GET endpoint with `{guid}`; switchboard `/v0/backends` lists are summarized |
| `ALLOW_UNAUTHENTICATED_MCP` | `false` | Only for an internal route behind a gateway that authenticates |
| `LOG_LEVEL` | `INFO` | JSON log level |

Credentials (`client_id`, `client_secret`, `mcp_api_key`, `static_token`,
`mysql_deep_username`, `mysql_deep_password`) come from the user-provided service named by
`HUB_CREDS_SERVICE` (default `hub-graphql-mcp-creds`), falling back to the matching upper-case
environment variables (`HUB_CLIENT_ID`, `HUB_CLIENT_SECRET`, `MCP_API_KEY`, ...).

## Project layout

| File | Role |
|---|---|
| `server.py` | FastAPI app factory: REST routes, `/mcp` mount, API-key middleware |
| `mcp_tools.py` | The seven MCP tools (read-only annotations) |
| `services.py` | Tool logic shared by MCP and REST |
| `queries.py` | GraphQL documents |
| `hub_client.py` | Hub HTTP client: token cache, retries, pagination, query-only guard |
| `auth.py` | Token providers (`client_credentials`, `graphql`, `static`) |
| `health.py` | Status normalization and BOSH / proxy rollups |
| `schema_check.py` | Live schema validation CLI |
| `tests/test_agent_example.py` | Keeps `../agent` (skill, AGENTS.md, manifests, deploy script) in sync with the tools |

## Known limitations

- The Hub GraphQL API is Tech Preview/Beta and may change. The shapes used here are pinned in
  `tests/hub_schema.graphql`; rerun `schema_check.py` after Hub upgrades.
- Filtering service instances by org, offering or foundation happens client-side after the
  space filter (which runs in Hub); responses list `filtered_client_side` and `truncated`.
- Hub reports MySQL BOSH VM state, but has no proxy-backend or replication view; that comes
  only from the optional deep-status endpoint (for example your existing dashboard app's
  MySQL route). Instances without their own BOSH deployment report no nodes.
- Hub does not inventory CF users as entities. `list_users` returns the CF foundation, org and
  space roles recorded in Hub, so roles granted directly with the cf CLI may be missing, and
  OAuth clients appear under their client ID.
