#!/usr/bin/env python3
"""Run the official WebArena evaluation stack with CUA-Sandbox DB isolation.

The benchmark agent, accessibility-tree observation, action parser, prompt,
early-stop logic, renderer, and evaluators are imported directly from the
official checkout.  This file only replaces per-task website isolation and
authentication so requests carry the signed DB route capability.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from omegaconf import OmegaConf


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEBARENA_ROOT = Path(os.environ.get("WEBARENA_ROOT", "/path/to/webarena"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Official WebArena GitLab eval with DB/reflink isolation"
    )
    parser.add_argument("--task_ids_file", required=True)
    parser.add_argument(
        "--tasks_dir", default=str(PROJECT_ROOT / "runtime/gitlab_eval_tasks")
    )
    parser.add_argument("--result_dir", required=True)
    parser.add_argument(
        "--webarena_root", default=str(DEFAULT_WEBARENA_ROOT)
    )
    parser.add_argument(
        "--instruction_path",
        default="agent/prompts/jsons/p_cot_id_actree_2s.json",
    )
    parser.add_argument("--model", default="Qwen/Qwen3.5-9B")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--max_tokens", type=int, default=384)
    parser.add_argument("--max_steps", type=int, default=30)
    parser.add_argument("--max_retry", type=int, default=1)
    parser.add_argument("--max_obs_length", type=int, default=1920)
    parser.add_argument("--reasoning_effort", default=None)
    return parser.parse_args()


def db_config():
    return OmegaConf.create(
        {
            "admin_dsn": os.getenv(
                "DB_ADMIN_DSN",
                "postgresql://postgres:postgres@127.0.0.1:55433/postgres",
            ),
            "admin_docker_container": "",
            "admin_docker_psql_command": "gitlab-psql",
            "base_template_db": os.getenv(
                "BASE_TEMPLATE_DB", "gitlab_base_template"
            ),
            "clone_strategy": os.getenv("DB_CLONE_STRATEGY", "FILE_COPY"),
            "agent_db_prefix": "agent_",
            "agent_db_suffix": "_db",
            "route_token_header": "X-Agent-Route",
            "route_token_secret": os.environ["ROUTE_TOKEN_SECRET"],
            "route_token_ttl_seconds": 86400,
            "checkpoint_tag": "_ckpt_",
            "branch_tag": "_branch_",
            "route_registry_path": os.getenv(
                "ROUTE_REGISTRY_PATH",
                str(PROJECT_ROOT / "runtime/db_routes_nondb.sqlite3"),
            ),
            "reset_on_setup": True,
            "drop_on_close": True,
            "route_drain_timeout_seconds": 30,
            "evaluation_drain_timeout_seconds": 30,
            "rails_pool_release_enabled": True,
            "rails_pool_release_timeout_seconds": 2,
            "rails_pool_release_path": "/__web_agent_release_pool",
            "state_audit": {
                "enabled": True,
                "audit_paths": [
                    str(PROJECT_ROOT / "experiments/state_audits/gitlab.json")
                ],
                "task_manifest_paths": [
                    str(
                        PROJECT_ROOT
                        / "experiments/gitlab/gitlab_task_contracts.json"
                    )
                ],
            },
            "non_db_state": {
                "enabled": True,
                "runtime_root": str(PROJECT_ROOT / "runtime/non_db_state"),
                "worker_mode": "shared",
                "overlay": {"enabled": False},
                "gitlab_state": {
                    "enabled": True,
                    "docker_container": os.getenv(
                        "GITLAB_SHARED_CONTAINER", "shared-gitlab-nondb"
                    ),
                    "statectl_path": "/opt/web-agent/gitlab_statectl.rb",
                    "timeout_seconds": 300,
                },
                "command_hooks": [],
            },
            "shared_site_hosts": {
                "gitlab": os.getenv("GITLAB_TARGET", "127.0.0.1:8023")
            },
            "mysql_xfs": {"enabled": False},
        }
    )


def install_official_imports(webarena_root: Path):
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(webarena_root))
    os.chdir(webarena_root)

    import run as official_run
    from browser_env.envs import ScriptBrowserEnv
    from browser_env.utils import DetachedPage
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    from rl_web_agent.isolation import DBAgentSession, DBIsolationManager

    return (
        official_run,
        ScriptBrowserEnv,
        DetachedPage,
        sync_playwright,
        PlaywrightTimeoutError,
        DBAgentSession,
        DBIsolationManager,
    )


def build_db_environment(
    base_environment,
    sync_playwright_fn,
    playwright_timeout_cls,
    db_manager_cls,
    logger: logging.Logger,
):
    class DBIsolatedOfficialEnvironment(base_environment):
        """Official ScriptBrowserEnv with a signed per-task DB route."""

        active_instance = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.db_manager = db_manager_cls(db_config(), logger)
            self.db_session = None
            self.task_config = None
            self.observation_retry_timeout_seconds = float(
                os.getenv("WEBARENA_OBSERVATION_RETRY_SECONDS", "10")
            )
            self.observation_retry_interval_seconds = 0.25
            type(self).active_instance = self

        def _get_obs(self):
            """Retry only transient official DOMSnapshot load races.

            The official processor divides DOM bounds by a scale inferred from
            the root width.  During navigation Chromium can briefly report a
            zero-width root; the processor's built-in 500ms load wait can then
            raise PlaywrightTimeoutError.  DB clones have colder page loads, so
            give the same official observation another chance once the page is
            stable without changing its contents or representation.
            """
            deadline = (
                time.monotonic() + self.observation_retry_timeout_seconds
            )
            attempt = 0
            while True:
                try:
                    return super()._get_obs()
                except (ZeroDivisionError, playwright_timeout_cls) as error:
                    if self.page.is_closed() or time.monotonic() >= deadline:
                        raise
                    attempt += 1
                    logger.warning(
                        "Transient official observation failure; retry %d: %r",
                        attempt,
                        error,
                    )
                    remaining_ms = max(
                        1, int((deadline - time.monotonic()) * 1000)
                    )
                    try:
                        self.page.wait_for_load_state(
                            "domcontentloaded", timeout=min(1000, remaining_ms)
                        )
                    except playwright_timeout_cls:
                        pass
                    time.sleep(self.observation_retry_interval_seconds)

        def _cleanup_db(self) -> None:
            if self.db_session is not None:
                self.db_manager.cleanup(self.db_session)
                self.db_session = None

        def _close_browser(self) -> None:
            if self.reset_finished:
                try:
                    self.context_manager.__exit__(None, None, None)
                finally:
                    self.reset_finished = False

        def reset(self, *, seed=None, options=None):
            self._close_browser()
            self._cleanup_db()
            if not options or "config_file" not in options:
                raise ValueError("DB-isolated official eval requires config_file")
            config_file = Path(options["config_file"])
            self.task_config = json.loads(config_file.read_text())
            self.db_session = self.db_manager.prepare_for_task(
                str(uuid.uuid4()), self.task_config
            )
            try:
                return super().reset(seed=seed, options=options)
            except Exception:
                self._close_browser()
                self._cleanup_db()
                raise

        def _install_route_token(self) -> None:
            assert self.db_session is not None
            allowed = {"127.0.0.1:8023", "localhost:8023"}
            for value in self.db_session.shared_site_hosts.values():
                parsed = urlsplit(
                    value if "://" in value else f"//{value}"
                )
                if parsed.netloc:
                    allowed.add(parsed.netloc)
            route_headers = dict(self.db_session.headers)

            def handler(route, request):
                if urlsplit(request.url).netloc not in allowed:
                    route.continue_()
                    return
                headers = dict(request.headers)
                headers.update(route_headers)
                route.continue_(headers=headers)

            self.context.route("**/*", handler)

        def _login_gitlab(self) -> None:
            login_page = self.context.new_page()
            try:
                login_page.goto(
                    "http://127.0.0.1:8023/users/sign_in",
                    wait_until="networkidle",
                )
                login_page.get_by_test_id("username-field").click()
                login_page.get_by_test_id("username-field").fill("byteblaze")
                login_page.get_by_test_id("username-field").press("Tab")
                login_page.get_by_test_id("password-field").fill("hello1234")
                login_page.get_by_test_id("sign-in-button").click()
                login_page.wait_for_load_state("networkidle")
                time.sleep(2)
            finally:
                login_page.close()

        def setup(self, config_file=None) -> None:
            self.context_manager = sync_playwright_fn()
            self.playwright = self.context_manager.__enter__()
            self.browser = self.playwright.chromium.launch(
                headless=self.headless, slow_mo=self.slow_mo
            )
            instance_config = (
                json.loads(Path(config_file).read_text()) if config_file else {}
            )
            self.context = self.browser.new_context(
                viewport=self.viewport_size,
                geolocation=instance_config.get("geolocation"),
                device_scale_factor=1,
            )
            self._install_route_token()
            if self.save_trace_enabled:
                self.context.tracing.start(screenshots=True, snapshots=True)
            self._login_gitlab()

            start_urls = instance_config.get("start_url", "").split(" |AND| ")
            start_urls = [url for url in start_urls if url]
            if not start_urls:
                start_urls = ["http://127.0.0.1:8023/"]
            for url in start_urls:
                page = self.context.new_page()
                client = page.context.new_cdp_session(page)
                if self.text_observation_type == "accessibility_tree":
                    client.send("Accessibility.enable")
                page.client = client
                page.goto(url)
            self.page = self.context.pages[0]
            self.page.bring_to_front()

        def wait_for_evaluation_state(self) -> None:
            if self.db_session is not None:
                self.db_manager.wait_for_background_jobs(self.db_session)

        def close(self) -> None:
            try:
                self._close_browser()
            finally:
                self._cleanup_db()
                type(self).active_instance = None

    return DBIsolatedOfficialEnvironment


def official_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        render=False,
        slow_mo=0,
        action_set_tag="id_accessibility_tree",
        observation_type="accessibility_tree",
        current_viewport_only=True,
        viewport_width=1280,
        viewport_height=720,
        save_trace_enabled=True,
        sleep_after_execution=float(os.getenv("WEBARENA_SLEEP_AFTER_EXECUTION", "0")),
        max_steps=args.max_steps,
        agent_type="prompt",
        instruction_path=args.instruction_path,
        parsing_failure_th=3,
        repeating_action_failure_th=3,
        provider="openai",
        model=args.model,
        mode="chat",
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=getattr(args, "top_k", -1),
        context_length=0,
        max_tokens=args.max_tokens,
        reasoning_effort=getattr(args, "reasoning_effort", None),
        stop_token=None,
        max_retry=args.max_retry,
        max_obs_length=args.max_obs_length,
        model_endpoint="",
        test_start_idx=0,
        test_end_idx=1000,
        test_ids_file=args.task_ids_file,
        result_dir=str(Path(args.result_dir).resolve()),
        render_screenshot=True,
    )


def make_task_configs(task_ids_file: Path, tasks_dir: Path) -> list[str]:
    values = [
        value
        for value in re.split(r"[\s,]+", task_ids_file.read_text().strip())
        if value
    ]
    task_ids = [int(value) for value in values]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task_ids_file contains duplicate task IDs")
    temp_dir = Path(tempfile.mkdtemp(prefix="webarena-db-configs-"))
    configs = []
    for task_id in task_ids:
        source = tasks_dir / f"{task_id}.json"
        if not source.is_file():
            raise FileNotFoundError(f"task config does not exist: {source}")
        task = json.loads(source.read_text())
        # Authentication is performed after installing the signed route token.
        task["storage_state"] = None
        destination = temp_dir / source.name
        destination.write_text(json.dumps(task, indent=2) + "\n")
        configs.append(str(destination))
    return configs


def main() -> None:
    args = parse_args()
    webarena_root = Path(args.webarena_root).resolve()
    (
        official_run,
        base_environment,
        _detached_page_cls,
        sync_playwright_fn,
        playwright_timeout_cls,
        _db_session_cls,
        db_manager_cls,
    ) = install_official_imports(webarena_root)

    db_environment = build_db_environment(
        base_environment,
        sync_playwright_fn,
        playwright_timeout_cls,
        db_manager_cls,
        official_run.logger,
    )
    official_run.ScriptBrowserEnv = db_environment

    official_evaluator_router = official_run.evaluator_router

    def waiting_evaluator_router(config_file):
        evaluator = official_evaluator_router(config_file)

        def evaluate_after_drain(*eval_args, **eval_kwargs):
            active = db_environment.active_instance
            if active is None:
                raise RuntimeError("DB environment is not active for evaluation")
            active.wait_for_evaluation_state()
            return evaluator(*eval_args, **eval_kwargs)

        return evaluate_after_drain

    official_run.evaluator_router = waiting_evaluator_router
    official_args = official_namespace(args)
    official_run.prepare(official_args)
    official_run.dump_config(official_args)
    configs = make_task_configs(
        Path(args.task_ids_file).resolve(), Path(args.tasks_dir).resolve()
    )
    configs = official_run.get_unfinished(configs, official_args.result_dir)
    if not configs:
        official_run.logger.info("No task left to run")
        return
    agent = official_run.construct_agent(official_args)
    official_run.test(official_args, agent, configs)


if __name__ == "__main__":
    main()
