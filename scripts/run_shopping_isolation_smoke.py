#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import urllib.request
import uuid
from pathlib import Path

from omegaconf import OmegaConf

from rl_web_agent.isolation.db_isolation import DBIsolationManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=("shopping", "shopping_admin"), required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--target", default="http://127.0.0.1:7772")
    parser.add_argument("--full-lifecycle", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ROUTE_TOKEN_SECRET"):
        raise RuntimeError("ROUTE_TOKEN_SECRET is required")
    tasks = json.loads(
        (PROJECT_ROOT / "thirdparty/webarena/config_files/test.raw.json").read_text()
    )
    task = next(task for task in tasks if int(task["task_id"]) == args.task_id)
    if task.get("sites") != [args.site]:
        raise ValueError(f"task {args.task_id} does not belong to {args.site}")

    root = OmegaConf.load(PROJECT_ROOT / "rl_web_agent/conf/base.yaml")
    config = root.environment.db_isolation
    manager = DBIsolationManager(config, logging.getLogger("shopping-smoke"))
    session = manager.prepare_for_task(f"smoke_{uuid.uuid4().hex[:12]}", task)
    try:
        if session.mysql_port is None:
            raise AssertionError("Shopping session did not receive a MySQL endpoint")
        query = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                f"mysql:{os.getgid()}",
                os.environ.get("MYSQL_RUNTIME_CONTAINER", "shopping-mysql-runtime"),
                "mysql",
                "--protocol=tcp",
                "--host=172.17.0.1",
                f"--port={session.mysql_port}",
                "--user=magentouser",
                "--password=MyPassword",
                "--batch",
                "--skip-column-names",
                "magentodb",
                "-e",
                "SELECT COUNT(*) FROM catalog_product_entity",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        product_count = int(query.stdout.strip())
        if product_count <= 0:
            raise AssertionError("routed Magento database is empty")

        path = "/admin" if args.site == "shopping_admin" else "/"
        original_host = (
            "metis.lti.cs.cmu.edu:7780"
            if args.site == "shopping_admin"
            else "metis.lti.cs.cmu.edu:7770"
        )
        request = urllib.request.Request(
            args.target.rstrip("/") + path,
            headers={**session.headers, "Host": original_host},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status >= 400:
                raise AssertionError(f"shared Magento returned HTTP {response.status}")

        if args.full_lifecycle:
            checkpoint = manager.checkpoint(session, 1)
            manager.fork(session, checkpoint, "smoke_child")
            manager.reset(session)
        print(
            json.dumps(
                {
                    "site": args.site,
                    "task_id": args.task_id,
                    "product_count": product_count,
                    "mysql_port": session.mysql_port,
                    "http": "ok",
                    "full_lifecycle": args.full_lifecycle,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        manager.cleanup(session)


if __name__ == "__main__":
    raise SystemExit(main())
