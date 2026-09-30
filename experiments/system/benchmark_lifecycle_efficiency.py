#!/usr/bin/env python3
"""Measure create/reset/clone/restore/fork for DB isolation and Docker.

The timed interval always ends after a readiness probe and a state assertion.
Preparation of the source environment or checkpoint is deliberately outside
the interval for reset, restore, and fork so those operations remain distinct.
Docker has no native WebArena checkpoint API; clone/restore/fork are explicitly
reported as docker-commit emulations.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from experiments.system.benchmark_environment_metrics import (
    SITES,
    SiteSpec,
    atomic_json,
    available_memory_bytes,
    distribution,
    docker_sizes,
    docker_url_headers,
    http_probe,
    ours_manager,
    ours_probe,
    ours_task,
    remove_docker,
    run_command,
    start_docker,
    used_bytes,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ("create", "reset", "clone", "restore", "fork")
MARKER_TABLE = "web_agent_lifecycle_probe"


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def docker_root() -> str:
    return run_command(
        ["docker", "info", "--format", "{{.DockerRootDir}}"]
    ).stdout.strip()


def background_snapshot() -> dict[str, Any]:
    """Capture concurrent evaluation/serving work that can perturb results."""
    process = run_command(
        [
            "bash",
            "-lc",
            "ps -eo pid,etime,pcpu,pmem,rss,cmd | "
            "grep -E 'run\\.py|run_official_|vllm serve|transformers serve|"
            "benchmark_lifecycle_efficiency' | grep -v grep || true",
        ]
    ).stdout
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "loadavg": list(os.getloadavg()),
        "available_memory_bytes": available_memory_bytes(),
        "relevant_processes": process.splitlines(),
    }


def sync_filesystem(path: str) -> None:
    run_command(["sync", "-f", path], check=False, timeout=120)


class StorageSampler:
    def __init__(self, path: str, interval_seconds: float) -> None:
        self.path = path
        self.interval_seconds = interval_seconds
        sync_filesystem(path)
        self.before = used_bytes(path)
        self.maximum = self.before
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.maximum = max(self.maximum, used_bytes(self.path))
            except OSError:
                pass

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> dict[str, int]:
        sync_filesystem(self.path)
        after = used_bytes(self.path)
        self.maximum = max(self.maximum, after)
        self._stop.set()
        self._thread.join(timeout=2)
        return {
            "before_bytes": self.before,
            "after_bytes": after,
            "end_delta_bytes": after - self.before,
            "peak_delta_bytes": self.maximum - self.before,
        }


def measure(
    storage_path: str,
    interval_seconds: float,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    sampler = StorageSampler(storage_path, interval_seconds)
    sampler.start()
    started = time.perf_counter()
    try:
        details = operation()
        seconds = time.perf_counter() - started
    finally:
        storage = sampler.finish()
    return {"seconds": seconds, "storage": storage, **details}


def assert_ready(probe: dict[str, Any]) -> None:
    status = int(probe["status"])
    if status < 200 or status >= 400:
        raise AssertionError(f"readiness probe returned HTTP {status}")


def safe_marker(value: str) -> str:
    marker = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    if not marker:
        raise ValueError("empty lifecycle marker")
    return marker


def mysql_command(manager: Any, session: Any, sql: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    backend = manager._mysql_backend(session.agent_id)
    resource = manager._mysql_resource(session.agent_id)
    command = backend._runtime_command(
        [
            "mysql",
            "--protocol=tcp",
            f"--host={backend.admin_host}",
            f"--port={resource.port}",
            f"--user={backend.mysql_user}",
        ]
    )
    if backend.mysql_password:
        command.append(f"--password={backend.mysql_password}")
    command.extend([backend.mysql_database, "--batch", "--skip-column-names", "-e", sql])
    return run_command(command, check=check, timeout=30)


def postgres_command(manager: Any, session: Any, sql: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    database = manager.current_db(session.agent_id)
    return run_command(
        [
            "docker",
            "exec",
            "pg18-gitlab-nondb",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        check=check,
        timeout=30,
    )


def db_command(manager: Any, session: Any, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if session.mysql_datadir is not None:
        return mysql_command(manager, session, sql, check=check)
    return postgres_command(manager, session, sql, check=check)


def add_db_marker(manager: Any, session: Any, marker: str) -> None:
    marker = safe_marker(marker)
    if session.mysql_datadir is not None:
        sql = (
            f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} "
            "(marker VARCHAR(191) PRIMARY KEY); "
            f"INSERT IGNORE INTO {MARKER_TABLE}(marker) VALUES ('{marker}');"
        )
    else:
        sql = (
            f"CREATE TABLE IF NOT EXISTS public.{MARKER_TABLE} "
            "(marker TEXT PRIMARY KEY); "
            f"INSERT INTO public.{MARKER_TABLE}(marker) VALUES ('{marker}') "
            "ON CONFLICT DO NOTHING;"
        )
    db_command(manager, session, sql)


def db_marker_exists(manager: Any, session: Any, marker: str) -> bool:
    marker = safe_marker(marker)
    table_check = (
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema=DATABASE() AND table_name='{MARKER_TABLE}';"
        if session.mysql_datadir is not None
        else f"SELECT CASE WHEN to_regclass('public.{MARKER_TABLE}') IS NULL THEN 0 ELSE 1 END;"
    )
    table = db_command(manager, session, table_check, check=False)
    if table.returncode != 0 or table.stdout.strip().splitlines()[-1:] != ["1"]:
        return False
    query = (
        f"SELECT COUNT(*) FROM {MARKER_TABLE} WHERE marker='{marker}';"
        if session.mysql_datadir is not None
        else f"SELECT COUNT(*) FROM public.{MARKER_TABLE} WHERE marker='{marker}';"
    )
    result = db_command(manager, session, query, check=False)
    return result.returncode == 0 and result.stdout.strip().splitlines()[-1:] == ["1"]


def validate_ours(manager: Any, spec: SiteSpec, session: Any, present: set[str], absent: set[str]) -> dict[str, Any]:
    probe = ours_probe(spec, session)
    assert_ready(probe)
    for marker in present:
        if not db_marker_exists(manager, session, marker):
            raise AssertionError(f"expected DB marker is missing: {marker}")
    for marker in absent:
        if db_marker_exists(manager, session, marker):
            raise AssertionError(f"unexpected DB marker survived: {marker}")
    return {"probe": probe, "state_valid": True}


def checkpoint_step(iteration: int, salt: int) -> int:
    return 100_000 + iteration * 10 + salt


def ours_sample(spec: SiteSpec, operation_name: str, prefix: str, iteration: int, storage_path: str, interval: float) -> dict[str, Any]:
    manager = ours_manager(spec)
    session = None
    marker_a = safe_marker(f"{prefix}_{operation_name}_{iteration}_a")
    marker_b = safe_marker(f"{prefix}_{operation_name}_{iteration}_b")
    try:
        if operation_name == "create":
            def action() -> dict[str, Any]:
                nonlocal session
                session = manager.prepare_for_task(
                    f"{prefix}_{spec.name}_create_{iteration}_{uuid.uuid4().hex[:8]}",
                    ours_task(spec),
                )
                return validate_ours(manager, spec, session, set(), {marker_a})
            return measure(storage_path, interval, action)

        session = manager.prepare_for_task(
            f"{prefix}_{spec.name}_{operation_name}_{iteration}_{uuid.uuid4().hex[:8]}",
            ours_task(spec),
        )
        add_db_marker(manager, session, marker_a)

        if operation_name == "reset":
            def action() -> dict[str, Any]:
                manager.reset(session)
                return validate_ours(manager, spec, session, set(), {marker_a})
            return measure(storage_path, interval, action)

        if operation_name == "clone":
            def action() -> dict[str, Any]:
                checkpoint = manager.checkpoint(
                    session, checkpoint_step(iteration, 1)
                )
                details = validate_ours(manager, spec, session, {marker_a}, set())
                details["checkpoint"] = checkpoint
                return details
            return measure(storage_path, interval, action)

        checkpoint = manager.checkpoint(
            session, checkpoint_step(iteration, 2 if operation_name == "restore" else 3)
        )
        add_db_marker(manager, session, marker_b)
        if operation_name == "restore":
            def action() -> dict[str, Any]:
                manager.restore(session, checkpoint)
                details = validate_ours(
                    manager, spec, session, {marker_a}, {marker_b}
                )
                details["checkpoint"] = checkpoint
                return details
            return measure(storage_path, interval, action)

        if operation_name == "fork":
            def action() -> dict[str, Any]:
                branch = manager.fork(
                    session, checkpoint, f"bench_{iteration}_{uuid.uuid4().hex[:6]}"
                )
                details = validate_ours(
                    manager, spec, session, {marker_a}, {marker_b}
                )
                details.update({"checkpoint": checkpoint, "branch": branch})
                return details
            return measure(storage_path, interval, action)

        raise ValueError(f"unknown operation: {operation_name}")
    finally:
        if session is not None:
            manager.cleanup(session)


def marker_path(marker: str) -> str:
    return f"/tmp/cfwa-lifecycle-{safe_marker(marker)}"


def add_docker_marker(container: str, marker: str) -> None:
    run_command(
        ["docker", "exec", container, "touch", marker_path(marker)],
        timeout=30,
    )


def docker_marker_exists(container: str, marker: str) -> bool:
    result = run_command(
        ["docker", "exec", container, "test", "-f", marker_path(marker)],
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def validate_docker(container: str, spec: SiteSpec, present: set[str], absent: set[str]) -> dict[str, Any]:
    url, headers = docker_url_headers(container, spec)
    probe = http_probe(url, headers, timeout=30)
    assert_ready(probe)
    for marker in present:
        if not docker_marker_exists(container, marker):
            raise AssertionError(f"expected Docker marker is missing: {marker}")
    for marker in absent:
        if docker_marker_exists(container, marker):
            raise AssertionError(f"unexpected Docker marker survived: {marker}")
    return {"probe": probe, "state_valid": True}


def start_docker_image(name: str, spec: SiteSpec, image: str) -> dict[str, Any]:
    return start_docker(name, replace(spec, image=image))


def remove_snapshot(image: str) -> None:
    if not image.startswith("cfwa-snapshot-"):
        raise ValueError(f"refusing to remove non-benchmark image {image}")
    run_command(["docker", "image", "rm", "--force", image], check=False, timeout=180)


def commit_snapshot(container: str, image: str) -> None:
    run_command(["docker", "commit", container, image], timeout=900)


def docker_sample(spec: SiteSpec, operation_name: str, prefix: str, iteration: int, storage_path: str, interval: float) -> dict[str, Any]:
    source = f"{prefix}-{spec.name}-{operation_name}-{iteration}-source"
    child = f"{prefix}-{spec.name}-{operation_name}-{iteration}-child"
    snapshot = f"cfwa-snapshot-{prefix.removeprefix('cfwa-fresh-')}-{spec.name}-{operation_name}-{iteration}:latest"
    containers: set[str] = set()
    images: set[str] = set()
    marker_a = safe_marker(f"{prefix}_{operation_name}_{iteration}_a")
    marker_b = safe_marker(f"{prefix}_{operation_name}_{iteration}_b")
    try:
        if operation_name == "create":
            def action() -> dict[str, Any]:
                containers.add(source)
                launch = start_docker(source, spec)
                details = validate_docker(source, spec, set(), {marker_a})
                details["launch"] = launch
                details["sizes"] = docker_sizes(source, spec)
                return details
            return measure(storage_path, interval, action)

        containers.add(source)
        start_docker(source, spec)
        add_docker_marker(source, marker_a)

        if operation_name == "reset":
            def action() -> dict[str, Any]:
                remove_docker(source)
                containers.discard(source)
                containers.add(source)
                launch = start_docker(source, spec)
                details = validate_docker(source, spec, set(), {marker_a})
                details["launch"] = launch
                details["sizes"] = docker_sizes(source, spec)
                return details
            return measure(storage_path, interval, action)

        if operation_name == "clone":
            def action() -> dict[str, Any]:
                images.add(snapshot)
                commit_snapshot(source, snapshot)
                details = validate_docker(source, spec, {marker_a}, set())
                details["snapshot_image"] = snapshot
                return details
            return measure(storage_path, interval, action)

        images.add(snapshot)
        commit_snapshot(source, snapshot)
        add_docker_marker(source, marker_b)

        if operation_name == "restore":
            def action() -> dict[str, Any]:
                remove_docker(source)
                containers.discard(source)
                containers.add(source)
                launch = start_docker_image(source, spec, snapshot)
                details = validate_docker(source, spec, {marker_a}, {marker_b})
                details.update({"launch": launch, "snapshot_image": snapshot})
                return details
            return measure(storage_path, interval, action)

        if operation_name == "fork":
            def action() -> dict[str, Any]:
                containers.add(child)
                launch = start_docker_image(child, spec, snapshot)
                details = validate_docker(child, spec, {marker_a}, {marker_b})
                if not docker_marker_exists(source, marker_b):
                    raise AssertionError("Docker fork modified or lost parent state")
                details.update({"launch": launch, "snapshot_image": snapshot})
                return details
            return measure(storage_path, interval, action)

        raise ValueError(f"unknown operation: {operation_name}")
    finally:
        for name in sorted(containers, reverse=True):
            remove_docker(name)
        for image in sorted(images, reverse=True):
            remove_snapshot(image)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    good = [sample for sample in samples if "error" not in sample]
    return {
        "latency_seconds": distribution([sample["seconds"] for sample in good]),
        "storage_end_delta_bytes": distribution(
            [sample["storage"]["end_delta_bytes"] for sample in good]
        ),
        "storage_peak_delta_bytes": distribution(
            [sample["storage"]["peak_delta_bytes"] for sample in good]
        ),
        "successes": len(good),
        "failures": len(samples) - len(good),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--sites", default="shopping_admin,shopping,gitlab")
    parser.add_argument("--implementations", default="ours,docker")
    parser.add_argument("--operations", default=",".join(OPERATIONS))
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--ours-storage-path", default="/var/lib/web-agent")
    parser.add_argument("--docker-storage-path", default="")
    parser.add_argument("--storage-sample-interval", type=float, default=0.05)
    parser.add_argument(
        "--environment-prefix",
        default="cfwa-fresh-life",
        help="short prefix for environment IDs; keep PostgreSQL-derived names under 63 characters",
    )
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    sites = parse_csv(args.sites)
    implementations = parse_csv(args.implementations)
    operations = parse_csv(args.operations)
    for site in sites:
        if site not in SITES:
            raise SystemExit(f"unknown site: {site}")
    for implementation in implementations:
        if implementation not in {"ours", "docker"}:
            raise SystemExit(f"unknown implementation: {implementation}")
    for operation_name in operations:
        if operation_name not in OPERATIONS:
            raise SystemExit(f"unknown operation: {operation_name}")
    if args.iterations < 1:
        raise SystemExit("--iterations must be positive")

    docker_storage = args.docker_storage_path or docker_root()
    output = Path(args.output).resolve()
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    prefix = args.environment_prefix.strip("-") + "-" + run_id.replace("_", "-")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "semantics": {
            "create": "base -> new active environment -> ready and state-valid",
            "reset": "dirty active environment -> base -> ready and state-valid",
            "clone": "active state -> durable cold checkpoint; source remains active",
            "restore": "existing checkpoint -> replace active state -> ready and state-valid",
            "fork": "existing checkpoint -> independent branch -> ready and state-valid",
            "docker_note": "clone/restore/fork use docker commit and are emulated, not native WebArena APIs",
        },
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "cpu_visible": os.cpu_count(),
            "ours_storage_path": args.ours_storage_path,
            "docker_storage_path": docker_storage,
        },
        "parameters": vars(args),
        "background_before": background_snapshot(),
        "results": {},
    }
    atomic_json(output, payload)

    try:
        for site in sites:
            payload["results"][site] = {}
            for implementation in implementations:
                payload["results"][site][implementation] = {}
                for operation_name in operations:
                    samples = []
                    for iteration in range(args.iterations):
                        print(
                            f"[lifecycle] site={site} implementation={implementation} "
                            f"operation={operation_name} iteration={iteration + 1}/{args.iterations}",
                            flush=True,
                        )
                        try:
                            if implementation == "ours":
                                sample = ours_sample(
                                    SITES[site], operation_name, prefix, iteration,
                                    args.ours_storage_path, args.storage_sample_interval,
                                )
                            else:
                                sample = docker_sample(
                                    SITES[site], operation_name, prefix, iteration,
                                    docker_storage, args.storage_sample_interval,
                                )
                            samples.append(sample)
                        except Exception as error:
                            samples.append({"error": repr(error)})
                            if not args.continue_on_error:
                                raise
                        payload["results"][site][implementation][operation_name] = {
                            "samples": samples,
                            "summary": summarize(samples),
                            "native": not (
                                implementation == "docker"
                                and operation_name in {"clone", "restore", "fork"}
                            ),
                        }
                        atomic_json(output, payload)
    except Exception as error:
        payload["status"] = "failed"
        payload["fatal_error"] = repr(error)
        raise
    else:
        payload["status"] = "complete"
    finally:
        payload["background_after"] = background_snapshot()
        atomic_json(output, payload)

    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
