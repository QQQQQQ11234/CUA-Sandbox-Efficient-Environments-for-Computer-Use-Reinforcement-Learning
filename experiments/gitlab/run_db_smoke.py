from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from hydra import compose, initialize_config_dir

from rl_web_agent.env import WebAgentEnv


async def run(args: argparse.Namespace) -> dict[str, object]:
    with urllib.request.urlopen(args.registry_health_url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("route registry is not healthy")

    runtime_config = json.loads(Path(args.runtime_config).read_text())
    task = json.loads(Path(args.task).read_text())
    task = json.loads(
        json.dumps(task).replace(args.task_gitlab_url, args.shared_gitlab_url)
    )

    overrides = [
        "environment.isolation.mode=db",
        f"environment.db_isolation.base_template_db={args.template_db}",
        f"environment.db_isolation.clone_strategy={args.clone_strategy}",
        f"environment.db_isolation.route_token_secret={runtime_config['route_secret']}",
        f"environment.db_isolation.route_registry_path={args.route_registry_path}",
        f"environment.db_isolation.shared_site_hosts.gitlab={args.shared_gitlab_host}",
        f"environment.db_isolation.non_db_state.gitlab_state.docker_container={args.gitlab_container}",
        f"environment.sites.gitlab={args.shared_gitlab_host}",
        "environment.proxy.enabled=false",
        "environment.db_isolation.drop_on_close=true",
        "environment.tracing.enabled=false",
        "environment.browser.timeouts.page_load_networkidle=15000",
    ]
    if args.admin_dsn:
        overrides.append(f"environment.db_isolation.admin_dsn={args.admin_dsn}")
        overrides.append("environment.db_isolation.admin_docker_container=")
    else:
        overrides.append(
            f"environment.db_isolation.admin_docker_container={args.gitlab_container}"
        )
    project_root = Path(__file__).resolve().parents[2]
    with initialize_config_dir(version_base=None, config_dir=str(project_root)):
        config = compose(config_name="config", overrides=overrides)

    with tempfile.TemporaryDirectory(prefix="gitlab_smoke_userdata_") as user_data:
        with tempfile.TemporaryDirectory(prefix="gitlab_smoke_cache_") as cache:
            config.environment.browser.user_data_dir = user_data
            config.environment.browser.cache_dir = cache
            environment = WebAgentEnv(config.environment)
            setup_started = time.perf_counter()
            try:
                await environment.setup(task)
                setup_seconds = time.perf_counter() - setup_started
                database_name = environment.db_agent_session.db_name
                await environment.page.goto(
                    f"{args.shared_gitlab_url}/-/profile",
                    wait_until="domcontentloaded",
                )
                update = await environment.page.evaluate(
                    """
                    async (message) => {
                      const form = document.querySelector('form.edit-user');
                      if (!form) throw new Error('profile form not found');
                      const body = new FormData(form);
                      body.set('user[status][emoji]', 'speech_balloon');
                      body.set('user[status][message]', message);
                      body.set('user[status][availability]', 'not_set');
                      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
                      const response = await fetch(form.action, {
                        method: 'PUT',
                        body,
                        headers: {
                          Accept: 'application/json',
                          'X-CSRF-Token': csrf,
                        },
                      });
                      return {status: response.status, body: await response.text()};
                    }
                    """,
                    args.status,
                )
                reward = await environment.evaluate_task()
                return {
                    "task_id": task["task_id"],
                    "intent": task["intent"],
                    "database": database_name,
                    "setup_seconds": round(setup_seconds, 3),
                    "gitlab_update_status": update["status"],
                    "gitlab_update_body": update["body"],
                    "reward": reward,
                    "success": reward == 1.0,
                }
            finally:
                await environment.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic task 418 through DB isolation and WebArena reward"
    )
    parser.add_argument("--task", default="runtime/gitlab_db_eval_tasks/418.json")
    parser.add_argument("--output", default="results/gitlab_db_smoke_418.json")
    parser.add_argument(
        "--runtime-config", default="runtime/gitlab_web_agent_config.json"
    )
    parser.add_argument("--gitlab-container", default="shared-gitlab-test")
    parser.add_argument("--admin-dsn", default=os.environ.get("DB_ADMIN_DSN", ""))
    parser.add_argument("--template-db", default="gitlab_base_template")
    parser.add_argument(
        "--route-registry-path", default="runtime/db_routes_nondb.sqlite3"
    )
    parser.add_argument("--clone-strategy", default="FILE_COPY")
    parser.add_argument(
        "--task-gitlab-url", default="http://metis.lti.cs.cmu.edu:8023"
    )
    parser.add_argument("--shared-gitlab-url", default="http://127.0.0.1:8023")
    parser.add_argument("--shared-gitlab-host", default="127.0.0.1:8023")
    parser.add_argument(
        "--registry-health-url", default="http://127.0.0.1:8765/health"
    )
    parser.add_argument("--status", default="Busy")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
