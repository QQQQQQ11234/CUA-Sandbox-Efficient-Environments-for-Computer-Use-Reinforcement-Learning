#!/usr/bin/env bash
set -Eeuo pipefail

# Prepare CUA-Sandbox's runtime directory on an already-mounted XFS filesystem.
# This script never formats a disk and never creates a loopback image.
#
# Typical usage:
#   sudo WEBAGENT_OWNER=yanxin \
#     bash scripts/setup_webagent_xfs_dir.sh
#
# Optional environment variables:
#   NVME_MOUNT=/mnt/nvme-data
#   WEBAGENT_ROOT=/mnt/nvme-data/yanxin/web-agent-test
#   COMPAT_MOUNT=/var/lib/web-agent
#   WEBAGENT_OWNER=yanxin
#   MIN_FREE_GIB=400
#   BIND_MOUNT=auto       # auto, true, or false
#   LOG_PATH=/var/log/webagent_xfs_setup.log

NVME_MOUNT="${NVME_MOUNT:-/mnt/nvme-data}"
WEBAGENT_OWNER="${WEBAGENT_OWNER:-yanxin}"
WEBAGENT_ROOT="${WEBAGENT_ROOT:-${NVME_MOUNT}/${WEBAGENT_OWNER}/web-agent-test}"
COMPAT_MOUNT="${COMPAT_MOUNT:-/var/lib/web-agent}"
MIN_FREE_GIB="${MIN_FREE_GIB:-400}"
BIND_MOUNT="${BIND_MOUNT:-auto}"
LOG_PATH="${LOG_PATH:-/mnt/afs/home/${WEBAGENT_OWNER}/webagent_xfs_setup.log}"

if [[ ! "$MIN_FREE_GIB" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIN_FREE_GIB must be a positive integer, got: $MIN_FREE_GIB" >&2
  exit 2
fi

case "$BIND_MOUNT" in
  auto|true|false) ;;
  *)
    echo "BIND_MOUNT must be auto, true, or false, got: $BIND_MOUNT" >&2
    exit 2
    ;;
esac

for command_name in findmnt df stat xfs_info dd cp cmp awk mktemp; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
done

log_dir="$(dirname "$LOG_PATH")"
if ! mkdir -p "$log_dir" 2>/dev/null || ! touch "$LOG_PATH" 2>/dev/null; then
  LOG_PATH="/tmp/webagent_xfs_setup.log"
  echo "Warning: requested log path is not writable; using $LOG_PATH" >&2
fi

exec > >(tee -a "$LOG_PATH") 2>&1

