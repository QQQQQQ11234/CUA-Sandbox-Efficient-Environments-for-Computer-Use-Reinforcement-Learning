#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_IMAGE="${WEB_ARENA_GITLAB_IMAGE:-gitlab-populated-final-port8023:latest}"
GITLAB_IMAGE="${GITLAB_IMAGE:-webarena-gitlab-nondb:dev}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-web-agent-route-registry:dev}"
GITLAB_CONTAINER="${GITLAB_CONTAINER:-shared-gitlab-nondb}"
REGISTRY_CONTAINER="${REGISTRY_CONTAINER:-web-agent-route-registry-nondb}"
PG_CONTAINER="${PG_CONTAINER:-pg18-gitlab-nondb}"
PG_DATA_DIR="${PG_DATA_DIR:-/var/lib/web-agent/pg18_clone_xfs/data}"
STATE_ROOT="${STATE_ROOT:-/var/lib/web-agent/pg18_clone_xfs/gitlab_non_db}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-$PROJECT_ROOT/runtime/gitlab_nondb_config.json}"
REGISTRY_DB="${REGISTRY_DB:-$PROJECT_ROOT/runtime/db_routes_nondb.sqlite3}"

for name in "$GITLAB_CONTAINER" "$REGISTRY_CONTAINER" "$PG_CONTAINER"; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker rm -f "$name" >/dev/null
  fi
done

mkdir -p "$STATE_ROOT/app" "$STATE_ROOT/gitaly"
if ! chmod 0777 "$STATE_ROOT/app" "$STATE_ROOT/gitaly" 2>/dev/null; then
  docker run --rm \
    --entrypoint chmod \
    -v "$STATE_ROOT:/web-agent-state" \
    postgres:18 \
    0777 /web-agent-state/app /web-agent-state/gitaly
fi

probe="$STATE_ROOT/.reflink-probe"
truncate -s 1M "$probe"
cp --reflink=always "$probe" "$probe.copy"
rm -f "$probe" "$probe.copy"

docker build \
  --build-arg "WEB_ARENA_GITLAB_IMAGE=$BASE_IMAGE" \
  -t "$GITLAB_IMAGE" \
  "$PROJECT_ROOT/gitlab_shared"
docker build \
  -f "$PROJECT_ROOT/gitlab_shared/registry.Dockerfile" \
  -t "$REGISTRY_IMAGE" \
  "$PROJECT_ROOT"

# Gitaly finalizes quarantined objects with an atomic rename, so quarantine
# and routed repositories must share one filesystem. Seed the XFS-backed bind
# mount once; subsequent deployments retain the immutable WebArena base data.
if [[ ! -f "$STATE_ROOT/gitaly/.web-agent-storage-seeded" ]]; then
  docker run --rm \
    --entrypoint /bin/bash \
    -v "$STATE_ROOT/gitaly:/web-agent-gitaly-target" \
    "$GITLAB_IMAGE" \
    -lc 'cp -a /var/opt/gitlab/git-data/repositories/. /web-agent-gitaly-target/ && touch /web-agent-gitaly-target/.web-agent-storage-seeded'
fi

docker run -d \
  --name "$PG_CONTAINER" \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=postgres \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -p 55433:5432 \
  -v "$PG_DATA_DIR:/var/lib/postgresql/data" \
  postgres:18 \
  -c file_copy_method=clone \
  -c log_min_messages=warning >/dev/null

for _ in $(seq 1 120); do
  if docker exec "$PG_CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U postgres >/dev/null

rm -f "$REGISTRY_DB" "$REGISTRY_DB-shm" "$REGISTRY_DB-wal"
docker run -d \
  --name "$REGISTRY_CONTAINER" \
  --user "$(id -u):$(id -g)" \
  -p 8765:8765 \
  -v "$PROJECT_ROOT/runtime:/runtime" \
  "$REGISTRY_IMAGE" \
  --registry-path /runtime/$(basename "$REGISTRY_DB") \
  --config-path /runtime/$(basename "$RUNTIME_CONFIG") \
  --host 0.0.0.0 --port 8765 >/dev/null

docker run -d \
  --name "$GITLAB_CONTAINER" \
  --add-host host.docker.internal:host-gateway \
  -p 8023:8023 \
  -v "$RUNTIME_CONFIG:/etc/web-agent/config.json:ro" \
  -v "$STATE_ROOT/app:/var/opt/gitlab/web-agent-state" \
  -v "$STATE_ROOT/gitaly:/var/opt/gitlab/git-data/repositories" \
  "$GITLAB_IMAGE" \
  /opt/web-agent/configure_local_url.sh \
  /opt/gitlab/embedded/bin/runsvdir-start >/dev/null

for _ in $(seq 1 180); do
  if curl --fail --silent http://127.0.0.1:8765/health >/dev/null &&
     [[ "$(curl --silent --output /dev/null --max-time 2 --write-out '%{http_code}' http://127.0.0.1:8023/-/health)" == "200" ]]; then
    echo "GitLab non-DB sandbox is ready on http://127.0.0.1:8023"
    exit 0
  fi
  sleep 2
done

docker logs "$GITLAB_CONTAINER" | tail -n 100 >&2 || true
exit 1
