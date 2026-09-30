#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_IMAGE="${WEB_ARENA_GITLAB_IMAGE:-}"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-webarena-gitlab-shared:dev}"

if [[ -z "$BASE_IMAGE" ]]; then
  echo "[ERROR] Set WEB_ARENA_GITLAB_IMAGE to the exact image used by WebArena/CUA-Sandbox."
  exit 1
fi

docker build \
  --build-arg "WEB_ARENA_GITLAB_IMAGE=$BASE_IMAGE" \
  -t "$OUTPUT_IMAGE" \
  "$ROOT_DIR"

echo "[DONE] Built $OUTPUT_IMAGE"
echo "[NEXT] Inspect the image's GitLab/Rails version before installing the initializer."
