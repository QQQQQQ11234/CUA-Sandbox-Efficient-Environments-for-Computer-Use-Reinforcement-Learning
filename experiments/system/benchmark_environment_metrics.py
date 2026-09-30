#!/usr/bin/env python3
"""Fresh Docker-vs-DB-isolation system benchmark for WebArena inference.

This benchmark intentionally excludes task accuracy and reward agreement.  It
measures the remaining inference-environment metrics from a clean set of newly
created resources:

* startup and reset latency;
* fixed and incremental memory;
* base/logical and incremental physical storage;
* routed HTTP request latency;
* Shopping concurrency scaling.

Every Docker container and DB-isolated session created here has a unique run
prefix and is removed in a finally block.  Existing evaluation routes and
containers are observed as background load but are never mutated or removed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rl_web_agent.isolation.db_isolation import DBIsolationManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_PATH = "/var/lib/web-agent"


@dataclass(frozen=True)
class SiteSpec:
    name: str
    task_id: int
    image: str
    docker_port: int
    docker_path: str
    docker_host: str
    docker_command: tuple[str, ...]
    ours_url: str
    shared_containers: tuple[str, ...]
    template_path: str | None
    timeout_seconds: float


SITES = {
    "shopping_admin": SiteSpec(
        name="shopping_admin",
        task_id=0,
        image="webarenaimages/shopping_admin_final_0719:latest",
        docker_port=80,
        docker_path="/admin",
        docker_host="metis.lti.cs.cmu.edu:7780",
        docker_command=(),
        ours_url="http://127.0.0.1:7780/admin",
        shared_containers=(
            "shared-shopping-admin",
            "shopping-admin-mysql-runtime",
            "shopping-route-registry",
        ),
        template_path=(
            "/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs/"
            "magento_admin_template"
        ),
        timeout_seconds=240,
    ),
    "shopping": SiteSpec(
        name="shopping",
        task_id=21,
        image="webarenaimages/shopping_final_0712:latest",
        docker_port=80,
        docker_path="/",
        docker_host="metis.lti.cs.cmu.edu:7770",
        docker_command=(),
        ours_url="http://127.0.0.1:7770/",
        shared_containers=(
            "shared-shopping",
            "shopping-mysql-runtime",
            "shopping-route-registry",
        ),
        template_path=(
            "/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs/"
            "magento_template"
        ),
        timeout_seconds=300,
    ),
    "gitlab": SiteSpec(
        name="gitlab",
        task_id=44,
        image="gitlab-populated-final-port8023:latest",
        docker_port=8023,
        docker_path="/users/sign_in",
        docker_host="127.0.0.1:8023",
        docker_command=("/opt/gitlab/embedded/bin/runsvdir-start",),
        ours_url="http://127.0.0.1:8023/users/sign_in",
        shared_containers=(
            "shared-gitlab-nondb",
            "pg18-gitlab-nondb",
            "web-agent-route-registry-nondb",
        ),
        template_path=None,
        timeout_seconds=900,
    ),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect)


def run_command(
    command: list[str], *, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def used_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return (stat.f_blocks - stat.f_bfree) * stat.f_frsize


def directory_sizes(path: str | None) -> dict[str, int] | None:
    if path is None or not Path(path).exists():
        return None
    allocated = int(
        run_command(["du", "-s", "-B1", path]).stdout.split()[0]
    )
    apparent = int(
        run_command(["du", "-s", "-B1", "--apparent-size", path])
        .stdout.split()[0]
    )
    return {"allocated_bytes": allocated, "apparent_bytes": apparent}


def memory_cgroup_for_pid(pid: int) -> Path:
    entries = [
        line.split(":", 2)
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    ]
    memory_v1 = next(
        (
            path
            for _, controllers, path in entries
            if "memory" in controllers.split(",")
        ),
        "",
    )
    if memory_v1:
        return Path("/sys/fs/cgroup/memory") / memory_v1.lstrip("/")
    unified = next(
        (path for _, controllers, path in entries if not controllers), ""
    )
    return Path("/sys/fs/cgroup/unified") / unified.lstrip("/")


def cgroup_memory_for_pid(pid: int) -> dict[str, int]:
    root = memory_cgroup_for_pid(pid)
    current_path = root / "memory.current"
    if not current_path.exists():
        current_path = root / "memory.usage_in_bytes"
    stats = {}
    for line in (root / "memory.stat").read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            stats[fields[0]] = int(fields[1])
    current = int(current_path.read_text())
    inactive_file = stats.get(
        "inactive_file", stats.get("total_inactive_file", 0)
    )
    return {
        "current_bytes": current,
        "anon_bytes": stats.get(
            "anon", stats.get("total_rss", stats.get("rss", 0))
        ),
        "file_bytes": stats.get(
            "file", stats.get("total_cache", stats.get("cache", 0))
        ),
        "inactive_file_bytes": inactive_file,
        # This is the memory accounting used by modern `docker stats`: cgroup
        # usage less reclaimable inactive file cache.  Keep raw usage and its
        # components above as well so the reported number is auditable.
        "working_set_bytes": max(0, current - inactive_file),
    }


def container_pid(name: str) -> int:
    return int(
        run_command(
            ["docker", "inspect", "-f", "{{.State.Pid}}", name]
        ).stdout.strip()
    )


def container_memory(name: str) -> dict[str, int]:
    return cgroup_memory_for_pid(container_pid(name))


def stable_container_memory(
    names: list[str] | tuple[str, ...], samples: int = 3
) -> dict[str, Any]:
    snapshots = []
    for sample_index in range(samples):
        snapshots.append({name: container_memory(name) for name in names})
        if sample_index + 1 < samples:
            time.sleep(0.25)
    result: dict[str, Any] = {"containers": {}}
    for name in names:
        result["containers"][name] = {
            key: int(statistics.median(item[name][key] for item in snapshots))
            for key in (
                "current_bytes",
                "anon_bytes",
                "file_bytes",
                "inactive_file_bytes",
                "working_set_bytes",
            )
        }
    for key in (
        "current_bytes",
        "anon_bytes",
        "file_bytes",
        "inactive_file_bytes",
        "working_set_bytes",
    ):
        result[f"total_{key}"] = sum(
            item[key] for item in result["containers"].values()
        )
    return result


def launch_distribution(
    records: list[dict[str, Any]], warmup_iterations: int
) -> dict[str, Any]:
    """Summarize sequential launches using CUA-Sandbox's warm-cache protocol."""
    measured = records[warmup_iterations:]
    return {
        "warmup_count": min(warmup_iterations, len(records)),
        "measurement_count": len(measured),
        "all_summary_seconds": distribution(
            [record["seconds"] for record in records]
        ),
        "summary_seconds": distribution(
            [record["seconds"] for record in measured]
        ),
    }


