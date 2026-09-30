#!/usr/bin/env bash
set -euo pipefail

GITLAB_URL="${GITLAB_URL:-http://127.0.0.1:8023}"
AGENT_A_TOKEN="${AGENT_A_TOKEN:-}"
AGENT_B_TOKEN="${AGENT_B_TOKEN:-}"

if [[ -z "$AGENT_A_TOKEN" || -z "$AGENT_B_TOKEN" ]]; then
  echo "[ERROR] AGENT_A_TOKEN and AGENT_B_TOKEN are required"
  exit 1
fi

request() {
  local token="$1"
  curl --fail --silent --show-error \
    -H "X-Agent-Route: $token" \
    -H "X-Agent-ID: spoofed-agent" \
    -H "X-Agent-DB: spoofed_database" \
    "$GITLAB_URL/-/health"
}

echo "[INFO] Checking Agent A route"
request "$AGENT_A_TOKEN"
echo
echo "[INFO] Checking Agent B route"
request "$AGENT_B_TOKEN"
echo
echo "[DONE] HTTP route checks passed; run DB write/read assertions separately."
