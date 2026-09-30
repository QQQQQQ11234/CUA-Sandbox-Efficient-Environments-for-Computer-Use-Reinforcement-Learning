#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv-gitlab-eval/bin/python}"
GITLAB_URL="${GITLAB_URL:-http://127.0.0.1:8023}"
GITLAB_HOST="${GITLAB_URL#http://}"
GITLAB_HOST="${GITLAB_HOST#https://}"
TASKS_DIR="${TASKS_DIR:-$PROJECT_ROOT/runtime/gitlab_eval_tasks}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-$PROJECT_ROOT/runtime/gitlab_nondb_config.json}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[ERROR] Python environment not found: $PYTHON_BIN"
  exit 1
fi

# Fail before launching tasks when evaluator-only dependencies or NLTK data
# are missing. Otherwise affected tasks appear as ordinary reward=0 failures.
if ! "$PYTHON_BIN" - <<'PY'
import nltk
import litellm  # noqa: F401
PY
then
  echo "[ERROR] Evaluator dependencies are incomplete."
  echo "        Install litellm and nltk in $PYTHON_BIN:"
  echo "        $PYTHON_BIN -m pip install 'litellm>=1.75.3'"
  exit 1
fi

if ! curl --fail --silent "${ROUTE_REGISTRY_HEALTH_URL:-http://127.0.0.1:8765/health}" >/dev/null; then
  echo "[ERROR] route registry is not healthy"
  exit 1
fi

if ! curl --fail --silent "$GITLAB_URL/-/health" >/dev/null; then
  echo "[ERROR] GitLab is not healthy at $GITLAB_URL"
  exit 1
fi

"$PYTHON_BIN" experiments/gitlab/prepare_db_eval_tasks.py \
  --output-dir "$TASKS_DIR" --gitlab-url "$GITLAB_URL"

if [[ -z "${TASK_IDS:-}" ]]; then
  TASK_IDS="$($PYTHON_BIN - <<'PY'
import json
from pathlib import Path
manifest = json.loads(Path('experiments/gitlab/gitlab_task_contracts.json').read_text())
print(','.join(str(task['task_id']) for task in manifest['tasks']))
PY
)"
fi

if [[ -z "${ROUTE_TOKEN_SECRET:-}" ]]; then
  ROUTE_TOKEN_SECRET="$($PYTHON_BIN - "$RUNTIME_CONFIG" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())['route_secret'])
PY
)"
fi

if [[ "${LLM_PROVIDER:-openai}" == "openai" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "[ERROR] OPENAI_API_KEY is required for a model accuracy run."
  echo "The deterministic WebArena smoke test is available via:"
  echo "  $PYTHON_BIN experiments/gitlab/run_db_smoke.py"
  exit 1
fi

export PYTHON_BIN TASK_IDS TASKS_DIR ROUTE_TOKEN_SECRET
export DB_ADMIN_DSN="${DB_ADMIN_DSN:-postgresql://postgres:postgres@127.0.0.1:55433/postgres}"
export DB_ADMIN_DOCKER_CONTAINER="${DB_ADMIN_DOCKER_CONTAINER:-}"
export BASE_TEMPLATE_DB="${BASE_TEMPLATE_DB:-gitlab_base_template}"
export DB_CLONE_STRATEGY="${DB_CLONE_STRATEGY:-FILE_COPY}"
export ROUTE_REGISTRY_PATH="${ROUTE_REGISTRY_PATH:-$PROJECT_ROOT/runtime/db_routes_nondb.sqlite3}"
export NON_DB_STATE_ENABLED="${NON_DB_STATE_ENABLED:-true}"
export GITLAB_NON_DB_ADAPTER_ENABLED="${GITLAB_NON_DB_ADAPTER_ENABLED:-true}"
export GITLAB_SHARED_CONTAINER="${GITLAB_SHARED_CONTAINER:-shared-gitlab-nondb}"
export GITLAB_TARGET="$GITLAB_HOST"
export GITLAB_SITE_HOST="$GITLAB_HOST"
export PROXY_ENABLED=false
export DROP_ON_CLOSE="${DROP_ON_CLOSE:-true}"
export RESET_ON_SETUP="${RESET_ON_SETUP:-true}"
export MAX_CONCURRENT="${MAX_CONCURRENT:-1}"
export MAX_CONCURRENT_LAUNCH="${MAX_CONCURRENT_LAUNCH:-1}"
export RETRY_COUNT="${RETRY_COUNT:-3}"
export OPENAI_TIMEOUT="${OPENAI_TIMEOUT:-300}"
export OPENAI_MAX_RETRIES="${OPENAI_MAX_RETRIES:-1}"
export LLM_TEMPERATURE="${LLM_TEMPERATURE:-1}"
export LLM_TOP_P="${LLM_TOP_P:-0.9}"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-384}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/var/lib/web-agent/pg18_clone_xfs/playwright/cua-sandbox}"

# Install/check this project's exact Playwright revision in its private cache.
# This cannot remove official WebArena's Chromium 1055 from the shared cache.
if ! PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" \
  "$PYTHON_BIN" -m playwright install --dry-run chromium \
  | grep -q "$PLAYWRIGHT_BROWSERS_PATH/chromium-1179"; then
  echo "[ERROR] Unexpected Playwright Chromium revision for $PYTHON_BIN"
  exit 1
fi
if [[ ! -x "$PLAYWRIGHT_BROWSERS_PATH/chromium_headless_shell-1179/chrome-linux/headless_shell" ]]; then
  echo "[ERROR] CUA-Sandbox Chromium 1179 is missing from $PLAYWRIGHT_BROWSERS_PATH"
  echo "        Install it with:"
  echo "        PLAYWRIGHT_BROWSERS_PATH='$PLAYWRIGHT_BROWSERS_PATH' $PYTHON_BIN -m playwright install chromium"
  exit 1
fi

exec "$PROJECT_ROOT/scripts/run_db_isolation_eval.sh" "$@"