def http_probe(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    started = time.perf_counter()
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            body = response.read(65536)
            return {
                "status": int(response.status),
                "elapsed_seconds": time.perf_counter() - started,
                "bytes_read": len(body),
            }
    except urllib.error.HTTPError as error:
        body = error.read(65536)
        return {
            "status": int(error.code),
            "elapsed_seconds": time.perf_counter() - started,
            "bytes_read": len(body),
        }


def wait_http(
    url_factory: Callable[[], tuple[str, dict[str, str]]], timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            url, headers = url_factory()
            result = http_probe(url, headers, timeout=min(10, timeout))
            if result["status"] < 500:
                return result
            last_error = f"HTTP {result['status']}"
        except (OSError, urllib.error.URLError, subprocess.SubprocessError) as error:
            last_error = repr(error)
        time.sleep(0.25)
    raise TimeoutError(f"HTTP readiness timed out: {last_error}")


def docker_ip(name: str) -> str:
    value = run_command(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            name,
        ]
    ).stdout.strip()
    if not value:
        raise RuntimeError(f"container {name} has no bridge IP")
    return value


def docker_url_headers(name: str, spec: SiteSpec) -> tuple[str, dict[str, str]]:
    return (
        f"http://{docker_ip(name)}:{spec.docker_port}{spec.docker_path}",
        {"Host": spec.docker_host},
    )


def start_docker(name: str, spec: SiteSpec) -> dict[str, Any]:
    command = ["docker", "run", "--name", name, "--detach", spec.image]
    command.extend(spec.docker_command)
    started = time.perf_counter()
    run_command(command, timeout=120)
    probe = wait_http(
        lambda: docker_url_headers(name, spec), spec.timeout_seconds
    )
    return {
        "seconds": time.perf_counter() - started,
        "probe": probe,
    }


def remove_docker(name: str) -> None:
    # ``g-`` is the intentionally short prefix used by the GitLab fork
    # supplement so PostgreSQL identifiers stay below its 63-character limit.
    # Keep the allow-list explicit; this helper must never remove arbitrary
    # user containers.
    if not (name.startswith("cfwa-fresh-") or name.startswith("g-")):
        raise ValueError(f"refusing to remove non-benchmark container {name}")
    run_command(["docker", "rm", "--force", name], check=False, timeout=120)


def docker_sizes(name: str, spec: SiteSpec) -> dict[str, int]:
    inspect = json.loads(
        run_command(["docker", "inspect", "--size", name]).stdout
    )[0]
    image = json.loads(
        run_command(["docker", "image", "inspect", spec.image]).stdout
    )[0]
    return {
        "image_base_bytes": int(image.get("Size", 0)),
        "container_rw_bytes": int(inspect.get("SizeRw", 0)),
        "container_rootfs_bytes": int(inspect.get("SizeRootFs", 0)),
    }


def shared_stack_storage(spec: SiteSpec) -> dict[str, Any]:
    """Report logical shared-image bytes separately from mutable DB storage."""
    containers: dict[str, Any] = {}
    images: dict[str, int] = {}
    for name in spec.shared_containers:
        inspect = json.loads(
            run_command(["docker", "inspect", "--size", name]).stdout
        )[0]
        image_id = str(inspect["Image"])
        containers[name] = {
            "image_id": image_id,
            "container_rw_bytes": int(inspect.get("SizeRw", 0)),
        }
        if image_id not in images:
            image = json.loads(
                run_command(["docker", "image", "inspect", image_id]).stdout
            )[0]
            images[image_id] = int(image.get("Size", 0))
    if spec.name == "gitlab":
        template_database_bytes = int(
            run_command(
                [
                    "docker",
                    "exec",
                    "pg18-gitlab-nondb",
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-Atc",
                    "select pg_database_size('gitlab_base_template')",
                ]
            ).stdout.strip()
        )
        template = {
            "database": "gitlab_base_template",
            "logical_bytes": template_database_bytes,
        }
    else:
        template = directory_sizes(spec.template_path)
    return {
        "containers": containers,
        "unique_image_logical_bytes": images,
        "shared_image_logical_total_bytes": sum(images.values()),
        "shared_container_rw_total_bytes": sum(
            item["container_rw_bytes"] for item in containers.values()
        ),
        "database_template": template,
    }


def docker_single_metrics(
    spec: SiteSpec,
    prefix: str,
    iterations: int,
    warmup_iterations: int,
    reset_iterations: int,
    latency_samples: int,
) -> dict[str, Any]:
    startup_records = []
    reset_records = []
    names: set[str] = set()
    try:
        for index in range(iterations):
            name = f"{prefix}-{spec.name}-startup-{index}"
            names.add(name)
            record = start_docker(name, spec)
            startup_records.append(record)
            remove_docker(name)
            names.discard(name)

        if reset_iterations:
            reset_name = f"{prefix}-{spec.name}-reset"
            names.add(reset_name)
            start_docker(reset_name, spec)
            for _ in range(reset_iterations):
                started = time.perf_counter()
                remove_docker(reset_name)
                record = start_docker(reset_name, spec)
                record["seconds"] = time.perf_counter() - started
                reset_records.append(record)
            remove_docker(reset_name)
            names.discard(reset_name)

        measure_name = f"{prefix}-{spec.name}-measure"
        names.add(measure_name)
        start_docker(measure_name, spec)
        time.sleep(1)
        idle_memory = stable_container_memory([measure_name])
        url, headers = docker_url_headers(measure_name, spec)
        for _ in range(3):
            http_probe(url, headers)
        latencies = [
            http_probe(url, headers)["elapsed_seconds"]
            for _ in range(latency_samples)
        ]
        active_memory = stable_container_memory([measure_name])
        sizes = docker_sizes(measure_name, spec)
        remove_docker(measure_name)
        names.discard(measure_name)
    finally:
        for name in sorted(names):
            remove_docker(name)

    return {
        "startup": {
            "samples": startup_records,
            "protocol": (
                "sequential launches; first N warm disk cache; first usable "
                "HTTP response marks ready"
            ),
            **launch_distribution(startup_records, warmup_iterations),
        },
        "reset": {
            "definition": "docker rm -f + docker run + first HTTP response",
            "samples": reset_records,
            "summary_seconds": distribution(
                [record["seconds"] for record in reset_records]
            ),
        },
        "memory": {"idle": idle_memory, "active": active_memory},
        "storage": sizes,
        "request_latency_seconds": {
            "samples": latencies,
            "summary": distribution(latencies),
        },
    }


def container_env_value(container: str, key: str) -> str:
    rows = run_command(
        ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container]
    ).stdout.splitlines()
    prefix = key + "="
    for row in rows:
        if row.startswith(prefix):
            return row[len(prefix) :]
    raise KeyError(f"{key} is missing from {container}")


