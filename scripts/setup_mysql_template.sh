#!/usr/bin/env bash
set -euo pipefail

# Build a quiesced Magento state template from the currently populated
# WebArena Shopping container. The one-time setup container is not part of the
# per-agent runtime.

xfs_base="${MYSQL_XFS_BASE_PATH:-/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs}"
source_container="${MAGENTO_SOURCE_CONTAINER:-shopping}"
site="${MAGENTO_SITE:-}"
if [[ -z "$site" ]]; then
  case "$source_container" in
    *admin*) site="shopping_admin" ;;
    *) site="shopping" ;;
  esac
fi
if [[ "$site" == "shopping_admin" ]]; then
  default_template_name="magento_admin_template"
  default_copy_media="1"
  default_runtime_image="webarenaimages/shopping_admin_final_0719:latest"
  default_authority="127.0.0.1:7780"
elif [[ "$site" == "shopping" ]]; then
  default_template_name="magento_template"
  default_copy_media="0"
  default_runtime_image="webarenaimages/shopping_final_0712:latest"
  default_authority="127.0.0.1:7770"
else
  echo "MAGENTO_SITE must be shopping or shopping_admin, got: $site" >&2
  exit 2
fi
template_name="${MYSQL_TEMPLATE_NAME:-${default_template_name}}"
copy_media="${COPY_MAGENTO_MEDIA:-${default_copy_media}}"
runtime_image="${MYSQL_RUNTIME_IMAGE:-$default_runtime_image}"
expected_authority="${MAGENTO_CANONICAL_AUTHORITY:-$default_authority}"
database="${MYSQL_DATABASE:-magentodb}"
source_user="${MAGENTO_SOURCE_DB_USER:-magentouser}"
source_password="${MAGENTO_SOURCE_DB_PASSWORD:-MyPassword}"
magento_user="${MAGENTO_DB_USER:-magentouser}"
magento_password="${MAGENTO_DB_PASSWORD:-MyPassword}"
force="${FORCE:-0}"

target="${xfs_base}/${template_name}"
stage="${xfs_base}/.template-stage-$$"
setup_container="web-agent-mysql-template-$$"

cleanup() {
  docker rm -f "${setup_container}" >/dev/null 2>&1 || true
  if [[ "${stage}" == "${xfs_base}/.template-stage-"* ]]; then
    docker run --rm \
      --entrypoint rm \
      -v "${xfs_base}:/web-agent-xfs" \
      "${runtime_image}" \
      -rf "/web-agent-xfs/$(basename "${stage}")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for command in docker stat cp; do
  command -v "${command}" >/dev/null || {
    echo "Required command is missing: ${command}" >&2
    exit 1
  }
done
docker inspect "${source_container}" >/dev/null
source_base_urls="$(
  docker exec -e "MYSQL_PWD=${source_password}" "${source_container}" \
    mysql --batch --skip-column-names --user="${source_user}" "${database}" \
    -e "SELECT value FROM core_config_data WHERE path IN ('web/unsecure/base_url','web/secure/base_url') ORDER BY path,scope,scope_id;"
)"
if [[ -z "$source_base_urls" ]]; then
  echo "Source Magento database has no configured base URL" >&2
  exit 1
fi
while IFS= read -r base_url; do
  base_authority="${base_url#*://}"
  base_authority="${base_authority%%/*}"
  if [[ "$base_authority" != "$expected_authority" ]]; then
    echo "Source Magento base URL authority mismatch: expected ${expected_authority}, got ${base_authority}" >&2
    exit 1
  fi
done <<< "$source_base_urls"
if [[ ! -d "${xfs_base}" ]]; then
  echo "Create the XFS base first: ${xfs_base}" >&2
  exit 1
fi
if [[ "$(stat -f -c %T "${xfs_base}")" != "xfs" ]]; then
  echo "Template base must be XFS: ${xfs_base}" >&2
  exit 1
fi

probe_source="${xfs_base}/.reflink-probe-$$"
probe_target="${xfs_base}/.reflink-probe-copy-$$"
printf probe > "${probe_source}"
if ! cp --reflink=always "${probe_source}" "${probe_target}"; then
  rm -f -- "${probe_source}" "${probe_target}"
  echo "XFS reflink is unavailable at ${xfs_base}" >&2
  exit 1
fi
rm -f -- "${probe_source}" "${probe_target}"

if [[ -e "${target}" && "${force}" != "1" ]]; then
  echo "Template already exists: ${target}; set FORCE=1 to replace it" >&2
  exit 1
fi

mkdir -p "${stage}/mysql" "${stage}/media" "${stage}/sessions"
dump_path="${stage}/magentodb.sql"

