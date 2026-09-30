#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-${WEB_ARENA_GITLAB_IMAGE:-}}"
if [[ -z "$IMAGE" ]]; then
  echo "Usage: $0 <webarena-gitlab-image>"
  exit 1
fi

docker run --rm --entrypoint bash "$IMAGE" -lc '
  set -e
  echo "=== GitLab version ==="
  gitlab-rake gitlab:env:info 2>/dev/null || gitlab-ctl status || true
  echo "=== Ruby/Rails ==="
  ruby --version || true
  cd /opt/gitlab/embedded/service/gitlab-rails 2>/dev/null && bundle exec rails runner "puts Rails.version; puts ActiveRecord.version" || true
  echo "=== PostgreSQL ==="
  /opt/gitlab/embedded/bin/postgres --version 2>/dev/null || true
  echo "=== Rails paths ==="
  find /opt/gitlab/embedded/service/gitlab-rails/config -maxdepth 2 -type d 2>/dev/null | sort | head -n 30
'