def ours_manager(spec: SiteSpec) -> DBIsolationManager:
    logger = type(
        "BenchmarkLogger",
        (),
        {"info": print, "warning": print, "exception": print},
    )()
    if spec.name in {"shopping", "shopping_admin"}:
        os.environ["ROUTE_TOKEN_SECRET"] = container_env_value(
            "shared-shopping-admin", "WEB_AGENT_ROUTE_TOKEN_SECRET"
        )
        os.environ.setdefault(
            "ROUTE_REGISTRY_PATH",
            str(PROJECT_ROOT / "runtime/shopping_db_routes.sqlite3"),
        )
        if spec.name == "shopping_admin":
            from experiments.shopping.run_official_cms_db_eval import db_config

            config = db_config()
        else:
            from experiments.shopping.run_official_storefront_db_eval import (
                storefront_db_config,
            )

            config = storefront_db_config()
    else:
        runtime = json.loads(
            (PROJECT_ROOT / "runtime/gitlab_nondb_config.json").read_text()
        )
        os.environ["ROUTE_TOKEN_SECRET"] = runtime["route_secret"]
        os.environ["ROUTE_REGISTRY_PATH"] = str(
            PROJECT_ROOT / "runtime/db_routes_nondb.sqlite3"
        )
        from experiments.gitlab.run_official_db_eval import db_config

        config = db_config()
    return DBIsolationManager(config, logger)


