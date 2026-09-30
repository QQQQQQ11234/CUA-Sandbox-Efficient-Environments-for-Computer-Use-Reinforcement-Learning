#!/usr/bin/env python3
"""Ablate CUA-Sandbox's non-DB state plane on WebArena Shopping.

The full configuration routes both MySQL and Magento's non-DB state (cache,
search/session namespaces, lifecycle hooks) per environment.  The ablated
configuration keeps the same shared application, MySQL branch, route token,
and benchmark task, but disables the non-DB state backends.  This isolates the
cost of the task-aware state adapters; it is intentionally run only on the
read-only WebArena task 21 and is not claimed as an isolation result for
mutation-heavy tasks.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from experiments.shopping.run_official_storefront_db_eval import storefront_db_config
from experiments.system.benchmark_environment_metrics import (
    container_env_value,
    http_probe,
    ours_probe,
)
from rl_web_agent.isolation.db_isolation import DBIsolationManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def manager_for(non_db_state: bool) -> DBIsolationManager:
    os.environ["ROUTE_TOKEN_SECRET"] = container_env_value(
        "shared-shopping-admin", "WEB_AGENT_ROUTE_TOKEN_SECRET"
    )
    os.environ.setdefault(
        "ROUTE_REGISTRY_PATH", str(PROJECT_ROOT / "runtime/shopping_db_routes.sqlite3")
    )
    config = storefront_db_config()
    config.non_db_state.enabled = bool(non_db_state)
    if non_db_state:
        config.non_db_state.runtime_root = str(
            PROJECT_ROOT / "runtime/non_db_state_ablation_full"
        )
    return DBIsolationManager(config, type("Logger", (), {"info": print, "warning": print, "exception": print})())


def one_sample(non_db_state: bool, iteration: int) -> dict[str, Any]:
    manager = manager_for(non_db_state)
    env_id = f"ablation-nondb-{int(time.time())}-{iteration}-{uuid.uuid4().hex[:8]}"
    task = {"task_id": 21, "sites": ["shopping"]}
    started = time.perf_counter()
    session = manager.prepare_for_task(env_id, task)
    prepare_seconds = time.perf_counter() - started
    try:
        first = ours_probe(
            type("Spec", (), {"ours_url": "http://127.0.0.1:7770/"})(), session
        )
        started = time.perf_counter()
        manager.reset(session)
        reset_seconds = time.perf_counter() - started
        after_reset = ours_probe(
            type("Spec", (), {"ours_url": "http://127.0.0.1:7770/"})(), session
        )
        return {
            "prepare_seconds": prepare_seconds,
            "reset_seconds": reset_seconds,
            "probe_before_reset": first,
            "probe_after_reset": after_reset,
            "non_db_state": non_db_state,
        }
    finally:
        manager.cleanup(session)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "task_id": 21,
        "status": "running",
        "variants": {},
        "parameters": vars(args),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        for label, enabled in (("full_non_db_state", True), ("db_only_ablation", False)):
            print(f"[non-db-ablation] variant={label}", flush=True)
            samples = [one_sample(enabled, i) for i in range(args.iterations)]
            payload["variants"][label] = {"samples": samples}
            output.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception as error:
        payload["status"] = "failed"
        payload["error"] = repr(error)
        output.write_text(json.dumps(payload, indent=2) + "\n")
        raise
    payload["status"] = "complete"
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
