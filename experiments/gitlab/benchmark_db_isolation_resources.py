#!/usr/bin/env python3
"""Measure incremental resources of the container-free GitLab DB isolation.

The benchmark deliberately measures the shared service cgroups plus the
physical free-block delta on the PostgreSQL/non-DB XFS volume.  It does not
count the model or browser processes, matching the server-manager comparison
in the CUA-Sandbox paper as closely as this shared-service architecture permits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from rl_web_agent.isolation.db_isolation import DBIsolationManager


def _memory_cgroup(container: str) -> Path:
    pid = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.State.Pid}}", container], text=True
    ).strip()
    entries = [line.split(":", 2) for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()]
    memory_v1 = next((path for _, controllers, path in entries if "memory" in controllers.split(",")), "")
    if memory_v1:
        return Path("/sys/fs/cgroup/memory") / memory_v1.lstrip("/")
    unified = next((path for _, controllers, path in entries if not controllers), "")
    return Path("/sys/fs/cgroup/unified") / unified.lstrip("/")


def _memory_snapshot(containers: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for container in containers:
        root = _memory_cgroup(container)
        usage_path = root / "memory.current"
        if not usage_path.exists():
            usage_path = root / "memory.usage_in_bytes"
        result[f"{container}.current"] = int(usage_path.read_text())
        stats = {
            line.split()[0]: int(line.split()[1])
            for line in (root / "memory.stat").read_text().splitlines()
            if len(line.split()) == 2 and line.split()[1].isdigit()
        }
        result[f"{container}.anon"] = stats.get("anon", stats.get("total_rss", stats.get("rss", 0)))
        result[f"{container}.file"] = stats.get("file", stats.get("total_cache", stats.get("cache", 0)))
    return result


def _stable_memory(containers: list[str], samples: int = 3) -> dict[str, int]:
    values = [_memory_snapshot(containers) for _ in range(samples)]
    return {key: int(statistics.median(item[key] for item in values)) for key in values[0]}


def _used_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return (stat.f_blocks - stat.f_bfree) * stat.f_frsize


def _total_current(snapshot: dict[str, int]) -> int:
    return sum(value for key, value in snapshot.items() if key.endswith(".current"))


def _active_routes(path: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT agent_id, db_name, lifecycle_state FROM agent_routes ORDER BY agent_id"
        ).fetchall()
    finally:
        connection.close()


def _db_sizes(dsn: str, prefix: str) -> dict[str, int]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT datname, pg_database_size(datname) FROM pg_database "
                "WHERE datname LIKE %s ORDER BY datname",
                (prefix + "%",),
            )
            return {str(name): int(size) for name, size in cursor.fetchall()}


def _warm_route(session: Any, url: str) -> int:
    request = urllib.request.Request(url, headers=session.headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(256)
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="results/gitlab_db_isolation_resources.json")
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--gitlab-url", default="http://127.0.0.1:8023")
    parser.add_argument("--storage-path", default="/var/lib/web-agent/pg18_clone_xfs")
    parser.add_argument("--route-registry-path", default="runtime/db_routes_nondb.sqlite3")
    parser.add_argument("--runtime-config", default="runtime/gitlab_nondb_config.json")
    parser.add_argument("--admin-dsn", default="postgresql://postgres:postgres@127.0.0.1:55433/postgres")
    parser.add_argument("--template-db", default="gitlab_base_template")
    parser.add_argument("--state-runtime-root", default="runtime/non_db_state")
    parser.add_argument("--gitlab-container", default="shared-gitlab-nondb")
    parser.add_argument("--postgres-container", default="pg18-gitlab-nondb")
    parser.add_argument("--registry-container", default="web-agent-route-registry-nondb")
    args = parser.parse_args()

    route_path = str(Path(args.route_registry_path).resolve())
    existing_routes = _active_routes(route_path)
    if existing_routes:
        raise SystemExit(
            "refusing to benchmark with active/stale routes; clean them first: "
            + json.dumps(existing_routes)
        )

    runtime_config = json.loads(Path(args.runtime_config).read_text())
    config = OmegaConf.create(
        {
            "admin_dsn": args.admin_dsn,
            "admin_docker_container": "",
            "base_template_db": args.template_db,
            "clone_strategy": "FILE_COPY",
            "route_token_secret": runtime_config["route_secret"],
            "route_registry_path": route_path,
            "drop_on_close": True,
            "reset_on_setup": True,
            "state_audit": {"enabled": False},
            "shared_site_hosts": {"gitlab": "127.0.0.1:8023"},
            "non_db_state": {
                "enabled": True,
                "runtime_root": args.state_runtime_root,
                "worker_mode": "shared",
                "gitlab_state": {
                    "enabled": True,
                    "docker_container": args.gitlab_container,
                    "statectl_path": "/opt/web-agent/gitlab_statectl.rb",
                    "timeout_seconds": 300,
                },
            },
        }
    )
    logger = type(
        "Logger",
        (),
        {"info": print, "warning": print, "exception": print},
    )()
    manager = DBIsolationManager(config, logger)
    containers = [args.postgres_container, args.gitlab_container, args.registry_container]
    before_memory = _stable_memory(containers, args.samples)
    before_used = _used_bytes(args.storage_path)
    before_db_sizes = _db_sizes(args.admin_dsn, "agent_")
    sessions = []
    records = []
    previous_used = before_used
    previous_memory = before_memory
    try:
        for index in range(args.samples):
            env_uuid = f"resource_benchmark_{index}_{uuid.uuid4().hex[:8]}"
            started = time.perf_counter()
            session = manager.prepare_for_task(env_uuid, {"task_id": 418, "sites": ["gitlab"]})
            elapsed = time.perf_counter() - started
            status = _warm_route(session, args.gitlab_url + "/")
            sessions.append(session)
            memory = _stable_memory(containers, 2)
            used = _used_bytes(args.storage_path)
            records.append(
                {
                    "index": index,
                    "setup_seconds": round(elapsed, 4),
                    "http_status": status,
                    "storage_delta_bytes": used - before_used,
                    "storage_increment_bytes": used - previous_used,
                    "memory_delta_bytes": _total_current(memory) - _total_current(before_memory),
                    "memory_increment_bytes": _total_current(memory) - _total_current(previous_memory),
                    "memory_snapshot": memory,
                    "database_sizes_bytes": _db_sizes(args.admin_dsn, "agent_task_418_resource_benchmark_%"),
                }
            )
            previous_used = used
            previous_memory = memory
    finally:
        for session in reversed(sessions):
            manager.cleanup(session)

    after_memory = _stable_memory(containers, args.samples)
    after_used = _used_bytes(args.storage_path)
    output = {
        "benchmark": "gitlab_db_isolation",
        "clone_strategy": "FILE_COPY",
        "storage_path": args.storage_path,
        "storage_measurement": "statvfs used-block delta on shared XFS volume",
        "memory_measurement": "cgroup memory.current for shared GitLab/Postgres/registry services",
        "samples": records,
        "summary": {
            "episodes": len(records),
            "median_setup_seconds": statistics.median(r["setup_seconds"] for r in records) if records else None,
            "median_storage_per_episode_bytes": statistics.median(r["storage_increment_bytes"] for r in records) if records else None,
            "median_memory_per_episode_bytes": statistics.median(r["memory_increment_bytes"] for r in records) if records else None,
            "aggregate_storage_per_episode_bytes": records[-1]["storage_delta_bytes"] / len(records) if records else None,
            "aggregate_memory_per_episode_bytes": records[-1]["memory_delta_bytes"] / len(records) if records else None,
            "aggregate_storage_delta_bytes_after_cleanup": after_used - before_used,
            "aggregate_memory_delta_bytes_after_cleanup": _total_current(after_memory) - _total_current(before_memory),
        },
        "before": {"storage_used_bytes": before_used, "memory": before_memory, "agent_databases": before_db_sizes},
        "after": {"storage_used_bytes": after_used, "memory": after_memory, "agent_databases": _db_sizes(args.admin_dsn, "agent_task_418_resource_benchmark_%")},
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