def ours_task(spec: SiteSpec) -> dict[str, Any]:
    return {"task_id": spec.task_id, "sites": [spec.name]}


def ours_probe(spec: SiteSpec, session: Any) -> dict[str, Any]:
    return http_probe(spec.ours_url, dict(session.headers), timeout=30)


def ours_single_metrics(
    spec: SiteSpec,
    prefix: str,
    iterations: int,
    warmup_iterations: int,
    reset_iterations: int,
    latency_samples: int,
    storage_path: str,
) -> dict[str, Any]:
    manager = ours_manager(spec)
    startup_records = []
    sessions = []
    try:
        for index in range(iterations):
            started = time.perf_counter()
            session = manager.prepare_for_task(
                f"{prefix}_{spec.name}_startup_{index}_{uuid.uuid4().hex[:8]}",
                ours_task(spec),
            )
            probe = ours_probe(spec, session)
            startup_records.append(
                {"seconds": time.perf_counter() - started, "probe": probe}
            )
            manager.cleanup(session)

        reset_records = []
        if reset_iterations:
            reset_session = manager.prepare_for_task(
                f"{prefix}_{spec.name}_reset_{uuid.uuid4().hex[:8]}",
                ours_task(spec),
            )
            sessions.append(reset_session)
            for _ in range(reset_iterations):
                started = time.perf_counter()
                manager.reset(reset_session)
                probe = ours_probe(spec, reset_session)
                reset_records.append(
                    {"seconds": time.perf_counter() - started, "probe": probe}
                )
            manager.cleanup(reset_session)
            sessions.remove(reset_session)

        before_memory = stable_container_memory(spec.shared_containers)
        before_storage = used_bytes(storage_path)
        measure_session = manager.prepare_for_task(
            f"{prefix}_{spec.name}_measure_{uuid.uuid4().hex[:8]}",
            ours_task(spec),
        )
        sessions.append(measure_session)
        ours_probe(spec, measure_session)
        time.sleep(1)
        idle_memory = stable_container_memory(spec.shared_containers)
        after_storage = used_bytes(storage_path)
        for _ in range(3):
            ours_probe(spec, measure_session)
        latencies = [
            ours_probe(spec, measure_session)["elapsed_seconds"]
            for _ in range(latency_samples)
        ]
        active_memory = stable_container_memory(spec.shared_containers)
        manager.cleanup(measure_session)
        sessions.remove(measure_session)
    finally:
        for session in reversed(sessions):
            manager.cleanup(session)

    return {
        "startup": {
            "samples": startup_records,
            "protocol": (
                "sequential launches; first N warm disk cache; first usable "
                "routed HTTP response marks ready"
            ),
            **launch_distribution(startup_records, warmup_iterations),
        },
        "reset": {
            "definition": "freeze/drain + restore DB/non-DB state + first routed HTTP response",
            "samples": reset_records,
            "summary_seconds": distribution(
                [record["seconds"] for record in reset_records]
            ),
        },
        "memory": {
            "shared_baseline": before_memory,
            "idle_with_one_environment": idle_memory,
            "active_with_one_environment": active_memory,
            "idle_increment_bytes": (
                idle_memory["total_current_bytes"]
                - before_memory["total_current_bytes"]
            ),
            "active_increment_bytes": (
                active_memory["total_current_bytes"]
                - before_memory["total_current_bytes"]
            ),
            "idle_working_set_increment_bytes": (
                idle_memory["total_working_set_bytes"]
                - before_memory["total_working_set_bytes"]
            ),
            "active_working_set_increment_bytes": (
                active_memory["total_working_set_bytes"]
                - before_memory["total_working_set_bytes"]
            ),
            "idle_anon_increment_bytes": (
                idle_memory["total_anon_bytes"]
                - before_memory["total_anon_bytes"]
            ),
            "active_anon_increment_bytes": (
                active_memory["total_anon_bytes"]
                - before_memory["total_anon_bytes"]
            ),
        },
        "storage": {
            "shared_base": shared_stack_storage(spec),
            "physical_increment_bytes": after_storage - before_storage,
            "measurement": f"statvfs used-block delta on {storage_path}",
        },
        "request_latency_seconds": {
            "samples": latencies,
            "summary": distribution(latencies),
        },
    }


