#!/usr/bin/env bash
set -euo pipefail

external_url="${WEB_AGENT_GITLAB_EXTERNAL_URL:-http://127.0.0.1:8023}"
external_host="${external_url#*://}"
external_host="${external_host%%:*}"

sed -i -E \
  "s|^external_url[[:space:]].*|external_url '${external_url}'|" \
  /etc/gitlab/gitlab.rb

# The populated WebArena image starts runit directly, without omnibus
# reconfigure. Patch the generated Rails configuration before Puma starts so
# links stay on the local trusted origin.
if [[ -f /var/opt/gitlab/gitlab-rails/etc/gitlab.yml ]]; then
  sed -i \
    -e "s|metis\.lti\.cs\.cmu\.edu|${external_host}|g" \
    /var/opt/gitlab/gitlab-rails/etc/gitlab.yml
fi

exec "$@"
