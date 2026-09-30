#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
webarena_root="${WEBARENA_ROOT:-/path/to/webarena-main}"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 TASK_IDS_FILE RESULT_DIR [runner options...]" >&2
  exit 2
fi
task_ids_file="$1"
result_dir="$2"
shift 2

cd "$project_root"

export REDDIT="${REDDIT:-http://127.0.0.1:9999}"
export WEBARENA_SHOPPING_AUTHORITY="${WEBARENA_SHOPPING_AUTHORITY:-127.0.0.1:7770}"
export SHOPPING="${SHOPPING:-http://${WEBARENA_SHOPPING_AUTHORITY}}"
export SHOPPING_ADMIN="${SHOPPING_ADMIN:-http://127.0.0.1:7780/admin}"
export GITLAB="${GITLAB:-http://127.0.0.1:8023}"
export WIKIPEDIA="${WIKIPEDIA:-http://127.0.0.1:8888/wikipedia_en_all_maxi_2022-05/A/User:The_other_Kiwix_guy/Landing}"
export MAP="${MAP:-http://127.0.0.1:3000}"
export HOMEPAGE="${HOMEPAGE:-http://127.0.0.1:4399}"

export OPENAI_API_KEY="${OPENAI_API_KEY:-EMPTY}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-${OPENAI_BASE_URL:-http://127.0.0.1:8001/v1}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-$OPENAI_API_BASE}"
export WEBARENA_JUDGE_MODEL="${WEBARENA_JUDGE_MODEL:-Qwen/Qwen3.5-9B}"
export ROUTE_TOKEN_SECRET
if [[ -z "${ROUTE_TOKEN_SECRET:-}" ]]; then
  ROUTE_TOKEN_SECRET="$(docker inspect shared-shopping --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^WEB_AGENT_ROUTE_TOKEN_SECRET=//p' | head -1)"
fi
export ROUTE_REGISTRY_PATH="${ROUTE_REGISTRY_PATH:-$project_root/runtime/shopping_db_routes.sqlite3}"
export SHOPPING_TARGET="${SHOPPING_TARGET:-127.0.0.1:7770}"
export MYSQL_RUNTIME_CONTAINER="${MYSQL_RUNTIME_CONTAINER:-shopping-mysql-runtime}"
export MAGENTO_SHARED_CONTAINER="${MAGENTO_SHARED_CONTAINER:-shared-shopping}"
export MAGENTO_STATE_HOOKS_ENABLED="${MAGENTO_STATE_HOOKS_ENABLED:-true}"
export PYTHONUNBUFFERED=1

curl --fail --silent "${OPENAI_API_BASE%/}/models" >/dev/null
curl --fail --silent http://127.0.0.1:8766/health >/dev/null
docker inspect -f '{{.State.Running}}' "$MYSQL_RUNTIME_CONTAINER" | grep -qx true
docker inspect -f '{{.State.Running}}' "$MAGENTO_SHARED_CONTAINER" | grep -qx true

exec "$python_bin" experiments/shopping/run_official_storefront_db_eval.py \
  --webarena_root "$webarena_root" \
  --task_ids_file "$task_ids_file" \
  --result_dir "$result_dir" \
  "$@"
