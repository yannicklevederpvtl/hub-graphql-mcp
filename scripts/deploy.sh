#!/usr/bin/env bash
# Deploy the MCP server and the example agent into the targeted org/space.
#   set -a; source .envrc; set +a   # see .envrc.example
#   scripts/deploy.sh               # both
#   scripts/deploy.sh server        # MCP server only
#   scripts/deploy.sh agent         # agent only (server must already be deployed)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_APP=hub-graphql-mcp
AGENT_APP=hub-platform-agent
CREDS_SERVICE=hub-graphql-mcp-creds    # server: Hub service account + MCP API key
TOOLS_SERVICE=hub-graphql-mcp-tools    # agent: MCP binding (url + api_key)
MODEL_SERVICE=hub-platform-model       # agent: ai-models
SSO_SERVICE=hub-platform-sso           # agent: p-identity (optional)
SERVER_PORT=8080                       # container port behind the apps.internal route
MCP_URL="${MCP_URL:-http://$SERVER_APP.apps.internal:$SERVER_PORT/mcp}"

step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'fail  %s\n' "$*" >&2; exit 1; }
warn() { printf 'warn  %s\n' "$*" >&2; }
require_var() { local v; for v in "$@"; do [[ -n "${!v:-}" ]] || die "set $v (see .envrc.example)"; done; }
service_exists() { cf service "$1" >/dev/null 2>&1; }

ensure_cups() {
  local name="$1" creds="$2" tags="${3:-}" verb=create
  service_exists "$name" && verb=update
  step "cf $verb-user-provided-service $name"
  if [[ -n "$tags" ]]; then
    cf "$verb-user-provided-service" "$name" -p "$creds" -t "$tags"
  else
    cf "$verb-user-provided-service" "$name" -p "$creds"
  fi
}

# Keep the existing MCP API key unless MCP_API_KEY is set, so redeploys don't rotate it.
mcp_api_key() {
  if [[ -n "${MCP_API_KEY:-}" ]]; then
    printf '%s' "$MCP_API_KEY"
  elif service_exists "$CREDS_SERVICE"; then
    cf curl "/v3/service_instances/$(cf service "$CREDS_SERVICE" --guid)/credentials" \
      | jq -r '.mcp_api_key // empty'
  fi
}

deploy_server() {
  require_var HUB_CLIENT_ID HUB_CLIENT_SECRET
  local key
  key="$(mcp_api_key)"
  [[ -n "$key" ]] || key="$(openssl rand -hex 24)"
  ensure_cups "$CREDS_SERVICE" "$(jq -cn --arg id "$HUB_CLIENT_ID" --arg secret "$HUB_CLIENT_SECRET" \
    --arg key "$key" '{client_id: $id, client_secret: $secret, mcp_api_key: $key}')"
  step "cf push $SERVER_APP"
  cf push -f "$ROOT_DIR/mcp-server/manifest.yml"
}

deploy_agent() {
  require_var MODEL_PLAN
  service_exists "$CREDS_SERVICE" || die "$CREDS_SERVICE not found: deploy the server first"
  local key
  key="$(mcp_api_key)"
  [[ -n "$key" ]] || die "no mcp_api_key in $CREDS_SERVICE"

  ensure_cups "$TOOLS_SERVICE" "$(jq -cn --arg url "$MCP_URL" --arg key "$key" \
    '{url: $url, api_key: $key}')" mcp-server
  if service_exists "$MODEL_SERVICE"; then
    echo "  ok  $MODEL_SERVICE exists"
  else
    step "cf create-service ai-models $MODEL_PLAN $MODEL_SERVICE"
    cf create-service ai-models "$MODEL_PLAN" "$MODEL_SERVICE" --wait
  fi

  step "cf push $AGENT_APP (not started)"
  cf push -f "$ROOT_DIR/agent/manifest.yml" --no-start
  if [[ -n "${DEFAULT_MODEL:-}" ]]; then
    cf set-env "$AGENT_APP" TANZU_AGENT_DEFAULT_MODEL "$DEFAULT_MODEL" >/dev/null
  fi
  if [[ -n "${SSO_PLAN:-}" ]]; then
    service_exists "$SSO_SERVICE" || cf create-service p-identity "$SSO_PLAN" "$SSO_SERVICE" --wait
    cf bind-service "$AGENT_APP" "$SSO_SERVICE"
  else
    warn "SSO_PLAN not set: anyone who can reach the agent's route can query it"
  fi

  if [[ "$MCP_URL" == *".apps.internal"* ]]; then
    step "cf add-network-policy $AGENT_APP $SERVER_APP --port $SERVER_PORT"
    cf add-network-policy "$AGENT_APP" "$SERVER_APP" --protocol tcp --port "$SERVER_PORT"
  fi

  step "cf start $AGENT_APP"
  cf start "$AGENT_APP"
}

command -v cf >/dev/null && command -v jq >/dev/null && command -v openssl >/dev/null \
  || die "needs cf, jq and openssl"
cf target >/dev/null 2>&1 || die "not logged in: cf login -a <API> --sso"

case "${1:-all}" in
  server) deploy_server ;;
  agent)  deploy_agent ;;
  all)    deploy_server; deploy_agent ;;
  *)      die "usage: $0 [server|agent|all]" ;;
esac

cat <<EOF

Done. Check:
  cf app $SERVER_APP && cf app $AGENT_APP
  Open the agent, then the debug panel: MCP server "$TOOLS_SERVICE" connected with 7 tools.
  Ask: "Which service instances are unhealthy right now?"
EOF
