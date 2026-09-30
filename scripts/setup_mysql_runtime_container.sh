#!/usr/bin/env bash
set -euo pipefail

site="${MAGENTO_SITE:-shopping}"
case "$site" in
  shopping)
    default_runtime_container="shopping-mysql-runtime"
    default_runtime_image="webarenaimages/shopping_final_0712:latest"
    ;;
  shopping_admin)
    default_runtime_container="shopping-admin-mysql-runtime"
    default_runtime_image="webarenaimages/shopping_admin_final_0719:latest"
    ;;
  *)
    echo "MAGENTO_SITE must be shopping or shopping_admin, got: $site" >&2
    exit 2
    ;;
esac
runtime_container="${MYSQL_RUNTIME_CONTAINER:-$default_runtime_container}"
runtime_image="${MYSQL_RUNTIME_IMAGE:-$default_runtime_image}"
xfs_base="${MYSQL_XFS_BASE_PATH:-/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs}"

if [[ ! -d "${xfs_base}" ]]; then
  echo "MySQL XFS base does not exist: ${xfs_base}" >&2
  exit 1
fi
if [[ "$(stat -f -c %T "${xfs_base}")" != "xfs" ]]; then
  echo "MySQL runtime must be stored on XFS: ${xfs_base}" >&2
  exit 1
fi

existing="$(docker inspect -f '{{.State.Running}}' "${runtime_container}" 2>/dev/null || true)"
if [[ "${existing}" == "true" ]]; then
  expected_image="$(docker image inspect "$runtime_image" --format '{{.Id}}')"
  actual_image="$(docker inspect "$runtime_container" --format '{{.Image}}')"
  if [[ "$actual_image" != "$expected_image" ]]; then
    echo "Shared MySQL runtime image mismatch for ${runtime_container}" >&2
    exit 1
  fi
  echo "Shared MySQL runtime already running: ${runtime_container}"
  exit 0
fi
if [[ -n "${existing}" ]]; then
  docker start "${runtime_container}" >/dev/null
else
  docker run -d \
    --name "${runtime_container}" \
    --network host \
    --restart unless-stopped \
    --entrypoint sleep \
    -v "${xfs_base}:${xfs_base}" \
    "${runtime_image}" infinity >/dev/null
fi

docker exec "${runtime_container}" sh -lc \
  'command -v mysqld >/dev/null && command -v mysqladmin >/dev/null'
echo "Shared MySQL runtime is ready: ${runtime_container}"
