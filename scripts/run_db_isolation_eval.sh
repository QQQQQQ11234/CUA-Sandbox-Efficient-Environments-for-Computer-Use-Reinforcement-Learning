#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

usage() {
  cat <<'EOF'
Run CUA-Sandbox evaluation in DB isolation mode (no training).

Required env (choose one database admin mode):
  DB_ADMIN_DSN                PostgreSQL admin DSN
  DB_ADMIN_DOCKER_CONTAINER   Local WebArena GitLab container name
  ROUTE_TOKEN_SECRET          Shared HMAC secret, at least 32 bytes
                              e.g. postgresql://postgres:postgres@127.0.0.1:5432/postgres

Common env (optional):
  OPENAI_API_KEY              Needed when using --provider openai
  PYTHON_BIN                  Python executable (default: python3.10)
  TASK_IDS                    Comma-separated task IDs (default: 1)
  TASKS_DIR                   Task json directory
  AGENT_TYPE                  regular | tool (default: regular)
  LLM_PROVIDER                openai | bedrock (default: openai)
  OPENAI_MODEL                default: gpt-4o-mini
  LLM_TEMPERATURE             default: 1 (official Qwen run)
  LLM_TOP_P                   default: 0.9 (official Qwen run)
  LLM_MAX_TOKENS              default: 384 (official Qwen run)
  BASE_TEMPLATE_DB            default: base_template_db
  DB_CLONE_STRATEGY           FILE_COPY | WAL_LOG | TEMPLATE (default: FILE_COPY)
  MAX_CONCURRENT              default: 1
  MAX_CONCURRENT_LAUNCH       default: 1
  RETRY_COUNT                 default: 3
  OUTPUT_DIR                  output path (default: ./results/db_iso_YYYYmmdd_HHMMSS)

Shared web target env (for host rewrite):
  SHOPPING_TARGET             default: 127.0.0.1:7770
  SHOPPING_ADMIN_TARGET       default: 127.0.0.1:7780
  REDDIT_TARGET               default: 127.0.0.1:9999
  GITLAB_TARGET               default: 127.0.0.1:8023
  GITLAB_SITE_HOST            browser-visible GitLab host (default: configured WebArena host)
  PROXY_ENABLED               true | false (default: true)
  MAP_TARGET                  default: 127.0.0.1:3000
  WIKIPEDIA_TARGET            default: 127.0.0.1:8888

Usage:
  DB_ADMIN_DSN='postgresql://...' OPENAI_API_KEY='...' \
    ./scripts/run_db_isolation_eval.sh --task_ids 1 --agent_type tool

Extra hydra overrides:
  ./scripts/run_db_isolation_eval.sh --task_ids 1 -- \
    llm.openai.base_url=https://api.openai.com/v1
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-python3.10}"
TASK_IDS="${TASK_IDS:-1}"
TASKS_DIR="${TASKS_DIR:-$PROJECT_ROOT/thirdparty/webarena/config_files/examples}"
AGENT_TYPE="${AGENT_TYPE:-regular}"
LLM_PROVIDER="${LLM_PROVIDER:-openai}"
OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}"
OPENAI_TIMEOUT="${OPENAI_TIMEOUT:-60}"
OPENAI_MAX_RETRIES="${OPENAI_MAX_RETRIES:-2}"
LLM_TEMPERATURE="${LLM_TEMPERATURE:-1}"
LLM_TOP_P="${LLM_TOP_P:-0.9}"
LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-384}"
MAX_CONCURRENT="${MAX_CONCURRENT:-1}"
MAX_CONCURRENT_LAUNCH="${MAX_CONCURRENT_LAUNCH:-1}"
RETRY_COUNT="${RETRY_COUNT:-3}"
BASE_TEMPLATE_DB="${BASE_TEMPLATE_DB:-base_template_db}"
DB_CLONE_STRATEGY="${DB_CLONE_STRATEGY:-FILE_COPY}"
ROUTE_REGISTRY_PATH="${ROUTE_REGISTRY_PATH:-$PROJECT_ROOT/runtime/db_routes.sqlite3}"
DROP_ON_CLOSE="${DROP_ON_CLOSE:-true}"
RESET_ON_SETUP="${RESET_ON_SETUP:-true}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/results/db_iso_$(date +%Y%m%d_%H%M%S)}"

SHOPPING_TARGET="${SHOPPING_TARGET:-127.0.0.1:7770}"
SHOPPING_ADMIN_TARGET="${SHOPPING_ADMIN_TARGET:-127.0.0.1:7780}"
REDDIT_TARGET="${REDDIT_TARGET:-127.0.0.1:9999}"
GITLAB_TARGET="${GITLAB_TARGET:-127.0.0.1:8023}"
GITLAB_SITE_HOST="${GITLAB_SITE_HOST:-metis.lti.cs.cmu.edu:8023}"
PROXY_ENABLED="${PROXY_ENABLED:-true}"
MAP_TARGET="${MAP_TARGET:-127.0.0.1:3000}"
WIKIPEDIA_TARGET="${WIKIPEDIA_TARGET:-127.0.0.1:8888}"

DB_ADMIN_DSN="${DB_ADMIN_DSN:-}"
DB_ADMIN_DOCKER_CONTAINER="${DB_ADMIN_DOCKER_CONTAINER:-}"
if [[ -z "$DB_ADMIN_DSN" && -z "$DB_ADMIN_DOCKER_CONTAINER" ]]; then
  echo "[ERROR] DB_ADMIN_DSN or DB_ADMIN_DOCKER_CONTAINER is required."
  exit 1
