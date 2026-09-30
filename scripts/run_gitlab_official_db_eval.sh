#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
webarena_root="${WEBARENA_ROOT:-/path/to/webarena}"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 TASK_IDS_FILE RESULT_DIR [run_official_db_eval.py options...]" >&2
  exit 2
fi
task_ids_file="$1"
result_dir="$2"
shift 2

cd "$project_root"

# Match the official WebArena process environment. GitLab alone points to the
# shared DB-routing frontend; this is the intentional isolation boundary.
export REDDIT="${REDDIT:-http://172.17.0.1:9999}"
export SHOPPING="${SHOPPING:-http://172.17.0.1:7770}"
export SHOPPING_ADMIN="${SHOPPING_ADMIN:-http://172.17.0.1:7780/admin}"
export GITLAB="${GITLAB:-http://127.0.0.1:8023}"
export WIKIPEDIA="${WIKIPEDIA:-http://172.17.0.1:8888/wikipedia_en_all_maxi_2022-05/A/User:The_other_Kiwix_guy/Landing}"
export MAP="${MAP:-http://172.17.0.1:3000}"
export HOMEPAGE="${HOMEPAGE:-http://172.17.0.1:4399}"

export OPENAI_API_KEY="${OPENAI_API_KEY:-EMPTY}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:8001/v1}"
export ROUTE_TOKEN_SECRET
if [[ -z "${ROUTE_TOKEN_SECRET:-}" ]]; then
  ROUTE_TOKEN_SECRET="$($python_bin -c 'import json; print(json.load(open("runtime/gitlab_nondb_config.json"))["route_secret"])')"
fi
export DB_ADMIN_DSN="${DB_ADMIN_DSN:-postgresql://postgres:postgres@127.0.0.1:55433/postgres}"
export BASE_TEMPLATE_DB="${BASE_TEMPLATE_DB:-gitlab_base_template}"
export DB_CLONE_STRATEGY="${DB_CLONE_STRATEGY:-FILE_COPY}"
export ROUTE_REGISTRY_PATH="${ROUTE_REGISTRY_PATH:-$project_root/runtime/db_routes_nondb.sqlite3}"
export GITLAB_SHARED_CONTAINER="${GITLAB_SHARED_CONTAINER:-shared-gitlab-nondb}"
export GITLAB_TARGET="${GITLAB_TARGET:-127.0.0.1:8023}"
export PYTHONUNBUFFERED=1

curl --fail --silent "${OPENAI_BASE_URL%/}/models" >/dev/null
curl --fail --silent http://127.0.0.1:8765/health >/dev/null

exec "$python_bin" experiments/gitlab/run_official_db_eval.py \
  --webarena_root "$webarena_root" \
  --task_ids_file "$task_ids_file" \
  --result_dir "$result_dir" \
  "$@"
