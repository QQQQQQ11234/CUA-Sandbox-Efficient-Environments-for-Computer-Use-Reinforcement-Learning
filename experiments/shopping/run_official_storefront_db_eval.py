#!/usr/bin/env python3
"""Run official WebArena Shopping tasks with CUA-Sandbox DB isolation.

The official WebArena agent, prompt, browser observation/action stack, early
stopping, renderer, and evaluators remain unchanged.  Only the per-task site
lifecycle is replaced with a clean MySQL/XFS-reflink branch routed to the
shared Magento storefront.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from experiments.gitlab.run_official_db_eval import (
    install_official_imports,
    official_namespace,
)
from experiments.shopping import run_official_cms_db_eval as cms_runner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEBARENA_ROOT = Path(os.environ.get("WEBARENA_ROOT", "/path/to/webarena-main"))
SHOPPING_AUTHORITY = os.getenv("WEBARENA_SHOPPING_AUTHORITY", "127.0.0.1:7770")
SHOPPING_ACCOUNT_PATH = "/customer/account/"
_CMS_DB_CONFIG = cms_runner.db_config


class RoutedEvaluatorRequests:
    """Add the active task route to official Shopping evaluator API calls.

    WebArena's Shopping helper functions use ``requests`` directly. Browser
    routing therefore does not cover their admin-token and REST calls. Without
    the signed task header those calls hit the shared gateway and are rejected
    with a plain-text 403, which the official helper then reports as an opaque
    JSONDecodeError.
    """

    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        delegate,
        environment_type,
        *,
        attempts: int = 3,
        timeout_seconds: float = 60,
        retry_interval_seconds: float = 1,
    ) -> None:
        self._delegate = delegate
        self._environment_type = environment_type
        self._attempts = attempts
        self._timeout_seconds = timeout_seconds
        self._retry_interval_seconds = retry_interval_seconds
        self._cua_sandbox_routed_evaluator_requests = True

    def _request(self, method: str, url: str, *args, **kwargs):
        active = self._environment_type.active_instance
        if active is None or active.db_session is None:
            raise RuntimeError(
                "Shopping evaluator request has no active DB-isolated task"
            )

        parsed = urlsplit(url)
        target_value = active.db_session.shared_site_hosts["shopping"]
        target = urlsplit(
            target_value if "://" in target_value else f"//{target_value}"
        ).netloc
        routed_authorities = {
            SHOPPING_AUTHORITY,
            "localhost:7770",
            "172.17.0.1:7770",
            "metis.lti.cs.cmu.edu:7770",
            target,
        }
        if parsed.netloc not in routed_authorities:
            return getattr(self._delegate, method)(url, *args, **kwargs)

        request_kwargs = dict(kwargs)
        headers = dict(request_kwargs.pop("headers", {}) or {})
        headers.update(active.db_session.headers)
        request_kwargs["headers"] = headers
        request_kwargs.setdefault("timeout", self._timeout_seconds)

        response = None
        for attempt in range(1, self._attempts + 1):
            response = getattr(self._delegate, method)(
                url, *args, **request_kwargs
            )
            if (
                response.status_code not in self._RETRYABLE_STATUS_CODES
                or attempt == self._attempts
            ):
                break
            time.sleep(self._retry_interval_seconds)
        assert response is not None
        response.raise_for_status()
        return response

    def get(self, url: str, *args, **kwargs):
        return self._request("get", url, *args, **kwargs)

    def post(self, url: str, *args, **kwargs):
        return self._request("post", url, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def install_routed_evaluator_requests(evaluator_helpers, environment_type) -> None:
    """Route official evaluator-side Shopping REST calls to the task branch."""
    current = evaluator_helpers.requests
    if getattr(current, "_cua_sandbox_routed_evaluator_requests", False):
        return
    evaluator_helpers.requests = RoutedEvaluatorRequests(
        current, environment_type
    )


def assert_authenticated_storefront_page(page, phase: str) -> None:
    """Reject expired customer sessions and broken routed storefront pages."""
    parsed = urlsplit(page.url)
    if parsed.netloc != SHOPPING_AUTHORITY:
        raise RuntimeError(
            f"Shopping {phase} changed authority from {SHOPPING_AUTHORITY} "
            f"to {parsed.netloc}"
        )
    content = page.content()
    login_visible = page.locator("#login-form").count() > 0
    logout_visible = (
        page.locator('a[href*="customer/account/logout"]').count() > 0
    )
    if login_visible or "/customer/account/login" in parsed.path:
        raise RuntimeError(f"Shopping {phase} is still on the login page")
    if "There has been an error processing your request" in content:
        raise RuntimeError(f"Shopping {phase} returned a Magento exception page")
    if not logout_visible:
        raise RuntimeError(f"Shopping {phase} has no authenticated customer session")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Official WebArena Shopping eval with MySQL/XFS isolation"
    )
    parser.add_argument("--task_ids_file", required=True)
    parser.add_argument(
        "--tasks_dir", default=str(DEFAULT_WEBARENA_ROOT / "config_files")
    )
    parser.add_argument("--result_dir", required=True)
    parser.add_argument("--webarena_root", default=str(DEFAULT_WEBARENA_ROOT))
    parser.add_argument(
        "--instruction_path",
        default="agent/prompts/jsons/p_cot_id_actree_2s.json",
    )
    parser.add_argument("--model", default="Qwen/Qwen3.5-9B")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--top_k", type=int, default=-1)
    parser.add_argument("--max_tokens", type=int, default=384)
    parser.add_argument("--max_steps", type=int, default=30)
    parser.add_argument("--max_retry", type=int, default=1)
    parser.add_argument("--max_obs_length", type=int, default=1920)
    parser.add_argument("--reasoning_effort", default=None)
    return parser.parse_args()


def storefront_db_config():
    """Specialize the reviewed Magento isolation config for Storefront."""
    config = _CMS_DB_CONFIG()
    xfs_base = str(config.mysql_xfs.xfs_base_path)
    config.state_audit.audit_paths = [
        str(PROJECT_ROOT / "experiments/state_audits/shopping.json")
    ]
    config.non_db_state.runtime_root = str(
        PROJECT_ROOT / "runtime/non_db_state_shopping"
    )
    config.non_db_state.magento_state.sites = ["shopping"]
    config.non_db_state.magento_state.template_root = f"{xfs_base}/magento_template"
    config.non_db_state.magento_state.template_roots = {
        "shopping": f"{xfs_base}/magento_template"
    }
    config.shared_site_hosts = {
        "shopping": os.getenv("SHOPPING_TARGET", SHOPPING_AUTHORITY)
    }
    config.mysql_xfs.sites = ["shopping"]
    config.mysql_xfs.template_name = "magento_template"
    config.mysql_xfs.templates = {"shopping": "magento_template"}
    config.mysql_xfs.runtime_container = os.getenv(
        "MYSQL_RUNTIME_CONTAINER", "shopping-mysql-runtime"
    )
    shared_container = os.getenv(
        "SHOPPING_SHARED_CONTAINER", "shared-shopping"
    )
    for hook in config.non_db_state.command_hooks:
        for phase in hook.commands:
            for command in hook.commands[phase]:
                if "--container" in command:
                    command[command.index("--container") + 1] = shared_container
    return config


def build_storefront_db_environment(
    base_environment,
    sync_playwright_fn,
    playwright_timeout_cls,
    db_manager_cls,
    logger,
):
    # The shared implementation deliberately resolves these names at runtime.
    # Specializing them in this process leaves the independently running CMS
    # evaluator untouched while retaining one reviewed lifecycle implementation.
    cms_runner.CMS_AUTHORITY = SHOPPING_AUTHORITY
    cms_runner.CMS_DASHBOARD_PATH = SHOPPING_ACCOUNT_PATH
    cms_runner.assert_authenticated_cms_page = assert_authenticated_storefront_page
    cms_runner.db_config = storefront_db_config
    parent = cms_runner.build_cms_db_environment(
        base_environment,
        sync_playwright_fn,
        playwright_timeout_cls,
        db_manager_cls,
        logger,
    )

    class StorefrontDBIsolatedOfficialEnvironment(parent):
        active_instance = None

        def _install_route(self) -> None:
            assert self.db_session is not None
            target_value = self.db_session.shared_site_hosts["shopping"]
            target = urlsplit(
                target_value if "://" in target_value else f"//{target_value}"
            ).netloc
            sources = {
                SHOPPING_AUTHORITY,
                "localhost:7770",
                "172.17.0.1:7770",
                "metis.lti.cs.cmu.edu:7770",
            }
            route_headers = dict(self.db_session.headers)

            def handler(route, request):
                parsed = urlsplit(request.url)
                if parsed.netloc not in sources and parsed.netloc != target:
                    route.continue_()
                    return
                headers = dict(request.headers)
                headers.update(route_headers)
                if parsed.netloc == target:
                    route.continue_(headers=headers)
                    return
                rewritten = urlunsplit(
                    (
                        parsed.scheme,
                        target,
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
                route.continue_(url=rewritten, headers=headers)

            self.context.route("**/*", handler)

        def _login_storefront_once(self) -> None:
            login_page = self.context.new_page()
            try:
                login_page.goto(
                    f"http://{SHOPPING_AUTHORITY}/customer/account/login/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                login_page.get_by_label("Email", exact=True).fill(
                    "emma.lopez@gmail.com"
                )
                login_page.get_by_label("Password", exact=True).fill(
                    "Password.123"
                )
                login_page.get_by_role("button", name="Sign In").click()
                login_page.wait_for_url(
                    re.compile(r"/customer/account/?(?:\?.*)?$"),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                login_page.wait_for_load_state("domcontentloaded", timeout=60000)
                assert_authenticated_storefront_page(login_page, "login response")

                verification_page = self.context.new_page()
                try:
                    verification_page.goto(
                        f"http://{SHOPPING_AUTHORITY}{SHOPPING_ACCOUNT_PATH}",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    assert_authenticated_storefront_page(
                        verification_page, "fresh-page session verification"
                    )
                finally:
                    verification_page.close()
            finally:
                login_page.close()

        def _login_cms(self) -> None:
            attempts = max(
                1, int(os.getenv("WEBARENA_STOREFRONT_LOGIN_ATTEMPTS", "3"))
            )
            for attempt in range(1, attempts + 1):
                try:
                    self._login_storefront_once()
                    return
                except (playwright_timeout_cls, RuntimeError) as error:
                    if attempt == attempts:
                        raise
                    logger.warning(
                        "Transient Shopping login failure; retry %d/%d: %r",
                        attempt,
                        attempts,
                        error,
                    )
                    # A rejected Magento login leaves a host-scoped frontend
                    # cookie behind. Reusing it makes the next attempt fail in
                    # exactly the same way, so start the retry with clean state.
                    self.context.clear_cookies()
                    time.sleep(min(2, attempt))

    return StorefrontDBIsolatedOfficialEnvironment


def _canonicalize_shopping_urls(value):
    if isinstance(value, str):
        for authority in (
            "172.17.0.1:7770",
            "127.0.0.1:7770",
            "localhost:7770",
            "metis.lti.cs.cmu.edu:7770",
        ):
            value = value.replace(authority, SHOPPING_AUTHORITY)
        return value
    if isinstance(value, list):
        return [_canonicalize_shopping_urls(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _canonicalize_shopping_urls(item) for key, item in value.items()
        }
    return value


def make_task_configs(task_ids_file: Path, tasks_dir: Path) -> list[str]:
    values = [
        value
        for value in re.split(r"[\s,]+", task_ids_file.read_text().strip())
        if value
    ]
    task_ids = [int(value) for value in values]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task_ids_file contains duplicate task IDs")
    temp_dir = Path(tempfile.mkdtemp(prefix="webarena-shopping-db-configs-"))
    configs = []
    for task_id in task_ids:
        source = tasks_dir / f"{task_id}.json"
        if not source.is_file():
            raise FileNotFoundError(f"task config does not exist: {source}")
        task = _canonicalize_shopping_urls(json.loads(source.read_text()))
        if task.get("sites") != ["shopping"]:
            raise ValueError(f"task {task_id} is not a Shopping task")
        # Official WebArena renews this customer login before every task.  The
        # routed environment performs the same login after its DB branch and
        # signed browser context exist, so an unrouted global cookie is unsafe.
        task["storage_state"] = None
        destination = temp_dir / source.name
        destination.write_text(json.dumps(task, indent=2) + "\n")
        configs.append(str(destination))
    return configs


def main() -> None:
    args = parse_args()
    task_ids_file = Path(args.task_ids_file).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    args.task_ids_file = str(task_ids_file)
    args.tasks_dir = str(tasks_dir)
    args.result_dir = str(Path(args.result_dir).resolve())
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
    cms_runner.install_official_compatibility_shims()

    db_environment = build_storefront_db_environment(
        base_environment,
        sync_playwright_fn,
        playwright_timeout_cls,
        db_manager_cls,
        official_run.logger,
    )
    import evaluation_harness.helper_functions as evaluator_helpers

    install_routed_evaluator_requests(evaluator_helpers, db_environment)
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
    configs = make_task_configs(task_ids_file, tasks_dir)
    configs = official_run.get_unfinished(configs, official_args.result_dir)
    if not configs:
        official_run.logger.info("No task left to run")
        return
    agent = official_run.construct_agent(official_args)
    official_run.test(official_args, agent, configs)


if __name__ == "__main__":
    main()