def available_memory_bytes() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def parallel_map(
    function: Callable[[int], Any], count: int, workers: int
) -> tuple[list[Any], list[str]]:
    if count <= 0:
        return [], []
    results: list[Any] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, count)
    ) as executor:
        futures = {executor.submit(function, index): index for index in range(count)}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                errors.append(f"index={futures[future]} error={error!r}")
    return results, errors


def docker_scale_level(
    spec: SiteSpec, prefix: str, level: int, workers: int
) -> dict[str, Any]:
    names = [f"{prefix}-scale-docker-{level}-{index}" for index in range(level)]

    def launch(index: int) -> dict[str, Any]:
        record = start_docker(names[index], spec)
        return {"index": index, **record}

    started = time.perf_counter()
    records, errors = parallel_map(launch, level, workers)
    wall = time.perf_counter() - started
    running = []
    available_at_peak = 0
    try:
        for name in names:
            inspect = run_command(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                check=False,
            )
            if inspect.returncode == 0 and inspect.stdout.strip() == "true":
                running.append(name)
        memory = stable_container_memory(running) if running else None
        sizes = [docker_sizes(name, spec) for name in running]
        available_at_peak = available_memory_bytes()
    finally:
        parallel_map(lambda index: remove_docker(names[index]), level, workers)
    return {
        "requested": level,
        "ready": len(records),
        "failures": errors,
        "wall_startup_seconds": wall,
        "per_environment_startup_seconds": distribution(
            [record["seconds"] for record in records]
        ),
        "memory": memory,
        "container_rw_total_bytes": sum(
            item["container_rw_bytes"] for item in sizes
        ),
        "available_memory_bytes_at_peak": available_at_peak,
    }


