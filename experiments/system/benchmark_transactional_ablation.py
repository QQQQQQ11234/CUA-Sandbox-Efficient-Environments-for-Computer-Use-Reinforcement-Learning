#!/usr/bin/env python3
"""Stress the transactional route lifecycle and an unsafe publication ablation.

The safe variant uses the production SQLiteRouteRegistry freeze/drain/activate
protocol.  The ablated variant directly replaces the route row while a lease
is in flight, modelling publication without a drain barrier.  This is a
method-level safety experiment, not a Docker efficiency benchmark.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from rl_web_agent.isolation.route_registry import (
    ACTIVE,
    FROZEN,
    RouteBusyError,
    RouteFrozenError,
    SQLiteRouteRegistry,
)


def safe_trial(index: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        registry = SQLiteRouteRegistry(str(Path(directory) / "routes.sqlite3"))
        agent = f"safe-{index}"
        registry.set_route(agent, "db-root", environment_id=agent, site="shopping")
        lease = registry.acquire(agent)
        old_generation = lease.generation
        registry.freeze(agent)
        frozen_reject = False
        try:
            registry.acquire(agent)
        except RouteFrozenError:
            frozen_reject = True
        busy_block = False
        try:
            registry.activate(agent, db_name="db-next", generation=old_generation + 1)
        except RouteBusyError:
            busy_block = True
        registry.release(agent)
        drained = registry.wait_for_drained(agent, timeout_seconds=0.2)
        current = registry.activate(agent, db_name="db-next", generation=old_generation + 1)
        return {
            "frozen_reject": frozen_reject,
            "busy_activation_blocked": busy_block,
            "drained_before_publish": drained.inflight_requests == 0,
            "published_generation": current.generation,
            "published_db": current.db_name,
            "lease_generation": old_generation,
            "mixed_generation": old_generation == current.generation,
        }


def unsafe_trial(index: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "routes.sqlite3"
        registry = SQLiteRouteRegistry(str(path))
        agent = f"unsafe-{index}"
        registry.set_route(agent, "db-root", environment_id=agent, site="shopping")
        lease = registry.acquire(agent)
        old_generation = lease.generation
        # Deliberately bypass freeze/drain/activate to model non-transactional
        # publication while the old request still holds a lease.
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE agent_routes SET db_name=?, generation=?, lifecycle_state=? WHERE agent_id=?",
                ("db-next", old_generation + 1, ACTIVE, agent),
            )
        current = registry.resolve_route(agent)
        return {
            "lease_generation": old_generation,
            "published_generation": current.generation,
            "published_db": current.db_name,
            "inflight_at_publish": current.inflight_requests,
            "mixed_generation": old_generation != current.generation,
            "unsafe_active_publish": current.lifecycle_state == ACTIVE and current.inflight_requests > 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload: dict[str, Any] = {"schema_version": 1, "status": "running", "parameters": vars(args)}
    safe = [safe_trial(i) for i in range(args.trials)]
    unsafe = [unsafe_trial(i) for i in range(args.trials)]
    payload["safe_transactional"] = {
        "trials": len(safe),
        "frozen_rejects": sum(x["frozen_reject"] for x in safe),
        "busy_activation_blocks": sum(x["busy_activation_blocked"] for x in safe),
        "drain_before_publish": sum(x["drained_before_publish"] for x in safe),
        "mixed_generations": sum(x["mixed_generation"] for x in safe),
    }
    payload["unsafe_direct_publish"] = {
        "trials": len(unsafe),
        "mixed_generations": sum(x["mixed_generation"] for x in unsafe),
        "active_publish_with_inflight": sum(x["unsafe_active_publish"] for x in unsafe),
    }
    payload["status"] = "complete"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
