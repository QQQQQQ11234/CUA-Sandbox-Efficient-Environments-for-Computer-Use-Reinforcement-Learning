#!/usr/bin/env python3
"""Ablate the state-capsule copy primitive used by CUA-Sandbox.

This benchmark keeps the application out of the measurement and compares the
two storage primitives used to materialize the same MySQL capsule:

* reflink: XFS copy-on-write (the CUA-Sandbox implementation);
* eager: ordinary full file copy (the ablated implementation).

The source is the immutable Magento template used by the running Shopping
stack.  For checkpoint/fork, preparation of the source checkpoint is outside
the timed interval; the timed interval measures only the lifecycle operation.
No shared container, route, or live environment is modified.
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
from statistics import fmean
from typing import Any


def run(command: list[str], *, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def used_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return (stat.f_blocks - stat.f_bfree) * stat.f_frsize


def tree_bytes(path: Path) -> dict[str, int]:
    allocated = int(run(["du", "-s", "-B1", str(path)]).stdout.split()[0])
    apparent = int(run(["du", "-s", "-B1", "--apparent-size", str(path)]).stdout.split()[0])
    return {"allocated_bytes": allocated, "apparent_bytes": apparent}


def sync(path: Path) -> None:
    run(["sync", "-f", str(path)], timeout=120)


def copy_tree(source: Path, target: Path, mode: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    flag = "always" if mode == "reflink" else "never"
    run(["cp", "-a", f"--reflink={flag}", f"{source}/.", str(target)], timeout=1800)


def timed_copy(source: Path, target: Path, mode: str, storage_root: Path) -> dict[str, Any]:
    sync(storage_root)
    before_used = used_bytes(storage_root)
    started = time.perf_counter()
    copy_tree(source, target, mode)
    sync(storage_root)
    return {
        "seconds": time.perf_counter() - started,
        "physical_delta_bytes": used_bytes(storage_root) - before_used,
        "tree": tree_bytes(target),
    }


def one_sample(mode: str, template: Path, root: Path, iteration: int) -> dict[str, Any]:
    prefix = root / f"{mode}-{iteration}-{uuid.uuid4().hex[:8]}"
    env = prefix / "environment"
    checkpoint = prefix / "checkpoint"
    child = prefix / "child"
    records: dict[str, Any] = {}
    try:
        records["create"] = timed_copy(template, env, mode, root)

        # Source preparation is intentionally outside the checkpoint interval.
        checkpoint.mkdir(parents=True, exist_ok=True)
        records["checkpoint"] = timed_copy(env, checkpoint, mode, root)

        # Fork measures one independent descendant from the prepared checkpoint.
        records["fork"] = timed_copy(checkpoint, child, mode, root)

        # Reset measures discard + rebuild from the immutable template.
        sync(root)
        before_used = used_bytes(root)
        started = time.perf_counter()
        shutil.rmtree(env)
        copy_tree(template, env, mode)
        sync(root)
        records["reset"] = {
            "seconds": time.perf_counter() - started,
            "physical_delta_bytes": used_bytes(root) - before_used,
            "tree": tree_bytes(env),
        }
        return records
    finally:
        if prefix.exists():
            shutil.rmtree(prefix)


def summarize(samples: list[dict[str, Any]], operation: str) -> dict[str, Any]:
    values = [s[operation] for s in samples]
    return {
        "count": len(values),
        "seconds_mean": fmean(v["seconds"] for v in values),
        "seconds": [v["seconds"] for v in values],
        "physical_delta_bytes_mean": fmean(v["physical_delta_bytes"] for v in values),
        "physical_delta_bytes": [v["physical_delta_bytes"] for v in values],
        "tree": values[-1]["tree"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template",
        default="/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs/magento_template/mysql",
    )
    parser.add_argument(
        "--storage-root",
        default="/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs",
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    template = Path(args.template).resolve()
    root = Path(args.storage_root).resolve()
    if not template.is_dir() or root not in template.parents:
        raise SystemExit(f"template must be a directory below storage root: {template}")
    if args.iterations < 1:
        raise SystemExit("--iterations must be positive")

    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "host": {"hostname": platform.node(), "platform": platform.platform()},
        "template": str(template),
        "template_tree": tree_bytes(template),
        "parameters": vars(args),
        "results": {},
    }
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")

    try:
        for mode in ("reflink", "eager"):
            samples = []
            for iteration in range(args.iterations):
                print(f"[capsule-ablation] mode={mode} iteration={iteration + 1}/{args.iterations}", flush=True)
                samples.append(one_sample(mode, template, root, iteration))
            payload["results"][mode] = {
                operation: summarize(samples, operation)
                for operation in ("create", "checkpoint", "fork", "reset")
            }
            payload["results"][mode]["samples"] = samples
            out.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception as error:
        payload["status"] = "failed"
        payload["error"] = repr(error)
        out.write_text(json.dumps(payload, indent=2) + "\n")
        raise
    payload["status"] = "complete"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