echo "Exporting a transaction-consistent Magento database from ${source_container}"
docker exec \
  -e "MYSQL_PWD=${source_password}" \
  "${source_container}" \
  mysqldump \
  --user="${source_user}" \
  --single-transaction \
  --quick \
  --triggers \
  --hex-blob \
  --default-character-set=utf8mb4 \
  "${database}" > "${dump_path}"

echo "Initializing a matching MariaDB 10.6 datadir"
docker run --rm \
  --entrypoint mariadb-install-db \
  -v "${stage}/mysql:/var/lib/mysql" \
  "${runtime_image}" \
  --user=mysql \
  --datadir=/var/lib/mysql >/dev/null

docker run -d \
  --name "${setup_container}" \
  --entrypoint mariadbd \
  -v "${stage}/mysql:/var/lib/mysql" \
  "${runtime_image}" \
  --user=mysql \
  --datadir=/var/lib/mysql \
  --socket=/tmp/web-agent-template.sock \
  --skip-networking >/dev/null

ready=0
for _ in $(seq 1 120); do
  if docker exec "${setup_container}" mysqladmin \
    --socket=/tmp/web-agent-template.sock --user=root ping >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" != "1" ]]; then
  docker logs "${setup_container}" >&2
  echo "Template MariaDB did not become ready" >&2
  exit 1
fi

docker exec "${setup_container}" mysql \
  --socket=/tmp/web-agent-template.sock --user=root \
  -e "CREATE DATABASE \`${database}\` CHARACTER SET utf8mb4; CREATE USER IF NOT EXISTS '${magento_user}'@'%' IDENTIFIED BY '${magento_password}'; GRANT ALL ON \`${database}\`.* TO '${magento_user}'@'%'; FLUSH PRIVILEGES;"
docker exec -i "${setup_container}" mysql \
  --socket=/tmp/web-agent-template.sock --user=root "${database}" < "${dump_path}"
# Docker reports the exec session as 137 when the requested server shutdown
# terminates that session together with PID 1. The container exit code checked
# by docker wait below is the authoritative clean-shutdown result.
docker exec "${setup_container}" mysqladmin \
  --socket=/tmp/web-agent-template.sock --user=root shutdown || true
container_exit="$(docker wait "${setup_container}")"
if [[ "${container_exit}" != "0" ]]; then
  docker logs "${setup_container}" >&2
  echo "Template MariaDB exited with status ${container_exit}" >&2
  exit 1
fi
docker rm "${setup_container}" >/dev/null

# Keep the exact image's MariaDB uid as owner while
# granting the host CUA-Sandbox group access needed for reflink cloning and
# lifecycle cleanup. setgid makes newly created database files inherit it.
host_group="$(id -g)"
mysql_uid="$(docker run --rm --entrypoint id "${runtime_image}" -u mysql)"
docker run --rm \
  --entrypoint sh \
  -v "${stage}:/web-agent-template" \
  "${runtime_image}" \
  -c "chown -R ${mysql_uid}:${host_group} /web-agent-template/mysql && chmod -R g+rwX /web-agent-template/mysql && find /web-agent-template/mysql -type d -exec chmod g+s {} +"

echo "Copying authenticated baseline sessions"
if [[ "${copy_media}" == "1" ]]; then
  echo "Copying Magento media baseline for ${source_container}"
  docker cp "${source_container}:/var/www/magento2/pub/media/." "${stage}/media/"
else
  echo "Using shared immutable storefront media with an empty branch delta"
fi
docker cp "${source_container}:/var/www/magento2/var/session/." "${stage}/sessions/"
rm -f -- "${dump_path}"

printf '%s\n' 'WebAgent MariaDB template; clean shutdown after logical import' \
  > "${stage}/.web-agent-mysql-template-ready"
printf '%s\n' 'WebAgent Magento media/session template' \
  > "${stage}/.web-agent-magento-state-ready"
docker image inspect "${runtime_image}" --format '{{.Id}}' \
  > "${stage}/runtime-image-id"
printf '%s\n' "$site" > "${stage}/magento-site"
printf '%s\n' "$expected_authority" > "${stage}/canonical-authority"
docker exec "${source_container}" sha256sum /var/www/magento2/composer.lock \
  | awk '{print $1}' > "${stage}/magento-composer-lock-sha256"

test_clone="${xfs_base}/.template-reflink-test-$$"
cp -a --reflink=always "${stage}" "${test_clone}"
rm -rf -- "${test_clone}"

if [[ -e "${target}" ]]; then
  backup="${target}.backup.$(date +%Y%m%d%H%M%S)"
  mv "${target}" "${backup}"
  echo "Previous template preserved at ${backup}"
fi
mv "${stage}" "${target}"
trap - EXIT

echo "Template ready at ${target}"
du -sh "${target}"
