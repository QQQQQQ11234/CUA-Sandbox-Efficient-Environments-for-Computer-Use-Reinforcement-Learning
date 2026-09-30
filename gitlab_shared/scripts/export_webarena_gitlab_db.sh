#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${GITLAB_CONTAINER:-gitlab}"
OUTPUT="${OUTPUT_DUMP:-gitlab_webarena.dump}"
PG_DUMP_BIN="${GITLAB_PG_DUMP_BIN:-/opt/gitlab/embedded/bin/pg_dump}"
PG_SOCKET="${GITLAB_PG_SOCKET:-/var/opt/gitlab/postgresql}"
PG_USER="${GITLAB_PG_USER:-gitlab-psql}"
PG_OS_USER="${GITLAB_PG_OS_USER:-gitlab-psql}"
PG_DATABASE="${GITLAB_PG_DATABASE:-gitlabhq_production}"

restart_services() {
  docker exec "$CONTAINER" gitlab-ctl start puma >/dev/null 2>&1 || true
  docker exec "$CONTAINER" gitlab-ctl start sidekiq >/dev/null 2>&1 || true
  docker exec "$CONTAINER" gitlab-ctl start gitlab-workhorse >/dev/null 2>&1 || true
}
trap restart_services EXIT

echo "[INFO] Quiescing GitLab application services in $CONTAINER"
docker exec "$CONTAINER" gitlab-ctl stop puma
docker exec "$CONTAINER" gitlab-ctl stop sidekiq
docker exec "$CONTAINER" gitlab-ctl stop gitlab-workhorse

echo "[INFO] Exporting $PG_DATABASE to $OUTPUT"
docker exec --user "$PG_OS_USER" "$CONTAINER" "$PG_DUMP_BIN" \
  --host="$PG_SOCKET" \
  --username="$PG_USER" \
  --format=custom \
  --no-owner \
  --file=/tmp/web_agent_gitlab.dump \
  "$PG_DATABASE"
docker cp "$CONTAINER:/tmp/web_agent_gitlab.dump" "$OUTPUT"
docker exec "$CONTAINER" rm -f /tmp/web_agent_gitlab.dump

echo "[DONE] GitLab logical dump written to $OUTPUT"