log() {
  printf '[%s] %s\n' "$(date '+%F %T %Z')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

cleanup_test_dir=""
cleanup() {
  if [[ -n "$cleanup_test_dir" && -d "$cleanup_test_dir" ]]; then
    rm -rf -- "$cleanup_test_dir"
  fi
}
trap cleanup EXIT

log "Starting CUA-Sandbox XFS directory setup"
log "NVME_MOUNT=$NVME_MOUNT"
log "WEBAGENT_ROOT=$WEBAGENT_ROOT"
log "COMPAT_MOUNT=$COMPAT_MOUNT"
log "MIN_FREE_GIB=$MIN_FREE_GIB"
log "BIND_MOUNT=$BIND_MOUNT"
log "LOG_PATH=$LOG_PATH"

[[ -d "$NVME_MOUNT" ]] || die "NVMe mount does not exist: $NVME_MOUNT"

filesystem_type="$(findmnt -T "$NVME_MOUNT" -n -o FSTYPE)"
filesystem_source="$(findmnt -T "$NVME_MOUNT" -n -o SOURCE)"
filesystem_target="$(findmnt -T "$NVME_MOUNT" -n -o TARGET)"
log "Filesystem: source=$filesystem_source target=$filesystem_target type=$filesystem_type"

[[ "$filesystem_type" == "xfs" ]] || die "$NVME_MOUNT is not XFS (found $filesystem_type)"

xfs_details="$(xfs_info "$NVME_MOUNT" 2>&1)" || die "xfs_info failed for $NVME_MOUNT"
printf '%s\n' "$xfs_details"
grep -Eq 'reflink=1([[:space:]]|$)' <<<"$xfs_details" \
  || die "XFS reflink=1 is not enabled on $NVME_MOUNT"
grep -Eq 'ftype=1([[:space:]]|$)' <<<"$xfs_details" \
  || die "XFS ftype=1 is not enabled on $NVME_MOUNT"

available_bytes="$(df -B1 --output=avail "$NVME_MOUNT" | awk 'NR == 2 {print $1}')"
required_bytes="$((MIN_FREE_GIB * 1024 * 1024 * 1024))"
[[ "$available_bytes" =~ ^[0-9]+$ ]] || die "Could not determine available bytes"

available_gib="$((available_bytes / 1024 / 1024 / 1024))"
log "Available space: ${available_gib} GiB"
(( available_bytes >= required_bytes )) \
  || die "Need at least ${MIN_FREE_GIB} GiB free; only ${available_gib} GiB is available"

directories=(
  "$WEBAGENT_ROOT/pg18_clone_xfs/data"
  "$WEBAGENT_ROOT/pg18_clone_xfs/gitlab_non_db"
  "$WEBAGENT_ROOT/pg18_clone_xfs/mysql_clone_xfs/runtime"
  "$WEBAGENT_ROOT/pg18_clone_xfs/playwright/cua-sandbox"
  "$WEBAGENT_ROOT/work"
  "$WEBAGENT_ROOT/tmp"
)

log "Creating CUA-Sandbox directories"
mkdir -p -- "${directories[@]}"
chmod 0755 "$WEBAGENT_ROOT" "$WEBAGENT_ROOT/pg18_clone_xfs"

if [[ "$(id -u)" -eq 0 ]] && id "$WEBAGENT_OWNER" >/dev/null 2>&1; then
  owner_group="$(id -gn "$WEBAGENT_OWNER")"
  chown -R "$WEBAGENT_OWNER:$owner_group" "$WEBAGENT_ROOT"
  log "Ownership set to $WEBAGENT_OWNER:$owner_group"
else
  log "Ownership unchanged (run as root and ensure user $WEBAGENT_OWNER exists to set it)"
fi

actual_root_type="$(stat -f -c %T "$WEBAGENT_ROOT")"
[[ "$actual_root_type" == "xfs" ]] \
  || die "$WEBAGENT_ROOT unexpectedly resolves to $actual_root_type instead of XFS"

log "Running an actual reflink clone test"
cleanup_test_dir="$(mktemp -d "$WEBAGENT_ROOT/.reflink-test.XXXXXX")"
source_file="$cleanup_test_dir/source.bin"
clone_file="$cleanup_test_dir/clone.bin"

dd if=/dev/urandom of="$source_file" bs=1M count=64 status=none
cp --reflink=always --preserve=mode,timestamps "$source_file" "$clone_file"
cmp --silent "$source_file" "$clone_file" || die "Reflink clone content verification failed"
log "Reflink test passed: cp --reflink=always succeeded and contents match"

rm -rf -- "$cleanup_test_dir"
cleanup_test_dir=""

bind_is_required=false
if [[ "$BIND_MOUNT" == "true" ]]; then
  bind_is_required=true
fi

if [[ "$BIND_MOUNT" != "false" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    if [[ "$bind_is_required" == "true" ]]; then
      die "BIND_MOUNT=true requires root"
    fi
    log "Skipping bind mount because the script is not running as root"
  elif ! command -v mount >/dev/null 2>&1; then
    if [[ "$bind_is_required" == "true" ]]; then
      die "BIND_MOUNT=true but mount is unavailable"
    fi
    log "Skipping bind mount because mount is unavailable"
  else
    mkdir -p "$COMPAT_MOUNT"
    if findmnt -M "$COMPAT_MOUNT" >/dev/null 2>&1; then
      compat_type="$(findmnt -M "$COMPAT_MOUNT" -n -o FSTYPE)"
      if [[ "$compat_type" != "xfs" ]]; then
        die "$COMPAT_MOUNT is already a mount point with filesystem type $compat_type"
      fi
      log "$COMPAT_MOUNT is already mounted on XFS; leaving it unchanged"
    elif mount --bind "$WEBAGENT_ROOT" "$COMPAT_MOUNT"; then
      log "Bind mount created: $WEBAGENT_ROOT -> $COMPAT_MOUNT"
    elif [[ "$bind_is_required" == "true" ]]; then
      die "Bind mount failed"
    else
      log "Bind mount was not permitted; use WEBAGENT_ROOT directly in configuration"
    fi
  fi
fi

log "Final verification"
findmnt -T "$WEBAGENT_ROOT"
df -hT "$WEBAGENT_ROOT"

if findmnt -M "$COMPAT_MOUNT" >/dev/null 2>&1; then
  findmnt -T "$COMPAT_MOUNT"
fi

cat <<EOF

CUA-Sandbox XFS setup completed successfully.

Runtime root:
  $WEBAGENT_ROOT

Default CUA-Sandbox-compatible path:
  $COMPAT_MOUNT

Useful environment variables when the bind mount is unavailable:
  export MYSQL_XFS_BASE_PATH="$WEBAGENT_ROOT/pg18_clone_xfs/mysql_clone_xfs"
  export MYSQL_XFS_RUNTIME_ROOT="$WEBAGENT_ROOT/pg18_clone_xfs/mysql_clone_xfs/runtime"
  export PLAYWRIGHT_BROWSERS_PATH="$WEBAGENT_ROOT/pg18_clone_xfs/playwright/cua-sandbox"

Log:
  $LOG_PATH
EOF
