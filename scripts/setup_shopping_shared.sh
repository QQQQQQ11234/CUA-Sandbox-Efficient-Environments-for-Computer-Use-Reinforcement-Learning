#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

site="${MAGENTO_SHARED_SITE:-}"
if [[ -z "$site" ]]; then
  case "${MAGENTO_SHARED_CONTAINER:-}" in
    *admin*) site="shopping_admin" ;;
    *) site="shopping" ;;
  esac
fi
case "$site" in
  shopping)
    default_container="shared-shopping"
    default_image="webarena-shopping-shared:dev"
    default_base_image="webarenaimages/shopping_final_0712:latest"
    default_port="7772"
    default_authority="172.17.0.1:7770"
    ;;
  shopping_admin)
    default_container="shared-shopping-admin"
    default_image="webarena-shopping-admin-shared:dev"
    default_base_image="webarenaimages/shopping_admin_final_0719:latest"
    default_port="7780"
    default_authority="127.0.0.1:7780"
    ;;
  *)
    echo "MAGENTO_SHARED_SITE must be shopping or shopping_admin, got: $site" >&2
    exit 2
    ;;
esac

container="${MAGENTO_SHARED_CONTAINER:-$default_container}"
image="${MAGENTO_SHARED_IMAGE:-$default_image}"
base_image="${MAGENTO_SHARED_BASE_IMAGE:-$default_base_image}"
xfs_base="${MYSQL_XFS_BASE_PATH:-/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs}"
state_runtime="${MAGENTO_STATE_RUNTIME_ROOT:-${xfs_base}/runtime/magento_state}"
state_container_root="${MAGENTO_STATE_CONTAINER_ROOT:-/var/www/magento2/.web-agent-state}"
route_secret="${ROUTE_TOKEN_SECRET:?ROUTE_TOKEN_SECRET is required}"
registry_secret="${WEB_AGENT_ROUTE_REGISTRY_SECRET:?WEB_AGENT_ROUTE_REGISTRY_SECRET is required}"
registry_url="${WEB_AGENT_ROUTE_REGISTRY_URL:-http://host.docker.internal:8766}"
port="${SHOPPING_SHARED_PORT:-$default_port}"
canonical_authority="${MAGENTO_CANONICAL_AUTHORITY:-$default_authority}"
shopping_authority="${MAGENTO_CANONICAL_AUTHORITY_SHOPPING:-172.17.0.1:7770}"
admin_authority="${MAGENTO_CANONICAL_AUTHORITY_SHOPPING_ADMIN:-127.0.0.1:7780}"
if [[ "$site" == "shopping" ]]; then
  shopping_authority="$canonical_authority"
else
  admin_authority="$canonical_authority"
fi
shopping_base_image="${MAGENTO_SHOPPING_BASE_IMAGE:-webarenaimages/shopping_final_0712:latest}"
admin_base_image="${MAGENTO_ADMIN_BASE_IMAGE:-webarenaimages/shopping_admin_final_0719:latest}"
shopping_crypt_key="$(docker run --rm --entrypoint php "$shopping_base_image" -r '$c=require "/var/www/magento2/app/etc/env.php"; echo $c["crypt"]["key"];')"
admin_crypt_key="$(docker run --rm --entrypoint php "$admin_base_image" -r '$c=require "/var/www/magento2/app/etc/env.php"; echo $c["crypt"]["key"];')"

test -f "${xfs_base}/magento_template/.web-agent-mysql-template-ready"
test -f "${xfs_base}/magento_template/.web-agent-magento-state-ready"
test -f "${xfs_base}/magento_admin_template/.web-agent-magento-state-ready"
curl -fsS "${WEB_AGENT_ROUTE_REGISTRY_HEALTH:-http://127.0.0.1:8766/health}" >/dev/null
mkdir -p "${state_runtime}"

docker build \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "WEB_AGENT_STATE_GID=$(id -g)" \
  -t "${image}" \
  "${project_root}/shopping_shared"
if docker inspect "${container}" >/dev/null 2>&1; then
  docker rm -f "${container}" >/dev/null
fi
docker run -d \
  --name "${container}" \
  --restart unless-stopped \
  --add-host host.docker.internal:host-gateway \
  --group-add "$(id -g)" \
  -p "${port}:80" \
  -e "WEB_AGENT_ROUTE_TOKEN_SECRET=${route_secret}" \
  -e "WEB_AGENT_ROUTE_REGISTRY_SECRET=${registry_secret}" \
  -e "WEB_AGENT_ROUTE_REGISTRY_URL=${registry_url}" \
  -e "WEB_AGENT_MAGENTO_CRYPT_KEY_SHOPPING=${shopping_crypt_key}" \
  -e "WEB_AGENT_MAGENTO_CRYPT_KEY_SHOPPING_ADMIN=${admin_crypt_key}" \
  -e "WEB_AGENT_CANONICAL_AUTHORITY_SHOPPING=${shopping_authority}" \
  -e "WEB_AGENT_CANONICAL_AUTHORITY_SHOPPING_ADMIN=${admin_authority}" \
  -e "WEB_AGENT_MAGENTO_SITE=${site}" \
  -e "WEB_AGENT_MAGENTO_STATE_ROOT=${state_container_root}" \
  -v "${state_runtime}:${state_container_root}" \
  "${image}" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "${container}" supervisorctl status php-fpm 2>/dev/null \
      | grep -q RUNNING; then
    runtime_lock="$(docker exec "${container}" sha256sum /var/www/magento2/composer.lock | awk '{print $1}')"
    source_lock="$(docker run --rm --entrypoint sha256sum "$base_image" /var/www/magento2/composer.lock | awk '{print $1}')"
    if [[ "$runtime_lock" != "$source_lock" ]]; then
      echo "Shared Magento code does not match ${base_image}" >&2
      exit 1
    fi
    echo "Shared Magento runtime is ready: ${container} (${site}, port ${port})"
    exit 0
  fi
  sleep 1
done
docker logs "${container}" >&2
echo "Shared Magento runtime failed to start" >&2
exit 1
