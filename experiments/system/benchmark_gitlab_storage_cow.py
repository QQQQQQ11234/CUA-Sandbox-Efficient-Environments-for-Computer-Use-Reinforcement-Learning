#!/usr/bin/env python3
"""GitLab PostgreSQL storage-level CoW clone benchmark.

The prepared PostgreSQL cluster and GitLab non-DB tree are snapshotted before
timing.  Each timed sandbox clone uses ``cp --reflink=always`` into a fresh
branch directory; the child PostgreSQL server is started only after timing for
validation and an independent-write check.  Docker uses a precommitted GitLab
image and times stopped-child ``docker create``.  This is deliberately a
storage-level CoW comparison, not PostgreSQL CREATE DATABASE TEMPLATE.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path("/var/lib/web-agent/pg18_clone_xfs")
LIVE_DATA = ROOT / "data"
LIVE_NON_DB = ROOT / "gitlab_non_db"
BENCH_ROOT = ROOT / "gitlab_storage_cow_benchmark"
IMAGE = "gitlab-populated-final-port8023:latest"


def run(argv: list[str], *, check: bool = True, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=timeout)


def docker_cp_reflink(source: Path, target: Path, *, source_subdir: str, target_subdir: str) -> None:
    target.mkdir(parents=True, exist_ok=True)
    source_root = source.resolve().relative_to(ROOT.resolve())
    target_root = target.resolve().relative_to(ROOT.resolve())
    run([
        "docker", "run", "--rm",
        # Mount the common XFS root once.  Separate bind mounts can make
        # reflink(2) appear cross-device even when their host paths share the
        # same filesystem.
        "-v", f"{ROOT}:/cow-root",
        "postgres:18", "bash", "-lc",
        f"mkdir -p /cow-root/{target_root}/{target_subdir}; "
        f"cp -a --reflink=always /cow-root/{source_root}/{source_subdir}/. "
        f"/cow-root/{target_root}/{target_subdir}/",
    ], timeout=1800)


def prepare_snapshot(run_id: str) -> Path:
    snapshot = BENCH_ROOT / ("snapshot-" + run_id)
    snapshot.mkdir(parents=True, exist_ok=True)
    docker_cp_reflink(LIVE_DATA, snapshot, source_subdir="pgdata", target_subdir="pgdata")
    docker_cp_reflink(LIVE_NON_DB, snapshot, source_subdir=".", target_subdir="gitlab_non_db")
    return snapshot


def start_pg(name: str, data_root: Path) -> None:
    run([
        "docker", "run", "--detach", "--name", name,
        "-e", "POSTGRES_PASSWORD=postgres",
        "-e", "PGDATA=/var/lib/postgresql/data/pgdata",
        "-v", f"{data_root}:/var/lib/postgresql/data",
        "postgres:18",
    ], timeout=120)
    for _ in range(60):
        probe = run(["docker", "exec", name, "pg_isready", "-U", "postgres"], check=False, timeout=20)
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"PostgreSQL child did not become ready: {name}")


def prepare_db_marker(snapshot: Path, marker: str) -> None:
    name = "gitlab-cow-prep-" + uuid.uuid4().hex[:8]
    try:
        start_pg(name, snapshot)
        run(["docker", "exec", name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-Atc",
             "DROP DATABASE IF EXISTS cow_probe;"], timeout=120)
        run(["docker", "exec", name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-Atc",
             "CREATE DATABASE cow_probe;"], timeout=120)
        run(["docker", "exec", name, "psql", "-U", "postgres", "-d", "cow_probe", "-v", "ON_ERROR_STOP=1", "-c",
             "CREATE TABLE clone_marker(value text); INSERT INTO clone_marker VALUES ('" + marker + "');"], timeout=120)
    finally:
        run(["docker", "rm", "-f", name], check=False, timeout=120)


def sandbox_clone(snapshot: Path, branch: Path, marker: str, iteration: int) -> dict[str, Any]:
    branch.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    docker_cp_reflink(snapshot, branch, source_subdir="pgdata", target_subdir="pgdata")
    docker_cp_reflink(snapshot, branch, source_subdir="gitlab_non_db", target_subdir="gitlab_non_db")
    elapsed = time.perf_counter() - started
    name = "gitlab-cow-child-" + uuid.uuid4().hex[:8]
    try:
        start_pg(name, branch)
        query = run(["docker", "exec", name, "psql", "-U", "postgres", "-d", "cow_probe", "-Atc",
                     "SELECT value FROM clone_marker LIMIT 1;"], timeout=120)
        valid = query.stdout.strip() == marker
        run(["docker", "exec", name, "psql", "-U", "postgres", "-d", "cow_probe", "-v", "ON_ERROR_STOP=1", "-c",
             f"INSERT INTO clone_marker VALUES ('child-{iteration}');"], timeout=120)
        return {"seconds": elapsed, "state_valid": valid, "independent_write": True, "mode": "storage-reflink-cow"}
    finally:
        run(["docker", "rm", "-f", name], check=False, timeout=120)


def docker_clone(token: str, image: str) -> dict[str, Any]:
    child = f"gitlab-cow-docker-child-{token}"
    started = time.perf_counter()
    run(["docker", "create", "--name", child, image], timeout=120)
    elapsed = time.perf_counter() - started
    try:
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", child]).stdout.strip()
        return {"seconds": elapsed, "state_valid": state == "false", "mode": "docker-image-cow"}
    finally:
        run(["docker", "rm", "-f", child], check=False, timeout=120)


def dist(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "mean": None, "min": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": ordered[(len(ordered) - 1) // 2],
        "mean": sum(ordered) / len(ordered),
        "min": min(ordered),
        "max": max(ordered),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    marker = "gitlab-storage-cow-" + run_id
    snapshot = prepare_snapshot(run_id)
    prepare_db_marker(snapshot, marker)
    image = f"gitlab-cow-snapshot-{run_id}:latest"
    source = f"gitlab-cow-image-source-{run_id}"
    ours: list[dict[str, Any]] = []
    docker: list[dict[str, Any]] = []
    try:
        # Snapshot preparation is outside the timed Docker operation.
        run(["docker", "create", "--name", source, IMAGE], timeout=120)
        run(["docker", "commit", source, image], timeout=900)
        for iteration in range(args.iterations):
            branch = BENCH_ROOT / f"branch-{run_id}-{iteration}"
            if branch.exists():
                shutil.rmtree(branch)
            ours.append(sandbox_clone(snapshot, branch, marker, iteration))
            shutil.rmtree(branch, ignore_errors=True)
            docker.append(docker_clone(uuid.uuid4().hex[:8], image))
    finally:
        run(["docker", "rm", "-f", source], check=False, timeout=120)
        run(["docker", "image", "rm", "--force", image], check=False, timeout=300)
    ours_dist = dist([x["seconds"] for x in ours])
    docker_dist = dist([x["seconds"] for x in docker])
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "semantics": "PostgreSQL data directory + GitLab non-DB tree cloned with storage-level reflink; child validation and first write are outside clone timing",
        "host": {"hostname": platform.node(), "platform": platform.platform()},
        "ours": {"samples": ours, "summary": ours_dist},
        "docker": {"samples": docker, "summary": docker_dist},
        "speedup_docker_over_ours": docker_dist["p50"] / ours_dist["p50"],
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
