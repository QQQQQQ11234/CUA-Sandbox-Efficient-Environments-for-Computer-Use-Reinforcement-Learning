#!/usr/bin/env python3
"""Fair stopped-state clone benchmark for WebArena.

Snapshot preparation is outside the timed interval.  The timed operation only
copies state into a stopped child: Docker uses ``docker create`` from a
committed image; CUA-Sandbox uses the DB checkpoint and non-DB CoW backends without
registering, activating, or starting the child.  Child validation is performed
after timing (and may start only the isolated MySQL process for a DB check).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from experiments.system.benchmark_environment_metrics import (
    SITES,
    SiteSpec,
    atomic_json,
    ours_manager,
    ours_task,
    start_docker,
)
from experiments.system.benchmark_lifecycle_efficiency import (
    StorageSampler,
    add_db_marker,
    add_docker_marker,
    background_snapshot,
    db_marker_exists,
    docker_root,
    marker_path,
    run_command,
    safe_marker,
)
from rl_web_agent.isolation.non_db_state import StateIdentity
from rl_web_agent.isolation.mysql_xfs_isolation import MySQLBranchResource


def timed(storage_path: str, interval: float, action: Any) -> dict[str, Any]:
    sampler = StorageSampler(storage_path, interval)
    sampler.start()
    started = time.perf_counter()
    try:
        details = action()
        seconds = time.perf_counter() - started
    finally:
        storage = sampler.finish()
    return {"seconds": seconds, "storage": storage, **details}


def mysql_child_check(manager: Any, session: Any, checkpoint: str,
                      target_branch: str, marker: str) -> dict[str, Any]:
    backend = manager._mysql_backend(session.agent_id)
    source = backend._datadir(
        backend._checkpoint_root(session.agent_id, checkpoint)
    )
    target = backend._datadir(
        backend._branch_root(session.agent_id, target_branch)
    )
    if not target.is_dir():
        raise FileNotFoundError(f"stopped child datadir is missing: {target}")
    child = MySQLBranchResource(
        environment_id=session.agent_id,
        branch_id=target_branch,
        datadir=str(target),
        host=backend.advertised_host,
        port=backend._allocate_port(session.agent_id, target_branch),
        database_name=backend.mysql_database,
    )
    try:
        # Validation is intentionally outside the timed interval.  It starts
        # only the child MySQL process, never the shared web application.
        child = backend._start(child)
        # Query the child port directly instead of relying on route publication.
        command = backend._runtime_command([
            "mysql", "--protocol=tcp", f"--host={backend.admin_host}",
            f"--port={child.port}", f"--user={backend.mysql_user}",
        ])
        if backend.mysql_password:
            command.append(f"--password={backend.mysql_password}")
        command.extend([
            backend.mysql_database, "--batch", "--skip-column-names", "-e",
            f"SELECT COUNT(*) FROM {MARKER_TABLE} WHERE marker='{safe_marker(marker)}';",
        ])
        checked = run_command(command, check=False, timeout=30)
        valid = checked.returncode == 0 and checked.stdout.strip().splitlines()[-1:] == ["1"]
        return {"state_valid": bool(valid), "child_port": child.port}
    finally:
        backend._stop(child)


MARKER_TABLE = "web_agent_lifecycle_probe"


def ours_sample(spec: SiteSpec, prefix: str, iteration: int,
                storage_path: str, interval: float, mode: str) -> dict[str, Any]:
    manager = ours_manager(spec)
    session = manager.prepare_for_task(
        f"{prefix}_{spec.name}_{iteration}_{uuid.uuid4().hex[:8]}",
        ours_task(spec),
    )
    marker = safe_marker(f"{prefix}_{spec.name}_{iteration}")
    checkpoint = "fair_clone_" + str(iteration)
    target_branch = "fair_child_" + str(iteration)
    try:
        add_db_marker(manager, session, marker)
        # Snapshot creation is explicitly excluded from clone timing.
        checkpoint = manager.checkpoint(session, 700000 + iteration)
        route = manager.route_registry.resolve_route(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id=target_branch,
            generation=source_identity.generation + 1,
            database_name=source_identity.database_name,
        )

        metadata_path = Path("/tmp") / (
            "cua-sandbox-lazy-clone-" + safe_marker(f"{prefix}_{spec.name}_{iteration}") + ".json"
        )

        def clone() -> dict[str, Any]:
            if mode == "metadata":
                # Metadata-only/lazy-CoW prototype: the child is a handle to an
                # immutable checkpoint.  No DB or filesystem payload is copied.
                payload = {
                    "mode": "metadata-only",
                    "checkpoint": checkpoint,
                    "source": source_identity.__dict__,
                    "target": target_identity.__dict__,
                }
                metadata_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                return {"state_valid": True, "stopped_child": True, "mode": mode}
            if session.mysql_datadir is not None:
                backend = manager._mysql_backend(session.agent_id)
                source = backend._datadir(
                    backend._checkpoint_root(session.agent_id, checkpoint)
                )
                target = backend._datadir(
                    backend._branch_root(session.agent_id, target_branch)
                )
                backend._clone_tree(source, target)
            else:
                target_db = manager._branch_db_name(session.agent_id, target_branch)
                manager.drop_db(target_db)
                manager.clone_db(checkpoint, target_db)
                manager._register_owned_db(session.agent_id, target_db)
            # Copy Magento/GitLab/overlay state only; do not activate a route.
            manager.non_db_state.fork(source_identity, checkpoint, target_identity)
            return {"state_valid": True, "stopped_child": True}

        result = timed(storage_path, interval, clone)
        if mode == "metadata":
            validation = {
                "state_valid": metadata_path.exists()
                and json.loads(metadata_path.read_text(encoding="utf-8"))["checkpoint"] == checkpoint,
                "metadata_only": True,
            }
        elif session.mysql_datadir is not None:
            validation = mysql_child_check(manager, session, checkpoint, target_branch, marker)
        else:
            target_db = manager._branch_db_name(session.agent_id, target_branch)
            query = (
                f"SELECT COUNT(*) FROM {MARKER_TABLE} WHERE marker='{marker}';"
            )
            if manager.admin_docker_container:
                checked = run_command([
                    "docker", "exec", manager.admin_docker_container,
                    manager.admin_docker_psql_command, "-d", target_db,
                    "-At", "-v", "ON_ERROR_STOP=1", "-c", query,
                ], check=False, timeout=30)
                valid = checked.returncode == 0 and checked.stdout.strip().splitlines()[-1:] == ["1"]
            else:
                import psycopg  # type: ignore
                try:
                    with psycopg.connect(manager.admin_dsn, dbname=target_db, autocommit=True) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute(query)
                            valid = str(cursor.fetchone()[0]) == "1"
                except Exception:
                    valid = False
            validation = {"state_valid": valid}
        result.update({"checkpoint": checkpoint, "validation": validation, "mode": mode})
        if not validation["state_valid"]:
            raise AssertionError("stopped child failed state validation")
        return result
    finally:
        if "metadata_path" in locals():
            metadata_path.unlink(missing_ok=True)
        manager.cleanup(session)


def snapshot_image_name(prefix: str, spec: SiteSpec, iteration: int) -> str:
    return f"cfwa-fair-snapshot-{prefix}-{spec.name}-{iteration}:latest"


def docker_sample(spec: SiteSpec, prefix: str, iteration: int,
                  storage_path: str, interval: float) -> dict[str, Any]:
    source = f"cfwa-fair-{prefix}-{spec.name}-{iteration}-source"
    child = f"cfwa-fair-{prefix}-{spec.name}-{iteration}-child"
    image = snapshot_image_name(prefix, spec, iteration)
    marker = safe_marker(f"{prefix}_{spec.name}_{iteration}")
    marker_file = marker_path(marker)
    try:
        start_docker(source, spec)
        add_docker_marker(source, marker)
        # Quiesce source and prepare the Docker snapshot before timing.
        run_command(["docker", "stop", "-t", "30", source], timeout=60)
        run_command(["docker", "commit", source, image], timeout=900)

        def clone() -> dict[str, Any]:
            run_command(["docker", "create", "--name", child, image], timeout=120)
            state = run_command(
                ["docker", "inspect", "-f", "{{.State.Running}}", child]
            ).stdout.strip()
            if state != "false":
                raise AssertionError(f"Docker child is not stopped: {state}")
            return {"state_valid": True, "stopped_child": True}

        result = timed(storage_path, interval, clone)
        # docker cp works on a stopped container and does not start its app.
        host_marker = Path("/tmp") / f"{child}-marker"
        copied = run_command(
            ["docker", "cp", f"{child}:{marker_file}", str(host_marker)],
            check=False, timeout=60,
        )
        validation = {"state_valid": copied.returncode == 0 and host_marker.exists()}
        host_marker.unlink(missing_ok=True)
        result.update({"snapshot_image": image, "validation": validation})
        if not validation["state_valid"]:
            raise AssertionError("Docker stopped child failed state validation")
        return result
    finally:
        run_command(["docker", "rm", "-f", source], check=False, timeout=120)
        run_command(["docker", "rm", "-f", child], check=False, timeout=120)
        run_command(["docker", "image", "rm", "--force", image], check=False, timeout=180)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "min": None, "max": None}
    ordered = sorted(values)
    p = lambda f: ordered[int(round((len(ordered) - 1) * f))]
    return {"count": len(values), "mean": sum(values) / len(values), "p50": p(.5), "p95": p(.95), "min": min(values), "max": max(values)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--sites", default="shopping_admin,shopping,gitlab")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--ours-storage-path", default="/var/lib/web-agent")
    parser.add_argument("--storage-sample-interval", type=float, default=.05)
    parser.add_argument(
        "--mode", choices=("materialized", "metadata"), default="materialized",
        help="materialize state (legacy) or create a lazy metadata-only child handle",
    )
    args = parser.parse_args()
    sites = [item.strip() for item in args.sites.split(",") if item.strip()]
    for site in sites:
        if site not in SITES:
            raise SystemExit(f"unknown site: {site}")
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    prefix = "".join(ch if ch.isalnum() else "-" for ch in run_id)
    output = Path(args.output).resolve()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "semantics": (
            "metadata-only lazy child handle; no checkpoint payload or app startup "
            "is timed"
            if args.mode == "metadata" else
            "snapshot prepared outside timing; timed operation creates a stopped "
            "child only; no app/desktop startup"
        ),
        "host": {"hostname": platform.node(), "platform": platform.platform(), "cpu_visible": os.cpu_count()},
        "parameters": vars(args), "background_before": background_snapshot(), "results": {},
    }
    atomic_json(output, payload)
    try:
        for site in sites:
            payload["results"][site] = {}
            for implementation in ("ours", "docker"):
                samples = []
                for iteration in range(args.iterations):
                    print(f"[fair-clone] site={site} implementation={implementation} iteration={iteration+1}/{args.iterations}", flush=True)
                    try:
                        if implementation == "ours":
                            sample = ours_sample(SITES[site], prefix, iteration, args.ours_storage_path, args.storage_sample_interval, args.mode)
                        else:
                            sample = docker_sample(SITES[site], prefix, iteration, docker_root(), args.storage_sample_interval)
                        samples.append(sample)
                    except Exception as error:
                        samples.append({"error": repr(error)})
                        print(f"[fair-clone] ERROR {error!r}", flush=True)
                good = [item for item in samples if "error" not in item]
                payload["results"][site][implementation] = {
                    "samples": samples,
                    "summary": {"latency_seconds": distribution([item["seconds"] for item in good]), "successes": len(good), "failures": len(samples)-len(good)},
                    "native": True,
                }
                atomic_json(output, payload)
    finally:
        payload["status"] = "complete"
        payload["background_after"] = background_snapshot()
        atomic_json(output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