def ours_scale_level(
    spec: SiteSpec,
    prefix: str,
    level: int,
    workers: int,
    storage_path: str,
) -> dict[str, Any]:
    manager = ours_manager(spec)
    sessions: list[Any] = []
    sessions_lock = threading.Lock()
    baseline_memory = stable_container_memory(spec.shared_containers)
    baseline_storage = used_bytes(storage_path)

    def launch(index: int) -> dict[str, Any]:
        started = time.perf_counter()
        session = manager.prepare_for_task(
            f"{prefix}_{spec.name}_scale_{level}_{index}_{uuid.uuid4().hex[:8]}",
            ours_task(spec),
        )
        try:
            probe = ours_probe(spec, session)
        except Exception:
            manager.cleanup(session)
            raise
        with sessions_lock:
            sessions.append(session)
        return {
            "index": index,
            "seconds": time.perf_counter() - started,
            "probe": probe,
        }

    started = time.perf_counter()
    records, errors = parallel_map(launch, level, workers)
    wall = time.perf_counter() - started
    try:
        memory = stable_container_memory(spec.shared_containers)
        storage = used_bytes(storage_path)
        available_at_peak = available_memory_bytes()
    finally:
        def cleanup(index: int) -> None:
            manager.cleanup(sessions[index])

        parallel_map(cleanup, len(sessions), workers)
    return {
        "requested": level,
        "ready": len(records),
        "failures": errors,
        "wall_startup_seconds": wall,
        "per_environment_startup_seconds": distribution(
            [record["seconds"] for record in records]
        ),
        "memory": memory,
        "shared_baseline_memory": baseline_memory,
        "memory_increment_bytes": (
            memory["total_current_bytes"]
            - baseline_memory["total_current_bytes"]
        ),
        "physical_storage_increment_bytes": storage - baseline_storage,
        "available_memory_bytes_at_peak": available_at_peak,
    }


