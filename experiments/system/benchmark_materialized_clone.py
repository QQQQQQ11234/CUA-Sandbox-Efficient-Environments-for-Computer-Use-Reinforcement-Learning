#!/usr/bin/env python3
"""Materialized stopped-state clone for WebArena Shopping sites.

Both sides copy the actual mutable snapshot into a fresh target directory with
the same ``cp -a`` primitive.  No child service is started.  Docker snapshots
are stopped containers' overlay writable layers; CUA-Sandbox snapshots are the
MySQL checkpoint datadir plus Magento non-DB checkpoint tree.  GitLab is not
included because its logical PostgreSQL clone is owned by a shared server and
cannot be copied as an independent stopped datadir without stopping that
shared service.
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

from experiments.system.benchmark_environment_metrics import SITES, ours_manager, ours_task
from experiments.system.benchmark_lifecycle_efficiency import (
    add_db_marker,
    background_snapshot,
    docker_root,
    run_command,
    safe_marker,
)


DOCKER_SOURCES = {
    "shopping_admin": "shopping_admin",
    "shopping": "shopping",
}


def cp_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    # Use the same ordinary recursive copy primitive for both implementations.
    # This deliberately does not give Docker a metadata-only create operation.
    subprocess.run(
        ["cp", "-a", str(source / "."), str(target)],
        check=True,
        capture_output=True,
        timeout=1800,
    )


def tree_stats(path: Path) -> dict[str, int]:
    apparent = int(
        subprocess.run(
            ["du", "-s", "-B1", "--apparent-size", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
    )
    allocated = int(
        subprocess.run(
            ["du", "-s", "-B1", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
    )
    files = sum(1 for item in path.rglob("*") if item.is_file())
    return {"apparent_bytes": apparent, "allocated_bytes": allocated, "files": files}


def docker_upper(container: str) -> Path:
    status = run_command(
        ["docker", "inspect", "-f", "{{.State.Status}}", container]
    ).stdout.strip()
    if status != "exited":
        raise RuntimeError(
            f"Docker source {container!r} is {status}; a stopped source is required"
        )
    upper = run_command(
        ["docker", "inspect", "-f", "{{.GraphDriver.Data.UpperDir}}", container]
    ).stdout.strip()
    if not upper:
        raise RuntimeError(f"Docker source {container!r} has no overlay upperdir")
    return Path(upper)


def docker_cp_from_daemon(
    helper: str, source: Path, target: Path, host_root: Path, output_root: Path
) -> None:
    relative = source.relative_to(host_root)
    target_relative = target.relative_to(output_root)
    command = (
        "mkdir -p /out/{target} && "
        "cp -a /docker-root/{source}/. /out/{target}/"
    ).format(
        source=str(relative),
        target=str(target_relative),
    )
    run_command(["docker", "exec", helper, "sh", "-lc", command], timeout=1800)


def docker_tree_stats(helper: str, source: Path, host_root: Path) -> dict[str, int]:
    relative = source.relative_to(host_root)
    command = (
        "du -sb /docker-root/{source} && "
        "du -sk /docker-root/{source} && "
        "find /docker-root/{source} -type f | wc -l"
    ).format(source=str(relative))
    output = run_command(
        ["docker", "exec", helper, "sh", "-lc", command],
        timeout=1800,
    ).stdout.splitlines()
    return {
        "apparent_bytes": int(output[0].split()[0]),
        "allocated_bytes": int(output[1].split()[0]) * 1024,
        "files": int(output[2].strip()),
    }


def docker_output_stats(helper: str, target: Path, output_root: Path) -> dict[str, int]:
    relative = target.relative_to(output_root)
    command = (
        "du -sb /out/{target} && "
        "du -sk /out/{target} && "
        "find /out/{target} -type f | wc -l"
    ).format(target=str(relative))
    output = run_command(
        ["docker", "exec", helper, "sh", "-lc", command],
        timeout=1800,
    ).stdout.splitlines()
    return {
        "apparent_bytes": int(output[0].split()[0]),
        "allocated_bytes": int(output[1].split()[0]) * 1024,
        "files": int(output[2].strip()),
    }


def start_helper(run_id: str, host_root: Path, output_root: Path) -> str:
    name = f"cfwa-materialized-helper-{run_id}"
    run_command(
        [
            "docker", "run", "--detach", "--rm", "--privileged",
            "--name", name,
            "-v", f"{host_root}:/docker-root:ro",
            "-v", f"{output_root}:/out",
            "alpine:latest", "sh", "-c", "sleep 86400",
        ],
        timeout=120,
    )
    return name


def ours_snapshot(spec: Any, prefix: str, iteration: int) -> tuple[Any, Any, Path, list[Path]]:
    manager = ours_manager(spec)
    session = manager.prepare_for_task(
        f"{prefix}_{spec.name}_{iteration}_{uuid.uuid4().hex[:8]}",
        ours_task(spec),
    )
    marker = safe_marker(f"{prefix}_{spec.name}_{iteration}")
    add_db_marker(manager, session, marker)
    checkpoint = manager.checkpoint(session, 800000 + iteration)
    backend = manager._mysql_backend(session.agent_id)
    db_source = backend._datadir(
        backend._checkpoint_root(session.agent_id, checkpoint)
    )
    non_db_sources: list[Path] = []
    route = manager.route_registry.resolve_route(session.agent_id)
    for state_backend in manager.non_db_state.backends:
        root_method = getattr(state_backend, "_checkpoint_root", None)
        if root_method is None:
            continue
        try:
            candidate = root_method(
                # Magento's method accepts an identity; MySQL is not in this list.
                type("Identity", (), {
                    "environment_id": session.agent_id,
                    "site": spec.name,
                })(), checkpoint
            )
        except Exception:
            continue
        if Path(candidate).is_dir():
            non_db_sources.append(Path(candidate))
    return manager, session, db_source, non_db_sources


def ours_sample(spec: Any, prefix: str, iteration: int, target_root: Path) -> dict[str, Any]:
    manager, session, db_source, non_db_sources = ours_snapshot(spec, prefix, iteration)
    try:
        target = target_root / f"ours-{spec.name}-{iteration}"
        started = time.perf_counter()
        cp_tree(db_source, target / "db")
        for index, source in enumerate(non_db_sources):
            cp_tree(source, target / f"non_db_{index}")
        seconds = time.perf_counter() - started
        stats = tree_stats(target)
        return {
            "seconds": seconds,
            "source": {"db": tree_stats(db_source), "non_db": [tree_stats(item) for item in non_db_sources]},
            "target": stats,
            "state_valid": stats["files"] >= tree_stats(db_source)["files"],
        }
    finally:
        manager.cleanup(session)


def docker_sample(
    spec: Any,
    prefix: str,
    iteration: int,
    source: Path,
    helper: str,
    host_root: Path,
    target_root: Path,
) -> dict[str, Any]:
    target = target_root / f"docker-{spec.name}-{iteration}"
    source_stats = docker_tree_stats(helper, source, host_root)
    started = time.perf_counter()
    docker_cp_from_daemon(helper, source, target, host_root, target_root)
    seconds = time.perf_counter() - started
    stats = docker_output_stats(helper, target, target_root)
    return {
        "seconds": seconds,
        "source": source_stats,
        "target": stats,
        "state_valid": stats["files"] >= source_stats["files"],
    }


def distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "min": None, "max": None}
    ordered = sorted(values)
    pick = lambda fraction: ordered[int(round((len(ordered) - 1) * fraction))]
    return {"count": len(values), "mean": sum(values) / len(values), "p50": pick(.5), "p95": pick(.95), "min": min(values), "max": max(values)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--sites", default="shopping_admin,shopping")
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    sites = [item.strip() for item in args.sites.split(",") if item.strip()]
    for site in sites:
        if site not in DOCKER_SOURCES:
            raise SystemExit(f"site {site!r} is unsupported; only Shopping MySQL sites are materialized")

    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    output = Path(args.output).resolve()
    work_root = Path(os.environ.get("WEBAGENT_ROOT", ".")) / "results" / f".materialized_clone_{run_id}"
    work_root.mkdir(parents=True, exist_ok=True)
    docker_root_path = Path(docker_root()).resolve()
    helper = start_helper(run_id.replace("_", "-"), docker_root_path, work_root)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "semantics": "snapshot prepared outside timing; both implementations recursively materialize mutable state into a stopped target; no child service startup",
        "docker_sources": DOCKER_SOURCES,
        "host": {"hostname": platform.node(), "platform": platform.platform()},
        "parameters": vars(args),
        "background_before": background_snapshot(),
        "results": {},
    }
    try:
        atomic = output.with_suffix(output.suffix + ".tmp")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        for site in sites:
            payload["results"][site] = {}
            source = docker_upper(DOCKER_SOURCES[site])
            for implementation in ("ours", "docker"):
                samples = []
                for iteration in range(args.iterations):
                    print(f"[materialized-clone] site={site} implementation={implementation} iteration={iteration+1}/{args.iterations}", flush=True)
                    try:
                        if implementation == "ours":
                            sample = ours_sample(SITES[site], run_id, iteration, work_root)
                        else:
                            sample = docker_sample(SITES[site], run_id, iteration, source, helper, docker_root_path, work_root)
                        samples.append(sample)
                    except Exception as error:
                        samples.append({"error": repr(error)})
                        print(f"[materialized-clone] ERROR {error!r}", flush=True)
                good = [item for item in samples if "error" not in item]
                payload["results"][site][implementation] = {
                    "samples": samples,
                    "summary": {
                        "latency_seconds": distribution([item["seconds"] for item in good]),
                        "successes": len(good),
                        "failures": len(samples) - len(good),
                    },
                }
                output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    finally:
        payload["status"] = "complete"
        payload["background_after"] = background_snapshot()
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        # Docker preserves root ownership from the overlay snapshot.  Hand the
        # generated targets back to the invoking user before local cleanup.
        run_command(
            [
                "docker", "exec", helper, "chown", "-R",
                f"{os.getuid()}:{os.getgid()}", "/out",
            ],
            check=False,
            timeout=300,
        )
        run_command(["docker", "rm", "-f", helper], check=False, timeout=120)
        shutil.rmtree(work_root, ignore_errors=True)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
