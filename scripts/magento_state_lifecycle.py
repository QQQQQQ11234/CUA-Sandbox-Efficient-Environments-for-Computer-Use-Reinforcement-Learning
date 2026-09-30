#!/usr/bin/env python3
"""Magento queue/search lifecycle hook for Shopping DB isolation.

Invoked by ``CommandHookBackend``. Browser traffic remains frozen while this
trusted maintenance command resolves the staged route and rebuilds/deletes the
branch-local search index.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _issue_route_token(secret: str, agent_id: str, ttl_seconds: int = 3600) -> str:
    if len(secret.encode()) < 32:
        raise ValueError("ROUTE_TOKEN_SECRET must contain at least 32 bytes")
    payload = json.dumps(
        {"agent_id": agent_id, "exp": int(time.time()) + ttl_seconds, "v": 1},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    return f"{encode(payload)}.{encode(signature)}"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _docker_exec(container: str, token: str, argv: list[str], timeout: float) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"WEB_AGENT_ROUTE_TOKEN={token}",
            "-e",
            "WEB_AGENT_ROUTE_MAINTENANCE=1",
            container,
            *argv,
        ],
        check=True,
        timeout=timeout,
    )


def _assert_workers_disabled(container: str, timeout: float) -> None:
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", "ps auxww"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    forbidden = ("queue:consumers:start", "bin/magento cron:run")
    running = [marker for marker in forbidden if marker in result.stdout]
    if running:
        raise RuntimeError(
            f"unscoped Magento background workers are running: {running}"
        )


def _reindex(container: str, token: str, timeout: float) -> None:
    _docker_exec(
        container,
        token,
        [
            "php",
            "/var/www/magento2/bin/magento",
            "indexer:reindex",
            "catalogsearch_fulltext",
        ],
        timeout,
    )


def _curl_json(
    container: str,
    method: str,
    path: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "exec",
        container,
        "curl",
        "--silent",
        "--fail-with-body",
        "--show-error",
        "--max-time",
        str(max(1, int(timeout))),
        "-X",
        method,
        "http://127.0.0.1:9200" + path,
    ]
    if payload is not None:
        command.extend(
            [
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                json.dumps(payload, separators=(",", ":")),
            ]
        )
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout + 5,
    )


def _index_exists(container: str, index: str, timeout: float) -> bool:
    result = _curl_json(
        container, "GET", f"/{index}", timeout, check=False
    )
    return result.returncode == 0


def _search_template(site: str) -> str:
    return f"webagent_template_{site}_product_1_v1"


def ensure_search_template(
    container: str, site: str, timeout: float, *, force: bool = False
) -> str:
    """Create one immutable Lucene template from the populated image index."""
    template = _search_template(site)
    if _index_exists(container, template, timeout) and not force:
        return template
    if force and _index_exists(container, template, timeout):
        _curl_json(container, "DELETE", f"/{template}", timeout)
    alias_result = _curl_json(
        container, "GET", "/_alias/magento2_product_1", timeout
    )
    aliases = json.loads(alias_result.stdout)
    if len(aliases) != 1:
        raise RuntimeError(
            "expected exactly one populated magento2_product_1 index; "
            f"found {sorted(aliases)}"
        )
    source = next(iter(aliases))
    _curl_json(
        container,
        "PUT",
        f"/{source}/_settings",
        timeout,
        {"index.blocks.write": True},
    )
    try:
        _curl_json(
            container,
            "POST",
            f"/{source}/_clone/{template}?wait_for_active_shards=1",
            timeout,
            {
                "settings": {
                    "index.blocks.write": True,
                    "index.number_of_replicas": 0,
                }
            },
        )
    finally:
        _curl_json(
            container,
            "PUT",
            f"/{source}/_settings",
            timeout,
            {"index.blocks.write": False},
            check=False,
        )
    return template


def _restore_search_index(
    container: str, site: str, index_prefix: str, timeout: float
) -> bool:
    template = _search_template(site)
    if not _index_exists(container, template, timeout):
        return False
    _delete_index(container, index_prefix, timeout)
    target = f"{index_prefix}_product_1_v1"
    alias = f"{index_prefix}_product_1"
    _curl_json(
        container,
        "POST",
        f"/{template}/_clone/{target}?wait_for_active_shards=1",
        timeout,
        {
            "settings": {
                "index.blocks.write": False,
                "index.number_of_replicas": 0,
            },
            "aliases": {alias: {}},
        },
    )
    return True


def magento_cache_prefix(
    environment_id: str, site: str, branch_id: str, generation: int
) -> str:
    environment_cache_id = hashlib.sha256(
        f"{environment_id}:{site}".encode()
    ).hexdigest()[:16]
    branch_cache_id = hashlib.sha256(branch_id.encode()).hexdigest()[:8]
    return f"wa_{environment_cache_id}_{branch_cache_id}_g{generation}_"


def cache_template_prefix(site: str) -> str:
    return f"wa_template_{site}_"


_REDIS_CLONE_LUA = r"""
local source = ARGV[1]
local target = ARGV[2]
local source_state = ARGV[3]
local target_state = ARGV[4]
local function replace(value)
  if not value then return value end
  local replaced = string.gsub(value, source, target)
  if source_state ~= '' then
    replaced = string.gsub(replaced, source_state, target_state)
  end
  return replaced
