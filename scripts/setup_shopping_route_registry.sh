#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container="${SHOPPING_ROUTE_REGISTRY_CONTAINER:-shopping-route-registry}"
image="${ROUTE_REGISTRY_IMAGE:-web-agent-route-registry:dev}"
secret="${WEB_AGENT_ROUTE_REGISTRY_SECRET:?WEB_AGENT_ROUTE_REGISTRY_SECRET is required}"
port="${SHOPPING_ROUTE_REGISTRY_PORT:-8766}"

mkdir -p "${project_root}/runtime"
docker build \
  -f "${project_root}/gitlab_shared/registry.Dockerfile" \
  -t "${image}" \
  "${project_root}" >/dev/null
if docker inspect "${container}" >/dev/null 2>&1; then
  docker rm -f "${container}" >/dev/null
fi
docker run -d \
  --name "${container}" \
  --user "$(id -u):$(id -g)" \
  --restart unless-stopped \
  -p "${port}:8765" \
  -e "WEB_AGENT_ROUTE_REGISTRY_SECRET=${secret}" \
  -v "${project_root}/runtime:/runtime" \
  "${image}" \
  --registry-path /runtime/shopping_db_routes.sqlite3 \
  --host 0.0.0.0 \
  --port 8765 >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null; then
    echo "Shopping route registry is ready on port ${port}"
    exit 0
  fi
  sleep 1
done
docker logs "${container}" >&2
exit 1