fi

ROUTE_TOKEN_SECRET="${ROUTE_TOKEN_SECRET:-}"
if [[ ${#ROUTE_TOKEN_SECRET} -lt 32 ]]; then
  echo "[ERROR] ROUTE_TOKEN_SECRET must contain at least 32 characters."
  echo "Generate one with: openssl rand -hex 32"
  exit 1
fi

if [[ "$AGENT_TYPE" != "regular" && "$AGENT_TYPE" != "tool" ]]; then
  echo "[ERROR] --agent_type must be regular or tool"
  exit 1
fi

if [[ "$LLM_PROVIDER" != "openai" && "$LLM_PROVIDER" != "bedrock" ]]; then
  echo "[ERROR] --provider must be openai or bedrock"
  exit 1
fi

EXTRA_OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --task_ids)
      TASK_IDS="${2:-}"
      shift 2
      ;;
    --tasks_dir)
      TASKS_DIR="${2:-}"
      shift 2
      ;;
    --agent_type)
      AGENT_TYPE="${2:-}"
      shift 2
      ;;
    --provider)
      LLM_PROVIDER="${2:-}"
      shift 2
      ;;
    --openai_model)
      OPENAI_MODEL="${2:-}"
      shift 2
      ;;
    --output_dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --max_concurrent)
      MAX_CONCURRENT="${2:-}"
      shift 2
      ;;
    --max_concurrent_launch)
      MAX_CONCURRENT_LAUNCH="${2:-}"
      shift 2
      ;;
    --retry_count)
      RETRY_COUNT="${2:-}"
      shift 2
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        EXTRA_OVERRIDES+=("$1")
        shift
      done
      ;;
    *)
      echo "[ERROR] Unknown arg: $1"
      usage
      exit 1
      ;;
  esac
done

mkdir -p "$OUTPUT_DIR"

if [[ "$LLM_PROVIDER" == "openai" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "[WARN] OPENAI_API_KEY is empty. OpenAI provider will fail without key."
fi

cmd=(
  "$PYTHON_BIN" -m rl_web_agent.entrypoints.batch_agent
  --task_ids "$TASK_IDS"
  --tasks_dir "$TASKS_DIR"
  --output_dir "$OUTPUT_DIR"
  --agent_type "$AGENT_TYPE"
  --max_concurrent "$MAX_CONCURRENT"
  --max_concurrent_launch "$MAX_CONCURRENT_LAUNCH"
  --retry_count "$RETRY_COUNT"

  "environment.isolation.mode=db"
  "environment.db_isolation.admin_dsn=$DB_ADMIN_DSN"
  "environment.db_isolation.admin_docker_container=$DB_ADMIN_DOCKER_CONTAINER"
  "environment.db_isolation.base_template_db=$BASE_TEMPLATE_DB"
  "environment.db_isolation.clone_strategy=$DB_CLONE_STRATEGY"
  "environment.db_isolation.route_registry_path=$ROUTE_REGISTRY_PATH"
  "environment.db_isolation.route_token_secret=$ROUTE_TOKEN_SECRET"
  "environment.db_isolation.drop_on_close=$DROP_ON_CLOSE"
  "environment.db_isolation.reset_on_setup=$RESET_ON_SETUP"

  "environment.db_isolation.shared_site_hosts.shopping=$SHOPPING_TARGET"
  "environment.db_isolation.shared_site_hosts.shopping_admin=$SHOPPING_ADMIN_TARGET"
  "environment.db_isolation.shared_site_hosts.reddit=$REDDIT_TARGET"
  "environment.db_isolation.shared_site_hosts.gitlab=$GITLAB_TARGET"
  "environment.db_isolation.shared_site_hosts.map=$MAP_TARGET"
  "environment.db_isolation.shared_site_hosts.wikipedia=$WIKIPEDIA_TARGET"
  "environment.sites.gitlab=$GITLAB_SITE_HOST"
  "environment.proxy.enabled=$PROXY_ENABLED"

  "llm.provider=$LLM_PROVIDER"
  "llm.openai.model=$OPENAI_MODEL"
  "llm.openai.timeout=$OPENAI_TIMEOUT"
  "llm.openai.max_retries=$OPENAI_MAX_RETRIES"
  "llm.generation.temperature=$LLM_TEMPERATURE"
  "llm.generation.top_p=$LLM_TOP_P"
  "llm.generation.max_tokens=$LLM_MAX_TOKENS"
  "evaluator_llm.provider=openai"
  "evaluator_llm.model=openai/$OPENAI_MODEL"
  "evaluator_llm.base_url=${EVALUATOR_LLM_BASE_URL:-${OPENAI_BASE_URL:-http://127.0.0.1:8000/v1}}"
  "evaluator_llm.api_key=${EVALUATOR_LLM_API_KEY:-${OPENAI_API_KEY:-EMPTY}}"
)

for override in "${EXTRA_OVERRIDES[@]}"; do
  cmd+=("$override")
done

echo "[INFO] Running command:"
printf '  %q' "${cmd[@]}"
echo
echo "[INFO] Output dir: $OUTPUT_DIR"

"${cmd[@]}"

echo "[DONE] Finished. Check summary in:"
echo "  $OUTPUT_DIR/batch_summary.json"
