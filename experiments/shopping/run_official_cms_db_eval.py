#!/usr/bin/env python3
"""Run official WebArena Shopping Admin tasks with CUA-Sandbox DB isolation.

The benchmark agent, prompt, accessibility-tree observation, action parser,
early stopping, renderer, and evaluators are imported from the official
WebArena checkout.  Only the website lifecycle is replaced: every task gets a
clean XFS-reflink MariaDB branch and a signed route to the shared Magento app.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from omegaconf import OmegaConf
from playwright.sync_api import Error as PlaywrightError

from experiments.gitlab.run_official_db_eval import (
    install_official_imports,
    official_namespace,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEBARENA_ROOT = Path(os.environ.get("WEBARENA_ROOT", "/path/to/webarena-main"))
CMS_AUTHORITY = os.getenv("WEBARENA_CMS_AUTHORITY", "127.0.0.1:7780")
CMS_DASHBOARD_PATH = "/admin/admin/dashboard/"

# Inspect the live page, not just the viewport-limited AX tree: Magento can
# expose its menu before the asynchronous product/order grid is usable.
CMS_LOADING_REASON_JS = """() => {
    if (document.readyState !== 'complete')
        return 'document ' + document.readyState;
    const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden'
            && style.visibility !== 'collapse'
            && element.getClientRects().length > 0;
    };
    for (const element of document.querySelectorAll(
        '.loading-mask, .admin__data-grid-loading-mask, [data-role="spinner"]'
    )) {
        if (visible(element)) return 'visible Magento loading mask';
    }
    return '';
}"""


def has_busy_root_observation(observation) -> bool:
    """A populated AX tree can still be a partially loaded document."""
    return any(
        re.match(r"\s*\[\d+\] RootWebArea\b", line)
        and re.search(r"\bbusy: (?:1|True|true)(?:\s|$)",
                      re.sub(r"'[^']*'", "", line))
        for line in observation.get("text", "").splitlines()
    )


def is_loading_only_observation(observation) -> bool:
    """Recognize an AX navigation placeholder, not an empty but loaded page.

    Chromium can return only a busy RootWebArea without raising a timeout.
    Retry that observation before the agent spends an action on it. Keep
    populated trees (even when busy) and ordinary error/empty pages unchanged.
    """
    text = observation.get("text", "")
    nodes = [line.strip() for line in text.splitlines()
             if re.match(r"\s*\[\d+\] ", line)]
    return (
        len(nodes) == 1
        and re.match(r"\[\d+\] RootWebArea\b", nodes[0]) is not None
        and re.search(r"\bbusy: (?:1|True|true)\s*$", nodes[0]) is not None
    )


def with_top_k_default(generator):
    """Adapt official judge calls to the locally extended OpenAI helper."""
    if getattr(generator, "_cua_sandbox_top_k_default", False) is True:
        return generator

    @functools.wraps(generator)
    def wrapped(*args, **kwargs):
        kwargs.setdefault("top_k", -1)
        judge_reasoning_effort = os.getenv(
            "WEBARENA_JUDGE_REASONING_EFFORT"
        )
        if judge_reasoning_effort:
            kwargs.setdefault("reasoning_effort", judge_reasoning_effort)
        return generator(*args, **kwargs)

    wrapped._cua_sandbox_top_k_default = True
    return wrapped


def guard_empty_id_action(parser):
    """Turn the official empty-action parser crash into a handled bad action."""
    if getattr(parser, "_cua_sandbox_empty_action_guard", False) is True:
        return parser

    @functools.wraps(parser)
    def wrapped(action_str, *args, **kwargs):
        if not str(action_str).split():
            raise ValueError("Empty action returned by the model")
        try:
            return parser(action_str, *args, **kwargs)
        except IndexError as error:
            raise ValueError("Malformed action returned by the model") from error

    wrapped._cua_sandbox_empty_action_guard = True
    return wrapped


def install_official_compatibility_shims() -> None:
    """Install narrow guards for known local WebArena interface mismatches."""
    import agent.agent as official_agent
    import evaluation_harness.helper_functions as evaluator_helpers

    evaluator_helpers.generate_from_openai_chat_completion = with_top_k_default(
        evaluator_helpers.generate_from_openai_chat_completion
    )
    official_agent.create_id_based_action = guard_empty_id_action(
        official_agent.create_id_based_action
    )


def assert_authenticated_cms_page(page, phase: str) -> None:
    """Reject false-positive CMS logins and broken routed start pages."""
    parsed = urlsplit(page.url)
    if parsed.netloc != CMS_AUTHORITY:
        raise RuntimeError(
            f"CMS {phase} changed authority from {CMS_AUTHORITY} "
            f"to {parsed.netloc}"
        )
    content = page.content()
    login_visible = page.locator("#login-form").count() > 0
    admin_menu_visible = page.locator(".admin__menu").count() > 0
    if login_visible or "Welcome, please sign in" in content:
        raise RuntimeError(f"CMS {phase} is still on the login page")
    if "There has been an error processing your request" in content:
        raise RuntimeError(f"CMS {phase} returned a Magento exception page")
    if not admin_menu_visible:
        raise RuntimeError(f"CMS {phase} has no authenticated admin menu")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Official WebArena CMS eval with MySQL/XFS isolation"
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


def db_config():
    xfs_base = os.getenv(
        "MYSQL_XFS_BASE_PATH",
        "/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs",
    )
    state_runtime = os.getenv(
        "MAGENTO_STATE_RUNTIME_ROOT", f"{xfs_base}/runtime/magento_state"
    )
    hooks_enabled = os.getenv("MAGENTO_STATE_HOOKS_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    command_hooks = []
    if hooks_enabled:
        lifecycle = str(PROJECT_ROOT / "scripts/magento_state_lifecycle.py")
        shared_container = os.getenv(
            "MAGENTO_SHARED_CONTAINER", "shared-shopping-admin"
        )
        commands = {
            phase: [
                [
                    sys.executable,
                    lifecycle,
                    phase,
                    "--container",
                    shared_container,
                ]
            ]
            for phase in (
                "prepare",
                "quiesce",
                "checkpoint",
                "fork",
                "reset",
                "activate",
                "cleanup",
            )
        }
        command_hooks.append(
            {
                "name": "magento_search_queue",
                "enabled": True,
                "timeout_seconds": 1800,
                "commands": commands,
            }
        )

    return OmegaConf.create(
        {
            "admin_dsn": "",
            "admin_docker_container": "",
            "base_template_db": "unused_for_mysql",
            "clone_strategy": "FILE_COPY",
            "agent_db_prefix": "agent_",
            "agent_db_suffix": "_db",
            "route_token_header": "X-Agent-Route",
            "route_token_secret": os.environ["ROUTE_TOKEN_SECRET"],
            "route_token_ttl_seconds": 86400,
            "checkpoint_tag": "_ckpt_",
            "branch_tag": "_branch_",
            "route_registry_path": os.getenv(
                "ROUTE_REGISTRY_PATH",
                str(PROJECT_ROOT / "runtime/shopping_db_routes.sqlite3"),
            ),
            "reset_on_setup": True,
            "drop_on_close": True,
            "route_drain_timeout_seconds": 30,
            "evaluation_drain_timeout_seconds": 30,
            "rails_pool_release_enabled": False,
            "state_audit": {
                "enabled": True,
                "audit_paths": [
                    str(PROJECT_ROOT / "experiments/state_audits/shopping_admin.json")
                ],
                "task_manifest_paths": [
                    str(
                        PROJECT_ROOT
                        / "experiments/shopping/shopping_task_contracts.json"
                    )
                ],
            },
            "non_db_state": {
                "enabled": True,
                "runtime_root": str(PROJECT_ROOT / "runtime/non_db_state_cms"),
                "worker_mode": "shared",
                "overlay": {"enabled": False},
                "magento_state": {
                    "enabled": True,
                    "sites": ["shopping_admin"],
                    "template_root": f"{xfs_base}/magento_admin_template",
                    "template_roots": {
                        "shopping_admin": f"{xfs_base}/magento_admin_template"
                    },
                    "runtime_root": state_runtime,
                    "container_root": "/var/www/magento2/.web-agent-state",
                },
                "command_hooks": command_hooks,
            },
            "shared_site_hosts": {
                "shopping_admin": os.getenv(
                    "SHOPPING_ADMIN_TARGET", "127.0.0.1:7780"
                )
            },
            "mysql_xfs": {
                "enabled": True,
                "sites": ["shopping_admin"],
                "xfs_base_path": xfs_base,
                "runtime_root": os.getenv(
                    "MYSQL_XFS_RUNTIME_ROOT", f"{xfs_base}/runtime"
                ),
                "template_name": "magento_admin_template",
                "templates": {"shopping_admin": "magento_admin_template"},
                "database_name": "magentodb",
                "bind_host": os.getenv("MYSQL_BIND_HOST", "172.17.0.1"),
                "admin_host": os.getenv("MYSQL_ADMIN_HOST", "172.17.0.1"),
                "advertised_host": os.getenv(
                    "MYSQL_ADVERTISED_HOST", "172.17.0.1"
                ),
                "mysql_port_base": int(os.getenv("MYSQL_PORT_BASE", "13306")),
                "mysql_max_instances": 1000,
                "mysql_user": "magentouser",
                "mysql_password": "MyPassword",
                "launch_mode": "docker_exec",
                "runtime_container": os.getenv(
                    "MYSQL_RUNTIME_CONTAINER", "shopping-admin-mysql-runtime"
                ),
                "runtime_container_user": "",
                "mysqld_binary": "mysqld",
                "mysqladmin_binary": "mysqladmin",
                "launch_enabled": True,
                "startup_timeout_seconds": 60,
                "shutdown_timeout_seconds": 30,
                "mysqld_extra_args": [],
            },
        }
    )


def build_cms_db_environment(
    base_environment,
    sync_playwright_fn,
    playwright_timeout_cls,
    db_manager_cls,
    logger: logging.Logger,
):
    class CMSDBIsolatedOfficialEnvironment(base_environment):
        """Official ScriptBrowserEnv with signed CMS DB routing."""

        active_instance = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.db_manager = db_manager_cls(db_config(), logger)
            self.db_session = None
            self.task_config = None
            self._playwright_entered = False
            self.observation_retry_timeout_seconds = float(
                os.getenv("WEBARENA_OBSERVATION_RETRY_SECONDS", "10")
            )
            self.observation_retry_interval_seconds = 0.25
            type(self).active_instance = self

        def _get_obs(self):
            deadline = time.monotonic() + self.observation_retry_timeout_seconds
            attempt = 0
            ready_since = None
            while True:
                try:
                    retry_reason = self.page.evaluate(CMS_LOADING_REASON_JS)
                    if not retry_reason:
                        if ready_since is None:
                            # AX/screenshot extraction itself can take seconds.
                            # Count that ready interval instead of starting the
                            # clock only after the expensive snapshot finishes.
                            ready_since = time.monotonic()
                        observation = super()._get_obs()
                        if has_busy_root_observation(observation):
                            retry_reason = "busy RootWebArea (possibly partial content)"
                        else:
                            # A navigation/loader can start during AX extraction.
                            retry_reason = self.page.evaluate(CMS_LOADING_REASON_JS)
                except (ZeroDivisionError, playwright_timeout_cls) as error:
                    if self.page.is_closed() or time.monotonic() >= deadline:
                        raise
                    retry_reason = repr(error)
                except PlaywrightError as error:
                    # A navigation can destroy the execution context between
                    # readiness checks. Do not hide unrelated browser errors.
                    if self.page.is_closed() or not any(message in str(error) for message in (
                        "Execution context was destroyed",
                        "Cannot find context with specified id",
                    )):
                        raise
                    retry_reason = str(error)

                now = time.monotonic()
                if not retry_reason:
                    # After loading, require one polling interval of readiness
                    # and return a fresh observation with matching element IDs.
                    if attempt == 0 or (ready_since is not None and
                            now - ready_since >= self.observation_retry_interval_seconds):
                        return observation
                    if ready_since is None:
                        ready_since = now
                    retry_reason = "confirming Magento page readiness"
                else:
                    ready_since = None
                if self.page.is_closed() or now >= deadline:
                    raise playwright_timeout_cls(
                        "Isolated Magento page did not become ready within "
                        f"{self.observation_retry_timeout_seconds}s: {retry_reason}"
                    )
                attempt += 1
                logger.warning(
                    "Transient official observation failure; retry %d: %s",
                    attempt,
                    retry_reason,
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
                # Let Playwright process navigation events while waiting.
                remaining_ms = max(0, (deadline - time.monotonic()) * 1000)
                self.page.wait_for_timeout(min(
                    self.observation_retry_interval_seconds * 1000,
                    remaining_ms,
                ))

        def get_page_client(self, page):
            """Attach the CDP session omitted by official close-last-tab logic."""
            client = getattr(page, "client", None)
            if client is None:
                client = page.context.new_cdp_session(page)
                if self.text_observation_type == "accessibility_tree":
                    client.send("Accessibility.enable")
                page.client = client
            return client

        def _cleanup_db(self) -> None:
            if self.db_session is not None:
                self.db_manager.cleanup(self.db_session)
                self.db_session = None

        def _close_browser(self) -> None:
            # Official ScriptBrowserEnv sets reset_finished only after setup
            # succeeds.  A login/navigation error can therefore leave the
            # sync Playwright context (and its asyncio loop) alive, poisoning
            # every later task with "Sync API inside the asyncio loop".
            context_manager = getattr(self, "context_manager", None)
            if self._playwright_entered and context_manager is not None:
                try:
                    context_manager.__exit__(None, None, None)
                finally:
                    self._playwright_entered = False
                    self.context_manager = None
                    self.playwright = None
                    self.browser = None
                    self.context = None
                    self.page = None
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

        def _install_route(self) -> None:
            assert self.db_session is not None
            target_value = self.db_session.shared_site_hosts["shopping_admin"]
            target = urlsplit(
                target_value if "://" in target_value else f"//{target_value}"
            ).netloc
            sources = {
                CMS_AUTHORITY,
                "localhost:7780",
                "172.17.0.1:7780",
                "metis.lti.cs.cmu.edu:7780",
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
                    # The CMS shared runtime binds the official authority
                    # directly. Keep the URL untouched and add only the
                    # signed route header; rewriting a URL to itself creates
                    # unnecessary redirect/request races in Playwright.
                    route.continue_(headers=headers)
                elif parsed.netloc in sources:
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
                else:
                    route.continue_(headers=headers)

            self.context.route("**/*", handler)

        def _login_cms(self) -> None:
            login_page = self.context.new_page()
            try:
                login_page.goto(
                    f"http://{CMS_AUTHORITY}/admin",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                login_page.get_by_placeholder("user name").fill("admin")
                login_page.get_by_placeholder("password").fill("admin1234")
                login_page.get_by_role("button", name="Sign in").click()
                login_page.wait_for_url(
                    re.compile(r"/admin(?:/admin)?/dashboard/?"),
                    timeout=60000,
                )
                login_page.wait_for_load_state("domcontentloaded", timeout=60000)
                assert_authenticated_cms_page(login_page, "login response")

                # A URL-only check is insufficient: Magento can render its
                # login form at the dashboard URL after losing a host-scoped
                # session.  Verify that the cookie also survives on a fresh
                # page using the exact benchmark authority.
                verification_page = self.context.new_page()
                try:
                    verification_page.goto(
                        f"http://{CMS_AUTHORITY}{CMS_DASHBOARD_PATH}",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    assert_authenticated_cms_page(
                        verification_page, "fresh-page session verification"
                    )
                finally:
                    verification_page.close()
            finally:
                login_page.close()

        def setup(self, config_file=None) -> None:
            self.context_manager = sync_playwright_fn()
            self.playwright = self.context_manager.__enter__()
            self._playwright_entered = True
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
                # Chromium may issue the follow-up request for a Magento 302
                # before a per-request route override has propagated its
                # custom headers.  A context default keeps the signed route
                # capability on same-context redirects; _install_route still
                # restricts which authorities are rewritten to the shared app.
                extra_http_headers=dict(self.db_session.headers),
            )
            self._install_route()
            if self.save_trace_enabled:
                self.context.tracing.start(screenshots=True, snapshots=True)
            self._login_cms()

            start_urls = instance_config.get("start_url", "").split(" |AND| ")
            start_urls = [url for url in start_urls if url]
            if not start_urls:
                start_urls = [f"http://{CMS_AUTHORITY}/admin"]
            for url in start_urls:
                page = self.context.new_page()
                client = page.context.new_cdp_session(page)
                if self.text_observation_type == "accessibility_tree":
                    client.send("Accessibility.enable")
                page.client = client
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                assert_authenticated_cms_page(page, "task start page")
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

    return CMSDBIsolatedOfficialEnvironment


def _canonicalize_cms_urls(value):
    if isinstance(value, str):
        for authority in (
            "172.17.0.1:7780",
            "127.0.0.1:7780",
            "localhost:7780",
            "metis.lti.cs.cmu.edu:7780",
        ):
            value = value.replace(authority, CMS_AUTHORITY)
        return value
    if isinstance(value, list):
        return [_canonicalize_cms_urls(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonicalize_cms_urls(item) for key, item in value.items()}
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
    temp_dir = Path(tempfile.mkdtemp(prefix="webarena-cms-db-configs-"))
    configs = []
    for task_id in task_ids:
        source = tasks_dir / f"{task_id}.json"
        if not source.is_file():
            raise FileNotFoundError(f"task config does not exist: {source}")
        task = _canonicalize_cms_urls(json.loads(source.read_text()))
        if task.get("sites") != ["shopping_admin"]:
            raise ValueError(f"task {task_id} is not a Shopping Admin task")
        # Login happens inside the routed per-task browser context, matching
        # official WebArena's per-task cookie renewal semantics.
        task["storage_state"] = None
        destination = temp_dir / source.name
        destination.write_text(json.dumps(task, indent=2) + "\n")
        configs.append(str(destination))
    return configs


def main() -> None:
    args = parse_args()
    # install_official_imports changes cwd to the official checkout. Resolve
    # every caller-owned path first so results and task lists remain anchored
    # to CUA-Sandbox rather than silently moving under webarena-main.
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
    install_official_compatibility_shims()

    db_environment = build_cms_db_environment(
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
        task_ids_file, tasks_dir
    )
    configs = official_run.get_unfinished(configs, official_args.result_dir)
    if not configs:
        official_run.logger.info("No task left to run")
        return
    agent = official_run.construct_agent(official_args)
    try:
        official_run.test(official_args, agent, configs)
    finally:
        # KeyboardInterrupt bypasses the official per-task Exception handler.
        # Release the routed DB branch when a run is interrupted as well.
        active = db_environment.active_instance
        if active is not None:
            active.close()


if __name__ == "__main__":
    main()