def background_snapshot() -> dict[str, Any]:
    process = run_command(
        [
            "bash",
            "-lc",
            "ps -eo pid,etime,pcpu,pmem,rss,cmd | "
            "grep -E 'run_official_|qwen_gpu_server' | grep -v grep || true",
        ]
    ).stdout
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "loadavg": list(os.getloadavg()),
        "available_memory_bytes": available_memory_bytes(),
        "relevant_processes": process.splitlines(),
    }


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--sites", default="shopping_admin,shopping,gitlab"
    )
    parser.add_argument("--implementations", default="docker,ours")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--warmup-iterations",
        type=int,
        default=0,
        help="exclude the first N sequential launches from the launch mean",
    )
    parser.add_argument("--reset-iterations", type=int, default=3)
    parser.add_argument("--latency-samples", type=int, default=20)
    parser.add_argument("--storage-path", default=DEFAULT_STORAGE_PATH)
    parser.add_argument("--skip-single", action="store_true")
    parser.add_argument("--scale", action="store_true")
    parser.add_argument("--scale-site", default="shopping")
    parser.add_argument("--scale-levels", default="1,16,32,64,128,256")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--minimum-available-memory-gib", type=float, default=64.0
    )
    args = parser.parse_args()

    if args.warmup_iterations < 0 or args.warmup_iterations >= args.iterations:
        raise SystemExit(
            "--warmup-iterations must be non-negative and less than --iterations"
        )

    selected_sites = parse_csv(args.sites)
    implementations = parse_csv(args.implementations)
    for site in selected_sites:
        if site not in SITES:
            raise SystemExit(f"unknown site: {site}")
    for implementation in implementations:
        if implementation not in {"docker", "ours"}:
            raise SystemExit(f"unknown implementation: {implementation}")

    output_path = Path(args.output).resolve()
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    prefix = "cfwa-fresh-" + run_id.replace("_", "-")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "fresh_measurements": True,
        "excluded_metrics": ["task_success", "reward_agreement"],
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_visible": os.cpu_count(),
            "storage_path": args.storage_path,
        },
        "parameters": vars(args),
        "background_before": background_snapshot(),
        "single_environment": {},
        "scaling": {},
        "status": "running",
    }
    atomic_json(output_path, payload)

    if not args.skip_single:
        for site in selected_sites:
            spec = SITES[site]
            payload["single_environment"][site] = {}
            for implementation in implementations:
                print(f"[benchmark] site={site} implementation={implementation}")
                try:
                    if implementation == "docker":
                        result = docker_single_metrics(
                            spec,
                            prefix,
                            args.iterations,
                            args.warmup_iterations,
                            args.reset_iterations,
                            args.latency_samples,
                        )
                    else:
                        result = ours_single_metrics(
                            spec,
                            prefix,
                            args.iterations,
                            args.warmup_iterations,
                            args.reset_iterations,
                            args.latency_samples,
                            args.storage_path,
                        )
                    payload["single_environment"][site][implementation] = result
                except Exception as error:
                    payload["single_environment"][site][implementation] = {
                        "error": repr(error)
                    }
                atomic_json(output_path, payload)

    if args.scale:
        spec = SITES[args.scale_site]
        levels = [int(value) for value in parse_csv(args.scale_levels)]
        minimum_available = int(args.minimum_available_memory_gib * 2**30)
        for implementation in implementations:
            payload["scaling"][implementation] = []
            for level in levels:
                available_before = available_memory_bytes()
                if available_before < minimum_available:
                    payload["scaling"][implementation].append(
                        {
                            "requested": level,
                            "skipped": "available memory below safety threshold",
                        }
                    )
                    break
                if implementation == "docker":
                    previous = [
                        item
                        for item in payload["scaling"][implementation]
                        if item.get("ready", 0) > 0 and item.get("memory")
                    ]
                    if previous:
                        last = previous[-1]
                        bytes_per_environment = (
                            last["memory"]["total_current_bytes"]
                            / last["ready"]
                        )
                        predicted = int(bytes_per_environment * level)
                        usable = max(0, available_before - minimum_available)
                        if predicted > usable:
                            payload["scaling"][implementation].append(
                                {
                                    "requested": level,
                                    "skipped": (
                                        "empirical Docker RAM projection exceeds "
                                        "the configured host safety reserve"
                                    ),
                                    "predicted_container_memory_bytes": predicted,
                                    "available_memory_bytes_before": available_before,
                                    "safety_reserve_bytes": minimum_available,
                                }
                            )
                            atomic_json(output_path, payload)
                            break
                print(
                    f"[benchmark] scale site={spec.name} "
                    f"implementation={implementation} level={level}"
                )
                try:
                    if implementation == "docker":
                        result = docker_scale_level(
                            spec, prefix, level, args.workers
                        )
                    else:
                        result = ours_scale_level(
                            spec,
                            prefix,
                            level,
                            args.workers,
                            args.storage_path,
                        )
                except Exception as error:
                    result = {"requested": level, "error": repr(error)}
                payload["scaling"][implementation].append(result)
                atomic_json(output_path, payload)
                if result.get("ready", level) < level:
                    break

    payload["background_after"] = background_snapshot()
    payload["status"] = "complete"
    atomic_json(output_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
