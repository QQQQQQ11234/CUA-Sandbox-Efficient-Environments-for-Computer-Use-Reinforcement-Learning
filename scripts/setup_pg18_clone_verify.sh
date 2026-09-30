#!/usr/bin/env bash
set -euo pipefail

# One-shot bootstrap + verification for:
# PostgreSQL 18 + file_copy_method=clone + FILE_COPY database cloning.
#
# Default behavior:
# - Run PostgreSQL 18 in Docker
# - Store PGDATA under DATA_DIR
# - Verify reflink capability on DATA_DIR
# - Build a sizable template DB
# - Clone with CREATE DATABASE ... STRATEGY FILE_COPY
# - Print timing + disk delta + DSN for CUA-Sandbox
#
# Optional strict CoW mode (recommended):
# - REQUIRE_COW=true (default) will stop if DATA_DIR does not support reflink
# - AUTO_CREATE_LOOP_XFS=true can auto-create and mount an XFS(loop) volume (needs sudo)

CONTAINER_NAME="${CONTAINER_NAME:-pg18_clone_lab}"
PG_IMAGE="${PG_IMAGE:-postgres:18}"
PG_PORT="${PG_PORT:-55432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"

DATA_DIR="${DATA_DIR:-/var/lib/web-agent/pg18_clone_lab/data}"
REQUIRE_COW="${REQUIRE_COW:-true}"
AUTO_CREATE_LOOP_XFS="${AUTO_CREATE_LOOP_XFS:-false}"
LOOP_XFS_IMG="${LOOP_XFS_IMG:-/var/lib/web-agent/pg18_clone_lab.xfs.img}"
LOOP_XFS_MOUNT="${LOOP_XFS_MOUNT:-/var/lib/web-agent/pg18_clone_lab_xfs}"
LOOP_XFS_SIZE_GB="${LOOP_XFS_SIZE_GB:-80}"

TEMPLATE_DB="${TEMPLATE_DB:-base_template_db}"
CLONE_DB="${CLONE_DB:-agent_1_db}"
FILL_ROWS="${FILL_ROWS:-600000}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"

USE_SG_DOCKER=0

log() {
  echo "[INFO] $*"
}

warn() {
  echo "[WARN] $*" >&2
}

die() {
  echo "[ERROR] $*" >&2
  exit 1
}

as_bool() {
  local v="${1:-}"
  shopt -s nocasematch
  if [[ "$v" =~ ^(1|true|yes|y|on)$ ]]; then
    echo "true"
    shopt -u nocasematch
    return
  fi
  echo "false"
  shopt -u nocasematch
}