end
for _, key in ipairs(redis.call('KEYS', 'zc:*' .. target .. '*')) do
  redis.call('DEL', key)
end
local copied = 0
for _, key in ipairs(redis.call('KEYS', 'zc:*' .. source .. '*')) do
  local destination = replace(key)
  local kind = redis.call('TYPE', key)['ok']
  if kind == 'string' then
    redis.call('SET', destination, replace(redis.call('GET', key)))
  elseif kind == 'hash' then
    local entries = redis.call('HGETALL', key)
    for index = 1, #entries, 2 do
      redis.call('HSET', destination, replace(entries[index]), replace(entries[index + 1]))
    end
  elseif kind == 'set' then
    for _, member in ipairs(redis.call('SMEMBERS', key)) do
      redis.call('SADD', destination, replace(member))
    end
  elseif kind == 'zset' then
    local entries = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
    for index = 1, #entries, 2 do
      redis.call('ZADD', destination, entries[index + 1], replace(entries[index]))
    end
  elseif kind == 'list' then
    for _, member in ipairs(redis.call('LRANGE', key, 0, -1)) do
      redis.call('RPUSH', destination, replace(member))
    end
  end
  local ttl = redis.call('PTTL', key)
  if ttl > 0 then redis.call('PEXPIRE', destination, ttl) end
  copied = copied + 1
end
if redis.call('TYPE', 'zc:tags')['ok'] == 'set' then
  for _, member in ipairs(redis.call('SMEMBERS', 'zc:tags')) do
    if string.find(member, target, 1, true) then
      redis.call('SREM', 'zc:tags', member)
    end
  end
  for _, member in ipairs(redis.call('SMEMBERS', 'zc:tags')) do
    if string.find(member, source, 1, true) then
      redis.call('SADD', 'zc:tags', replace(member))
    end
  end
