#!/usr/bin/env bash
set -euo pipefail

DB_ADMIN_DSN="${DB_ADMIN_DSN:-}"
DUMP_FILE="${DUMP_FILE:-gitlab_webarena.dump}"
SEED_DB="${SEED_DB:-gitlab_base_seed}"
TEMPLATE_DB="${TEMPLATE_DB:-gitlab_base_template}"
DB_OWNER="${DB_OWNER:-postgres}"

if [[ -z "$DB_ADMIN_DSN" ]]; then
  echo "[ERROR] DB_ADMIN_DSN is required"
  exit 1
fi
if [[ ! -f "$DUMP_FILE" ]]; then
  echo "[ERROR] Dump file not found: $DUMP_FILE"
  exit 1
fi
for identifier in "$SEED_DB" "$TEMPLATE_DB" "$DB_OWNER"; do
  if [[ ! "$identifier" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "[ERROR] Unsafe PostgreSQL identifier: $identifier"
    exit 1
  fi
done

drop_database() {
  local database="$1"
  psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 -v database="$database" <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = :'database' AND pid <> pg_backend_pid();
SELECT format('DROP DATABASE IF EXISTS %I', :'database') \gexec
SQL
}

drop_database "$TEMPLATE_DB"
drop_database "$SEED_DB"

psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 -v database="$SEED_DB" -v owner="$DB_OWNER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'database', :'owner') \gexec
SQL

echo "[INFO] Restoring GitLab dump into $SEED_DB"
pg_restore \
  --dbname="${DB_ADMIN_DSN%/*}/$SEED_DB" \
  --no-owner \
  --role="$DB_OWNER" \
  --exit-on-error \
  "$DUMP_FILE"

echo "[INFO] Creating PostgreSQL template $TEMPLATE_DB"
psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 -v seed="$SEED_DB" -v template="$TEMPLATE_DB" <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = :'seed' AND pid <> pg_backend_pid();
SELECT format(
  'CREATE DATABASE %I TEMPLATE %I STRATEGY FILE_COPY',
  :'template',
  :'seed'
) \gexec
SELECT format('ALTER DATABASE %I WITH ALLOW_CONNECTIONS = false', :'template') \gexec
SQL

echo "[DONE] Created $TEMPLATE_DB from $SEED_DB"
echo "[CHECK] Verify extensions, collation, schema migration version, and GitLab login before benchmarking."
