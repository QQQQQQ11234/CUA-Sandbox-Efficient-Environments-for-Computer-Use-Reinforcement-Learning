#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv-gitlab-eval/bin/python}"
LLAMA_PYTHON="${LLAMA_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
MODEL_DIR="${LLAMA_MODEL_DIR:-/path/to/Llama-3.1-8B-Instruct}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:18000}"
LLAMA_DEVICE_GROUPS="${LLAMA_DEVICE_GROUPS:-cuda:0,cuda:1;cuda:2,cuda:3}"
LLAMA_MAX_MODEL_LEN="${LLAMA_MAX_MODEL_LEN:-131072}"
LLAMA_ADAPTIVE_OBSERVATION_COMPRESSION="${LLAMA_ADAPTIVE_OBSERVATION_COMPRESSION:-true}"
EVAL_MAX_CONCURRENT="${EVAL_MAX_CONCURRENT:-2}"
EVAL_MAX_CONCURRENT_LAUNCH="${EVAL_MAX_CONCURRENT_LAUNCH:-$EVAL_MAX_CONCURRENT}"
EVAL_MAX_TOKENS="${EVAL_MAX_TOKENS:-1000}"
AGENT_TEMPERATURE="${AGENT_TEMPERATURE:-1}"

run_llama() {
  local output_dir="$1"
  local llama_args
  set -o pipefail
  cd "$PROJECT_ROOT"
  llama_args=(
    scripts/llama31_openai_server.py
    --model "$MODEL_DIR"
    --host 127.0.0.1
    --port 18000
    --device-groups "$LLAMA_DEVICE_GROUPS"
    --max-model-len "$LLAMA_MAX_MODEL_LEN"
  )
  if [[ "$LLAMA_ADAPTIVE_OBSERVATION_COMPRESSION" == "true" ]]; then
    llama_args+=(--adaptive-observation-compression)
  fi
  env \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    TOKENIZERS_PARALLELISM=false \
    "$LLAMA_PYTHON" "${llama_args[@]}" \
    2>&1 | tee "$output_dir/llama_server.log"
}

run_eval() {
  local output_dir="$1"
  local status
  cd "$PROJECT_ROOT"

  while ! curl --fail --silent "$LLAMA_URL/v1/models" >/dev/null; do
    printf '[%s] waiting for Llama server\n' "$(date --iso-8601=seconds)" \
      | tee -a "$output_dir/eval.log"
    sleep 5
  done

  {
    echo "started_at=$(date --iso-8601=seconds)"
    echo "git_commit=$(git rev-parse HEAD)"
    echo "model_dir=$MODEL_DIR"
    echo "llama_url=$LLAMA_URL/v1"
    echo "task_ids=${TASK_IDS:-all}"
    echo "max_concurrent=$EVAL_MAX_CONCURRENT"
    echo "llama_device_groups=$LLAMA_DEVICE_GROUPS"
    echo "llama_max_model_len=$LLAMA_MAX_MODEL_LEN"
    echo "adaptive_observation_compression=$LLAMA_ADAPTIVE_OBSERVATION_COMPRESSION"
    echo "evaluator_model=openai/Llama-3.1-8B-Instruct"
    echo "evaluator_base_url=$LLAMA_URL/v1"
    echo "agent_temperature=$AGENT_TEMPERATURE"
    echo "evaluator_temperature=0"
    echo "max_steps=30"
    echo "max_tokens=$EVAL_MAX_TOKENS"
    sha256sum \
      rl_web_agent/prompts/system_cot.txt \
      rl_web_agent/prompts/system_cot.txt \
      thirdparty/webarena/config_files/test.raw.json
  } >"$output_dir/run_metadata.txt"

  set +e
  set -o pipefail
  env \
    OPENAI_API_KEY=local-llama \
    LLM_PROVIDER=openai \
    OPENAI_MODEL=Llama-3.1-8B-Instruct \
    OPENAI_TIMEOUT=300 \
    OPENAI_MAX_RETRIES=1 \
    LITELLM_LOG=ERROR \
    LLM_TEMPERATURE="$AGENT_TEMPERATURE" \
    MAX_CONCURRENT="$EVAL_MAX_CONCURRENT" \
    MAX_CONCURRENT_LAUNCH="$EVAL_MAX_CONCURRENT_LAUNCH" \
    RETRY_COUNT=1 \
    PYTHONUNBUFFERED=1 \
    ./scripts/run_gitlab_db_accuracy.sh \
      --output_dir "$output_dir" \
      --max_concurrent "$EVAL_MAX_CONCURRENT" \
      --max_concurrent_launch "$EVAL_MAX_CONCURRENT_LAUNCH" \
      --retry_count 1 \
      -- \
      agent.max_steps=30 \
      llm.openai.base_url="$LLAMA_URL/v1" \
      llm.generation.max_tokens="$EVAL_MAX_TOKENS" \
      evaluator_llm.provider=openai \
      evaluator_llm.model=openai/Llama-3.1-8B-Instruct \
      evaluator_llm.base_url="$LLAMA_URL/v1" \
      evaluator_llm.api_key=local-llama \
      evaluator_llm.temperature=0 \
      evaluator_llm.max_tokens=64 \
      2>&1 | tee -a "$output_dir/eval.log"
  status=${PIPESTATUS[0]}
  set -e
  printf '%s\n' "$status" >"$output_dir/exit_code"
  printf 'finished_at=%s\nexit_code=%s\n' \
    "$(date --iso-8601=seconds)" "$status" >>"$output_dir/run_metadata.txt"
  return "$status"
}

start_tmux() {
  local timestamp session output_dir
  timestamp="$(date +%Y%m%d_%H%M%S)"
  session="${TMUX_SESSION:-gitlab_llama31_full25_$timestamp}"
  output_dir="${OUTPUT_DIR:-$PROJECT_ROOT/results/$session}"

  if env -u TMUX tmux has-session -t "$session" 2>/dev/null; then
    echo "tmux session already exists: $session" >&2
    return 1
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python environment not found: $PYTHON_BIN" >&2
    return 1
  fi
  if [[ ! -x "$LLAMA_PYTHON" ]]; then
    echo "Llama Python environment not found: $LLAMA_PYTHON" >&2
    return 1
  fi
  if [[ ! -d "$MODEL_DIR" ]]; then
    echo "Llama model not found: $MODEL_DIR" >&2
    return 1
  fi

  mkdir -p "$output_dir"
  printf '%s\n' "$session" >"$output_dir/tmux_session"
  env -u TMUX tmux new-session -d -s "$session" -n llama \
    "$PROJECT_ROOT/scripts/run_gitlab_llama31_full25_tmux.sh _llama '$output_dir'"
  env -u TMUX tmux new-window -d -t "$session" -n eval \
    "$PROJECT_ROOT/scripts/run_gitlab_llama31_full25_tmux.sh _eval '$output_dir'"

  echo "TMUX_SESSION=$session"
  echo "OUTPUT_DIR=$output_dir"
  echo "ATTACH=env -u TMUX tmux attach -t $session"
}

case "${1:-start}" in
  _llama)
    run_llama "$2"
    ;;
  _eval)
    run_eval "$2"
    ;;
  start)
    start_tmux
    ;;
  *)
    echo "usage: $0 [start]" >&2
    exit 2
    ;;
esac
