#!/usr/bin/env bash
set -euo pipefail

site="${1:?usage: quick_validation.sh shopping|shopping_admin TASK_ID[,TASK_ID...]}"
tasks="${2:?usage: quick_validation.sh shopping|shopping_admin TASK_ID[,TASK_ID...]}"
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "${site}" in
  shopping|shopping_admin) ;;
  *) echo "site must be shopping or shopping_admin" >&2; exit 2 ;;
esac

cd "${project_root}"
python_bin="${WEB_AGENT_PYTHON:-${project_root}/.venv-gitlab-eval/bin/python}"
if [[ ! -x "${python_bin}" ]]; then
  python_bin="python3"
fi
"${python_bin}" scripts/check_shopping_db_isolation.py
"${python_bin}" scripts/validate_reward_consistency.py \
  --site "${site}" \
  --tasks "${tasks}" \
  --parallel 1 \
  --output-dir "validation_results/${site}_quick_$(date +%Y%m%d_%H%M%S)"
