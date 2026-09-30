#!/usr/bin/env bash
# Run an inference batch against a WebArena-compatible task directory.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tasks_dir="${TASKS_DIR:-$project_root/thirdparty/webarena/config_files}"
output_dir="${OUTPUT_DIR:-$project_root/results/inference}"
task_ids="${TASK_IDS:-0}"

cd "$project_root"
exec python -m rl_web_agent.entrypoints.batch_agent \
  --tasks_dir "$tasks_dir" \
  --task_ids "$task_ids" \
  --output_dir "$output_dir" \
  --agent_type "${AGENT_TYPE:-tool}" \
  --max_concurrent "${MAX_CONCURRENT:-1}" \
  "$@"