validate_identifier() {
  local name="$1"
  [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid SQL identifier: $name"
}

docker_cmd() {
  if [[ "$USE_SG_DOCKER" -eq 1 ]]; then
    local escaped=()
    local arg
    for arg in "$@"; do
      escaped+=("$(printf '%q' "$arg")")
    done
    sg docker -c "docker ${escaped[*]}"
  else
    docker "$@"
  fi
}

detect_docker_access() {
  if docker info >/dev/null 2>&1; then
    USE_SG_DOCKER=0
    return
  fi
  if sg docker -c "docker info >/dev/null 2>&1"; then
    USE_SG_DOCKER=1
    return
  fi
  die "docker daemon is not accessible. Add your user to docker group or use sudo-compatible setup."
}

fs_type_of_path() {
  local p="$1"
  local target="$p"
  if [[ ! -e "$target" ]]; then
    target="$(dirname "$target")"
  fi
  findmnt -T "$target" -o FSTYPE -n 2>/dev/null || echo "unknown"
}

supports_reflink() {
  local dir="$1"
  mkdir -p "$dir"
  local probe_dir="$dir/.reflink_probe_$$"
  mkdir -p "$probe_dir"
  dd if=/dev/zero of="$probe_dir/src.bin" bs=1M count=8 status=none
  if cp --reflink=always "$probe_dir/src.bin" "$probe_dir/dst.bin" >/dev/null 2>&1; then
    rm -rf "$probe_dir"
    return 0
  fi
  rm -rf "$probe_dir"
  return 1
}

create_loop_xfs_mount() {
  command -v sudo >/dev/null 2>&1 || die "sudo is required for AUTO_CREATE_LOOP_XFS=true"
  command -v mkfs.xfs >/dev/null 2>&1 || die "mkfs.xfs is required but not found"

  log "Creating/using loopback XFS at $LOOP_XFS_MOUNT (image: $LOOP_XFS_IMG)"

  sudo mkdir -p "$LOOP_XFS_MOUNT"
  if [[ ! -f "$LOOP_XFS_IMG" ]]; then
    log "Allocating loop image (${LOOP_XFS_SIZE_GB}G): $LOOP_XFS_IMG"
    sudo truncate -s "${LOOP_XFS_SIZE_GB}G" "$LOOP_XFS_IMG"
  fi

  if ! sudo blkid -p "$LOOP_XFS_IMG" >/dev/null 2>&1; then
    log "Formatting loop image as XFS with reflink=1"
    sudo mkfs.xfs -m reflink=1 -f "$LOOP_XFS_IMG"
  fi

  if ! findmnt -T "$LOOP_XFS_MOUNT" >/dev/null 2>&1; then
    log "Mounting loop image to $LOOP_XFS_MOUNT"
    sudo mount -o loop "$LOOP_XFS_IMG" "$LOOP_XFS_MOUNT"
  fi

  DATA_DIR="$LOOP_XFS_MOUNT/data"
  sudo mkdir -p "$DATA_DIR"
  sudo chmod 0777 "$DATA_DIR"
}

prepare_data_dir() {
  mkdir -p "$DATA_DIR"
  chmod 0777 "$DATA_DIR" || true

  local fstype
  fstype="$(fs_type_of_path "$DATA_DIR")"
  log "DATA_DIR=$DATA_DIR (fstype=$fstype)"

  if supports_reflink "$DATA_DIR"; then
    log "Reflink probe: PASS (cp --reflink=always works)"
    return
  fi

  warn "Reflink probe: FAIL on $DATA_DIR"
  if [[ "$(as_bool "$AUTO_CREATE_LOOP_XFS")" == "true" ]]; then
    create_loop_xfs_mount
    local newfstype
    newfstype="$(fs_type_of_path "$DATA_DIR")"
    log "Switched DATA_DIR to $DATA_DIR (fstype=$newfstype)"
    supports_reflink "$DATA_DIR" || die "reflink probe still failed after loop-XFS setup"
    return
  fi

  if [[ "$(as_bool "$REQUIRE_COW")" == "true" ]]; then
    die "Current DATA_DIR does not support reflink. Set AUTO_CREATE_LOOP_XFS=true or point DATA_DIR to XFS/Btrfs/ZFS."
  fi

  warn "Continuing without reflink support. file_copy_method=clone may degrade to copy behavior."
}

wait_pg_ready() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while ((SECONDS < deadline)); do
    if docker_cmd exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

psql_q() {
  local db="$1"
  local sql="$2"
  docker_cmd exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
    psql -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 -Atqc "$sql"
}

ms_now() {
  date +%s%3N
}

human_bytes() {
  local bytes="$1"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$bytes"
  else
    echo "${bytes}B"
  fi
}

start_pg_container() {
  log "Pulling image: $PG_IMAGE"
  docker_cmd pull "$PG_IMAGE" >/dev/null

  if docker_cmd ps -a --format "{{.Names}}" | grep -qx "$CONTAINER_NAME"; then
    log "Removing existing container: $CONTAINER_NAME"
    docker_cmd rm -f "$CONTAINER_NAME" >/dev/null
  fi

  log "Starting container: $CONTAINER_NAME on port $PG_PORT"
  docker_cmd run -d \
    --name "$CONTAINER_NAME" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -e PGDATA="/var/lib/postgresql/data/pgdata" \
    -p "${PG_PORT}:5432" \
    -v "${DATA_DIR}:/var/lib/postgresql/data" \
    "$PG_IMAGE" \
    -c file_copy_method=clone \
    -c log_min_messages=warning >/dev/null

  log "Waiting for PostgreSQL readiness..."
  wait_pg_ready || {
    docker_cmd logs "$CONTAINER_NAME" | tail -n 80 >&2 || true
    die "PostgreSQL did not become ready in ${WAIT_SECONDS}s"
  }
}

run_verification() {
  local server_version_num
  local server_version
  local file_copy_method
  local data_dir
  local base_oid
  local clone_oid
  local template_bytes
  local base_dir_before
  local base_dir_after
  local clone_ms
  local t0
  local t1

  server_version="$(psql_q "$POSTGRES_DB" "SHOW server_version;")"
  server_version_num="$(psql_q "$POSTGRES_DB" "SHOW server_version_num;")"
  file_copy_method="$(psql_q "$POSTGRES_DB" "SHOW file_copy_method;")"
  data_dir="$(psql_q "$POSTGRES_DB" "SHOW data_directory;")"

  log "server_version=$server_version"
  log "server_version_num=$server_version_num"
  log "file_copy_method=$file_copy_method"
  log "data_directory=$data_dir"

  if (( server_version_num < 180000 )); then
    die "Expected PostgreSQL 18+, got $server_version"
  fi

  if [[ "${file_copy_method,,}" != "clone" ]]; then
    die "file_copy_method is not clone (actual: $file_copy_method)"
  fi

  log "Preparing template database: $TEMPLATE_DB"
  psql_q "$POSTGRES_DB" "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$TEMPLATE_DB', '$CLONE_DB') AND pid <> pg_backend_pid();"
  psql_q "$POSTGRES_DB" "DROP DATABASE IF EXISTS \"$CLONE_DB\";"
  psql_q "$POSTGRES_DB" "DROP DATABASE IF EXISTS \"$TEMPLATE_DB\";"
  psql_q "$POSTGRES_DB" "CREATE DATABASE \"$TEMPLATE_DB\";"

  log "Generating sample data in $TEMPLATE_DB (rows=$FILL_ROWS)"
  psql_q "$TEMPLATE_DB" "CREATE TABLE IF NOT EXISTS big_data AS SELECT g AS id, repeat(md5(g::text), 8) AS payload FROM generate_series(1, $FILL_ROWS) AS g;"
  psql_q "$TEMPLATE_DB" "VACUUM ANALYZE big_data;"
  psql_q "$TEMPLATE_DB" "CHECKPOINT;"

  template_bytes="$(psql_q "$POSTGRES_DB" "SELECT pg_database_size('$TEMPLATE_DB');")"
  base_oid="$(psql_q "$POSTGRES_DB" "SELECT oid FROM pg_database WHERE datname='$TEMPLATE_DB';")"
  base_dir_before="$(docker_cmd exec "$CONTAINER_NAME" bash -lc "du -sb '$data_dir/base' | awk '{print \$1}'")"

  log "Cloning: CREATE DATABASE \"$CLONE_DB\" TEMPLATE \"$TEMPLATE_DB\" STRATEGY FILE_COPY"
  t0="$(ms_now)"
  psql_q "$POSTGRES_DB" "CREATE DATABASE \"$CLONE_DB\" TEMPLATE \"$TEMPLATE_DB\" STRATEGY FILE_COPY;"
  t1="$(ms_now)"
  clone_ms="$((t1 - t0))"

  clone_oid="$(psql_q "$POSTGRES_DB" "SELECT oid FROM pg_database WHERE datname='$CLONE_DB';")"
  base_dir_after="$(docker_cmd exec "$CONTAINER_NAME" bash -lc "du -sb '$data_dir/base' | awk '{print \$1}'")"

  local delta_bytes
  delta_bytes="$((base_dir_after - base_dir_before))"

  echo
  echo "================ Verification Result ================"
  echo "Container            : $CONTAINER_NAME"
  echo "PostgreSQL version   : $server_version"
  echo "file_copy_method     : $file_copy_method"
  echo "DATA_DIR             : $DATA_DIR"
  echo "Template DB          : $TEMPLATE_DB (oid=$base_oid)"
  echo "Clone DB             : $CLONE_DB (oid=$clone_oid)"
  echo "Template DB size     : $(human_bytes "$template_bytes")"
  echo "Clone elapsed time   : ${clone_ms} ms"
  echo "Disk delta (base dir): $(human_bytes "$delta_bytes")"
  echo "====================================================="
  echo

  if supports_reflink "$DATA_DIR"; then
    log "Reflink capability confirmed at DATA_DIR."
  else
    warn "Reflink capability not confirmed at DATA_DIR."
  fi

  cat <<EOF
Use this DSN for CUA-Sandbox:
  export DB_ADMIN_DSN='postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${PG_PORT}/${POSTGRES_DB}'
  export BASE_TEMPLATE_DB='${TEMPLATE_DB}'

Then run:
  ./scripts/run_db_isolation_eval.sh --task_ids 1 --agent_type tool
EOF
}

main() {
  validate_identifier "$TEMPLATE_DB"
  validate_identifier "$CLONE_DB"
  [[ "$FILL_ROWS" =~ ^[0-9]+$ ]] || die "FILL_ROWS must be integer"
  [[ "$PG_PORT" =~ ^[0-9]+$ ]] || die "PG_PORT must be integer"

  detect_docker_access
  prepare_data_dir
  start_pg_container
  run_verification
}

main "$@"
