#!/usr/bin/env bash
set -uo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_root="${1:?usage: $0 OUTPUT_ROOT}"
model="google/gemma-4-31B-it"
model_base="${OPENAI_API_BASE:-http://127.0.0.1:8004/v1}"
judge_base="${WEBARENA_JUDGE_API_BASE:-http://127.0.0.1:8010/v1}"
judge_model="${WEBARENA_JUDGE_MODEL:-Qwen/Qwen3.5-9B}"
python_bin="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"

gitlab_ids="${project_root}/results/gpt56_terra_low_gitlab_cua-sandbox_dbiso_clean180_maxsteps30_20260818/task_ids.txt"
cms_ids="${project_root}/results/gpt56_terra_low_cms_cua-sandbox_dbiso_clean182_maxsteps30_20260820/task_ids.txt"
shopping_ids="${project_root}/results/qwen35_9b_shopping_dbiso_webarena_main_clean187_gpu1_maxsteps30_20260819_153115/task_ids.txt"

mkdir -p "${output_root}"
coordinator_log="${output_root}/coordinator.log"
exec > >(tee -a "${coordinator_log}") 2>&1

timestamp() { date --iso-8601=seconds; }
log() { echo "$(timestamp) $*"; }

export WEBARENA_ROOT="/path/to/webarena-main"
export OPENAI_API_BASE="${model_base}"
export OPENAI_BASE_URL="${model_base}"
export OPENAI_API_KEY="local-gemma"
export WEBARENA_JUDGE_API_BASE="${judge_base}"
export WEBARENA_JUDGE_MODEL="${judge_model}"
export WEBARENA_JUDGE_API_KEY="${WEBARENA_JUDGE_API_KEY:-EMPTY}"
export PYTHONUNBUFFERED=1

preflight() {
  curl -fsS --max-time 10 "${model_base}/models" | grep -Fq "${model}"
  curl -fsS --max-time 10 "${judge_base}/models" | grep -Fq "${judge_model}"
  curl -fsS --max-time 10 http://127.0.0.1:8765/health >/dev/null
  curl -fsS --max-time 10 http://127.0.0.1:8766/health >/dev/null
  for container in \
    shared-gitlab-nondb \
    shared-shopping-admin \
    shared-shopping \
    shopping-admin-mysql-runtime \
    shopping-mysql-runtime; do
    docker inspect -f '{{.State.Running}}' "${container}" | grep -qx true
  done
  for ids_file in "${gitlab_ids}" "${cms_ids}" "${shopping_ids}"; do
    test -s "${ids_file}"
  done
}

write_run_info() {
  "${python_bin}" - "${output_root}/run_info.json" \
    "${model}" "${model_base}" "${judge_model}" "${judge_base}" \
    "${gitlab_ids}" "${cms_ids}" "${shopping_ids}" <<'PY'
import json
import sys
from pathlib import Path

out, model, model_base, judge_model, judge_base, *id_paths = sys.argv[1:]
sites = ["gitlab", "shopping_admin", "shopping"]
counts = {
    site: len(Path(path).read_text().split())
    for site, path in zip(sites, id_paths)
}
data = {
    "model": model,
    "agent_endpoint": model_base,
    "isolation": "CUA-Sandbox DB/reflink with signed per-task routing",
    "official_webarena_root": "/path/to/webarena-main",
    "task_counts": counts,
    "evaluation": {
        "max_steps": 30,
        "max_tokens": 1000,
        "max_obs_length": 1920,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": -1,
        "prompt": "agent/prompts/jsons/p_cot_id_actree_2s.json",
        "action_set_tag": "id_accessibility_tree",
        "observation_type": "accessibility_tree",
        "judge_model": judge_model,
        "judge_endpoint": judge_base,
    },
}
Path(out).write_text(json.dumps(data, indent=2) + "\n")
print(json.dumps(data, indent=2))
PY
}

archive_unscored_artifacts() {
  local ids_file="$1"
  local result_dir="$2"
  local round="$3"
  "${python_bin}" - "${ids_file}" "${result_dir}" "${round}" <<'PY'
import re
import shutil
import sys
from pathlib import Path

ids_file = Path(sys.argv[1])
result_dir = Path(sys.argv[2])
round_number = sys.argv[3]
expected = {int(value) for value in ids_file.read_text().split()}
log_path = result_dir / "eval.log"
text = log_path.read_text(errors="replace") if log_path.exists() else ""
scored = {
    int(value)
    for value in re.findall(
        r"\[Result\] \((?:PASS|FAIL)\).*?/([0-9]+)\.json", text
    )
}
unscored = sorted(expected - scored)
attempt_dir = result_dir / "infra_attempts" / f"round_{round_number}"
attempt_dir.mkdir(parents=True, exist_ok=True)
for task_id in unscored:
    candidates = [
        result_dir / f"render_{task_id}.html",
        result_dir / "traces" / f"{task_id}.zip",
    ]
    for path in candidates:
        if path.exists():
            target = attempt_dir / path.name
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
(result_dir / "unscored_ids.txt").write_text(
    " ".join(map(str, unscored)) + ("\n" if unscored else "")
)
print(f"scored={len(scored)}/{len(expected)} unscored={len(unscored)}")
PY
}

count_scored() {
  local result_dir="$1"
  grep -Eho '\[Result\] \((PASS|FAIL)\).*[/][0-9]+\.json' \
    "${result_dir}/eval.log" 2>/dev/null \
    | sed -E 's#.*[/]([0-9]+)\.json#\1#' \
    | sort -nu \
    | wc -l
}

run_site() {
  local site="$1"
  local ids_file="$2"
  local launcher="$3"
  local result_dir="${output_root}/${site}"
  local expected scored round
  shift 3
  mkdir -p "${result_dir}/infra_attempts"
  cp "${ids_file}" "${result_dir}/task_ids.txt"
  expected="$(wc -w < "${ids_file}")"
  log "site=${site} starting expected=${expected}"

  for round in 1 2 3; do
    scored="$(count_scored "${result_dir}")"
    if [[ "${scored}" -ge "${expected}" ]]; then
      break
    fi
    log "site=${site} round=${round} scored_before=${scored}/${expected}"
    (
      cd "${project_root}"
      bash "${launcher}" "${ids_file}" "${result_dir}" \
        --model "${model}" \
        --temperature 1.0 \
        --top_p 0.95 \
        "$@" \
        --max_tokens 1000 \
        --max_steps 30 \
        --max_retry 1 \
        --max_obs_length 1920
    ) 2>&1 | tee -a "${result_dir}/eval.log" || true
    archive_unscored_artifacts "${ids_file}" "${result_dir}" "${round}"
  done

  scored="$(count_scored "${result_dir}")"
  log "site=${site} finished scored=${scored}/${expected}"
}

log "starting Gemma-4-31B CUA-Sandbox DB/reflink WebArena evaluation"
preflight
write_run_info

run_site gitlab "${gitlab_ids}" \
  "${project_root}/scripts/run_gitlab_official_db_eval.sh"
run_site shopping_admin "${cms_ids}" \
  "${project_root}/scripts/run_cms_official_db_eval.sh" --top_k -1
run_site shopping "${shopping_ids}" \
  "${project_root}/scripts/run_shopping_official_db_eval.sh" --top_k -1

log "all sites finished"
