#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def check(condition: bool, message: str, failures: list[str]) -> None:
    marker = "OK" if condition else "FAIL"
    print(f"[{marker}] {message}")
    if not condition:
        failures.append(message)


def container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def main() -> int:
    xfs_base = Path(
        os.environ.get(
            "MYSQL_XFS_BASE_PATH",
            "/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs",
        )
    )
    template = xfs_base / "magento_template"
    admin_template = xfs_base / "magento_admin_template"
    failures: list[str] = []
    check(shutil.which("docker") is not None, "Docker CLI is installed", failures)
    fs_type = ""
    if xfs_base.is_dir():
        result = subprocess.run(
            ["stat", "-f", "-c", "%T", str(xfs_base)],
            capture_output=True,
            text=True,
            check=False,
        )
        fs_type = result.stdout.strip()
    check(xfs_base.is_dir() and fs_type == "xfs", f"XFS base: {xfs_base}", failures)
    check(
        (template / ".web-agent-mysql-template-ready").is_file(),
        "quiesced MariaDB template marker",
        failures,
    )
    check(
        (admin_template / ".web-agent-mysql-template-ready").is_file(),
        "quiesced Shopping Admin MariaDB template marker",
        failures,
    )
    check(
        (admin_template / ".web-agent-magento-state-ready").is_file(),
        "Shopping Admin media/session template marker",
        failures,
    )
    check(
        (template / ".web-agent-magento-state-ready").is_file(),
        "Magento media/session template marker",
        failures,
    )
    check(
        container_running(
            os.environ.get("MYSQL_RUNTIME_CONTAINER", "shopping-mysql-runtime")
        ),
        "shared MariaDB binary runtime",
        failures,
    )
    shared = os.environ.get("MAGENTO_SHARED_CONTAINER", "shared-shopping")
    check(container_running(shared), "shared routed Magento runtime", failures)
    if container_running(shared):
        process = subprocess.run(
            ["docker", "exec", shared, "sh", "-lc", "ps auxww"],
            capture_output=True,
            text=True,
            check=False,
        )
        check(
            "mysqld --user=mysql" not in process.stdout,
            "embedded Magento mysqld is disabled",
            failures,
        )
        check(
            "crond -f" not in process.stdout,
            "unscoped Magento cron is disabled",
            failures,
        )
        lint = subprocess.run(
            [
                "docker",
                "exec",
                shared,
                "php",
                "-l",
                "/opt/web-agent-magento/web_agent_magento_route.php",
            ],
            capture_output=True,
            check=False,
        )
        check(lint.returncode == 0, "Magento route adapter PHP syntax", failures)

    for relative in (
        "experiments/state_audits/shopping.json",
        "experiments/state_audits/shopping_admin.json",
        "experiments/shopping/shopping_task_contracts.json",
    ):
        path = PROJECT_ROOT / relative
        try:
            json.loads(path.read_text())
            valid = True
        except (OSError, json.JSONDecodeError):
            valid = False
        check(valid, f"valid contract/audit: {relative}", failures)

    registry_url = os.environ.get(
        "WEB_AGENT_ROUTE_REGISTRY_HEALTH", "http://127.0.0.1:8766/health"
    )
    try:
        with urllib.request.urlopen(registry_url, timeout=2) as response:
            registry_ok = response.status == 200
    except OSError:
        registry_ok = False
    check(registry_ok, f"route registry health: {registry_url}", failures)

    if failures:
        print(f"\nShopping DB isolation is NOT ready ({len(failures)} failed checks).")
        return 1
    print("\nShopping DB isolation prerequisites are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