end
return copied
"""


def clone_redis_cache(
    container: str,
    source_prefix: str,
    target_prefix: str,
    timeout: float,
    *,
    source_state: str = "",
    target_state: str = "",
) -> int:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "redis-cli",
            "--raw",
            "EVAL",
            _REDIS_CLONE_LUA,
            "0",
            source_prefix,
            target_prefix,
            source_state,
            target_state,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return int(result.stdout.strip())


def _restore_cache(
    container: str, identity: dict[str, str | int], timeout: float
) -> bool:
    target = magento_cache_prefix(
        str(identity["environment_id"]),
        str(identity["site"]),
        str(identity["branch_id"]),
        int(identity["generation"]),
    )
    copied = clone_redis_cache(
        container,
        cache_template_prefix(str(identity["site"])),
        target,
        timeout,
        source_state=f"webagent_template_{identity['site']}",
        target_state=str(identity["search_index"]),
    )
    return copied > 0


def _delete_index(container: str, index: str, timeout: float) -> None:
    if not index.startswith("webagent_"):
        raise ValueError(f"refusing to delete non-WebAgent search prefix: {index!r}")
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "-X",
            "DELETE",
            f"http://127.0.0.1:9200/{index}_*",
        ],
        check=False,  # HTTP 404 means there was no derived index to remove.
        timeout=timeout,
    )


def _delete_redis_cache(container: str, environment_id: str, site: str, timeout: float) -> None:
    environment_cache_id = hashlib.sha256(
        f"{environment_id}:{site}".encode()
    ).hexdigest()[:16]
    prefix = f"wa_{environment_cache_id}_"
    script = r"""
local prefix = ARGV[1]
local removed = 0
for _, key in ipairs(redis.call('KEYS', 'zc:*' .. prefix .. '*')) do
  redis.call('UNLINK', key)
  removed = removed + 1
end
if redis.call('TYPE', 'zc:tags')['ok'] == 'set' then
  for _, member in ipairs(redis.call('SMEMBERS', 'zc:tags')) do
    if string.find(member, prefix, 1, true) then
      redis.call('SREM', 'zc:tags', member)
    end
  end
end
return removed
"""
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "redis-cli",
            "--raw",
            "EVAL",
            script,
            "0",
            prefix,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("prepare", "quiesce", "checkpoint", "fork", "reset", "activate", "cleanup"),
    )
    parser.add_argument(
        "--container",
        default=os.environ.get("MAGENTO_SHARED_CONTAINER", "shared-shopping"),
    )
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()

    site = _required("WEB_AGENT_SITE")
    if site not in {"shopping", "shopping_admin"}:
        return
    agent_id = _required("WEB_AGENT_ENVIRONMENT_ID")
    route_secret = _required("ROUTE_TOKEN_SECRET")
    token = _issue_route_token(route_secret, agent_id)
    identity: dict[str, str | int] = {
        "environment_id": agent_id,
        "site": site,
        "branch_id": _required("WEB_AGENT_BRANCH_ID"),
        "generation": int(_required("WEB_AGENT_GENERATION")),
        "search_index": _required("WEB_AGENT_SEARCH_INDEX"),
    }

    if args.phase == "quiesce":
        _assert_workers_disabled(args.container, args.timeout)
    elif args.phase in {"prepare", "fork", "reset"}:
        _assert_workers_disabled(args.container, args.timeout)
        # The first isolated environment may run before an administrator has
        # materialized the immutable OpenSearch template. Building it clones
        # the populated image index and does not require the staged MySQL
        # branch to be active. Without this step the historical fallback tries
        # Magento reindex while its frozen DB route is still cold.
        ensure_search_template(args.container, site, args.timeout)
        if args.phase in {"prepare", "reset"}:
            _delete_redis_cache(
                args.container, agent_id, site, args.timeout
            )
        if not _restore_search_index(
            args.container,
            site,
            str(identity["search_index"]),
            args.timeout,
        ):
            # Compatibility fallback while a deployment is being upgraded.
            # Once the immutable populated-image template exists, prepare and
            # reset use Elasticsearch segment cloning instead of reindexing.
            _reindex(args.container, token, args.timeout)
        copied = _restore_cache(args.container, identity, args.timeout)
        if not copied:
            print(
                f"Magento cache template for {site} is not initialized; "
                "the first request will warm an isolated empty namespace",
                flush=True,
            )
    elif args.phase == "cleanup":
        _delete_index(
            args.container,
            _required("WEB_AGENT_SEARCH_INDEX"),
            args.timeout,
        )
        _delete_redis_cache(args.container, agent_id, site, args.timeout)


if __name__ == "__main__":
    main()
