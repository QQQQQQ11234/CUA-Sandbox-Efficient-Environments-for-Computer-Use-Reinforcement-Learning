from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

from rl_web_agent.env import WebAgentEnv


def replace_url(value: object, source: str, target: str) -> object:
    if isinstance(value, str):
        return value.replace(source, target).replace("__GITLAB__", target)
    if isinstance(value, list):
        return [replace_url(item, source, target) for item in value]
    if isinstance(value, dict):
        return {key: replace_url(item, source, target) for key, item in value.items()}
    return value


async def api(
    page,
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
) -> Any:
    result = await page.evaluate(
        """
        async ({url, method, body}) => {
          const headers = {Accept: 'application/json'};
          const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
          if (csrf) headers['X-CSRF-Token'] = csrf;
          if (body !== null) headers['Content-Type'] = 'application/json';
          const response = await fetch(url, {
            method,
            headers,
            body: body === null ? undefined : JSON.stringify(body),
          });
          return {status: response.status, text: await response.text()};
        }
        """,
        {"url": url, "method": method, "body": body},
    )
    status = int(result["status"])
    if status not in expected:
        raise RuntimeError(f"{method} {url} returned {status}: {result['text']}")
    if not result["text"]:
        return None
    return json.loads(result["text"])


async def status(page, url: str) -> int:
    result = await page.evaluate(
        "async (url) => (await fetch(url, {headers: {Accept: 'application/json'}})).status",
        url,
    )
    return int(result)


async def poll(
    operation: Callable[[], Awaitable[Any]],
    predicate: Callable[[Any], bool],
    description: str,
    timeout_seconds: float = 120,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = await operation()
        if predicate(last):
            return last
        await asyncio.sleep(1)
    raise TimeoutError(f"timed out waiting for {description}; last value={last!r}")


def project_api(base_url: str, path: str) -> str:
    return f"{base_url}/api/v4/projects/{urllib.parse.quote(path, safe='')}"


def build_environment_config(
    args: argparse.Namespace,
    runtime_config: dict[str, Any],
    uuid: str,
    root: Path,
):
    overrides = [
        "environment.isolation.mode=db",
        f"environment.db_isolation.admin_dsn={args.admin_dsn}",
        "environment.db_isolation.admin_docker_container=",
        f"environment.db_isolation.base_template_db={args.template_db}",
        f"environment.db_isolation.clone_strategy={args.clone_strategy}",
        f"environment.db_isolation.route_token_secret={runtime_config['route_secret']}",
        f"environment.db_isolation.route_registry_path={args.route_registry_path}",
        f"environment.db_isolation.shared_site_hosts.gitlab={args.shared_gitlab_host}",
        "environment.db_isolation.state_audit.enabled=true",
        "environment.db_isolation.non_db_state.enabled=true",
        "environment.db_isolation.non_db_state.gitlab_state.enabled=true",
        f"environment.db_isolation.non_db_state.gitlab_state.docker_container={args.gitlab_container}",
        f"environment.sites.gitlab={args.shared_gitlab_host}",
        "environment.proxy.enabled=false",
        "environment.evaluation.enabled=true",
        "environment.recording.enabled=false",
        "environment.tracing.enabled=false",
        "environment.browser.launch_options.headless=true",
        "environment.browser.timeouts.page_load_networkidle=15000",
    ]
    project_root = Path(__file__).resolve().parents[2]
    with initialize_config_dir(version_base=None, config_dir=str(project_root)):
        config = compose(config_name="config", overrides=overrides)
    OmegaConf.update(config.environment, "uuid", uuid, force_add=True)
    config.environment.browser.user_data_dir = str(root / f"profile_{uuid}")
    config.environment.browser.cache_dir = str(root / f"cache_{uuid}")
    return config.environment


async def wait_for_jobs(environment: WebAgentEnv) -> None:
    assert environment.db_isolation_manager and environment.db_agent_session
    await asyncio.to_thread(
        environment.db_isolation_manager.wait_for_background_jobs,
        environment.db_agent_session,
    )


def fetch_pool_probes(url: str, headers: dict[str, str], count: int = 96) -> list[dict[str, Any]]:
    def fetch_one(_: int) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={**headers, "Connection": "close", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(fetch_one, range(count)))


async def validate_pool_cleanup(
    args: argparse.Namespace,
    runtime_config: dict[str, Any],
    task: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    probe_url = f"{args.shared_gitlab_url}/__web_agent_probe/pool"
    first = WebAgentEnv(build_environment_config(args, runtime_config, "pool_cleanup_first", root))
    second = WebAgentEnv(build_environment_config(args, runtime_config, "pool_cleanup_second", root))
    try:
        await first.setup(task)
        assert first.db_agent_session
        first_probes = await asyncio.to_thread(
            fetch_pool_probes, probe_url, first.db_agent_session.headers
        )
        first_pids = {int(probe["process_id"]) for probe in first_probes}
    finally:
        await first.close()

    await asyncio.sleep(6)
    try:
        await second.setup(task)
        assert second.db_agent_session
        await asyncio.to_thread(
            fetch_pool_probes, probe_url, second.db_agent_session.headers
        )
        await asyncio.sleep(6)
        second_probes = await asyncio.to_thread(
            fetch_pool_probes, probe_url, second.db_agent_session.headers
        )
        second_by_pid = {
            int(probe["process_id"]): int(probe["registered_pool_count"])
            for probe in second_probes
        }
        overlap = first_pids.intersection(second_by_pid)
        if not overlap:
            raise AssertionError("pool cleanup probe did not revisit a Puma worker")
        leaked = {pid: second_by_pid[pid] for pid in overlap if second_by_pid[pid] != 1}
        if leaked:
            raise AssertionError(f"orphan Rails pools survived episode cleanup: {leaked}")
        return {
            "first_worker_count": len(first_pids),
            "second_worker_count": len(second_by_pid),
            "revisited_worker_count": len(overlap),
            "registered_pool_counts": second_by_pid,
        }
    finally:
        await second.close()


async def evaluate(environment: WebAgentEnv) -> float:
    environment.model_answer = ""
    reward = await environment.evaluate_task()
    if reward != 1.0:
        from rl_web_agent.evaluator import HTMLContentEvaluator, URLEvaluator

        assert environment.page and environment.task_config
        url_reward = await URLEvaluator().evaluate(
            "", environment.page, environment.task_config, environment.config, environment.extra_headers
        )
        html_reward = await HTMLContentEvaluator().evaluate(
            "", environment.page, environment.task_config, environment.config, environment.extra_headers
        )
        raise AssertionError(
            f"WebArena evaluator returned {reward}; url={url_reward}, "
            f"program_html={html_reward}, page={environment.page.url}"
        )
    return reward


async def reset_and_assert_absent(environment: WebAgentEnv, target_api: str) -> None:
    environment.config.evaluation.enabled = False
    await environment.reset()
    assert environment.page
    if await status(environment.page, target_api) != 404:
        raise AssertionError(f"state survived reset: {target_api}")


async def user_id(page, base_url: str, *, username: str | None = None, search: str | None = None) -> int:
    query = urllib.parse.urlencode({"username": username} if username else {"search": search})
    users = await api(page, f"{base_url}/api/v4/users?{query}")
    if search:
        exact = [user for user in users if user.get("name") == search]
        users = exact or users
    if not users:
        raise AssertionError(f"GitLab user not found: {username or search}")
    return int(users[0]["id"])


async def add_members(page, project: dict[str, Any], base_url: str, usernames: list[str]) -> None:
    for username in usernames:
        member_id = await user_id(page, base_url, username=username)
        await api(
            page,
            f"{base_url}/api/v4/projects/{project['id']}/members",
            "POST",
            {"user_id": member_id, "access_level": 30},
            (201,),
        )


async def run_fork(env_a: WebAgentEnv, env_b: WebAgentEnv, args: argparse.Namespace) -> dict[str, Any]:
    assert env_a.page and env_b.page
    projects = await api(
        env_a.page,
        f"{args.shared_gitlab_url}/api/v4/projects?search=2019-nCov&simple=true",
    )
    sources = [project for project in projects if project["path"] == "2019-nCov"]
    if not sources:
        raise AssertionError("2019-nCov source project not found")
    source = sources[0]
    await api(
        env_a.page,
        f"{args.shared_gitlab_url}/api/v4/projects/{source['id']}/fork",
        "POST",
        {},
        (201, 202),
    )
    target = project_api(args.shared_gitlab_url, "byteblaze/2019-nCov")
    forked = await poll(
        lambda: api(env_a.page, target, expected=(200, 404)),
        lambda value: isinstance(value, dict) and value.get("import_status") in ("finished", "none"),
        "project fork",
    )
    await wait_for_jobs(env_a)
    tree = await api(env_a.page, f"{target}/repository/tree")
    if not tree:
        raise AssertionError("fork has no Gitaly repository tree")
    if await status(env_b.page, target) != 404:
        raise AssertionError("fork is visible in agent B")
    reward = await evaluate(env_a)
    await reset_and_assert_absent(env_a, target)
    return {"task_id": 394, "reward": reward, "project_id": forked["id"], "tree_entries": len(tree)}


async def run_template(env_a: WebAgentEnv, env_b: WebAgentEnv, args: argparse.Namespace) -> dict[str, Any]:
    assert env_a.page and env_b.page
    created = await api(
        env_a.page,
        f"{args.shared_gitlab_url}/api/v4/projects",
        "POST",
        {
            "name": "web_agent_android_xl",
            "visibility": "private",
            "template_name": "android",
            "use_custom_template": False,
        },
        (201,),
    )
    await add_members(env_a.page, created, args.shared_gitlab_url, ["primer", "convexegg", "abisubramanya27"])
    target = project_api(args.shared_gitlab_url, "byteblaze/web_agent_android_xl")
    await poll(
        lambda: api(env_a.page, f"{target}/repository/commits", expected=(200, 409)),
        lambda commits: isinstance(commits, list) and bool(commits),
        "template repository initialization",
    )
    await wait_for_jobs(env_a)
    commits = await api(env_a.page, f"{target}/repository/commits")
    if "Android" not in commits[0]["title"]:
        raise AssertionError(f"unexpected template commit: {commits[0]['title']}")
    if await status(env_b.page, target) != 404:
        raise AssertionError("template project is visible in agent B")
    reward = await evaluate(env_a)
    await reset_and_assert_absent(env_a, target)
    return {"task_id": 748, "reward": reward, "commit": commits[0]["title"]}


async def run_merge_request(env_a: WebAgentEnv, env_b: WebAgentEnv, args: argparse.Namespace) -> dict[str, Any]:
    assert env_a.page and env_b.page
    target = project_api(args.shared_gitlab_url, "primer/design")
    reviewer = await user_id(env_a.page, args.shared_gitlab_url, search="Caroline Stewart")
    merge_request = await api(
        env_a.page,
        f"{target}/merge_requests",
        "POST",
        {
            "source_branch": "dialog-component",
            "target_branch": "dialog",
            "title": "Merge dialog component",
            "reviewer_ids": [reviewer],
        },
        (201,),
    )
    await wait_for_jobs(env_a)
    mr_api = f"{target}/merge_requests/{merge_request['iid']}"
    observed = await api(env_a.page, mr_api)
    if [item["id"] for item in observed["reviewers"]] != [reviewer]:
        raise AssertionError("merge request reviewer was not persisted")
    if await status(env_b.page, mr_api) != 404:
        raise AssertionError("merge request is visible in agent B")
    await env_a.page.goto(merge_request["web_url"], wait_until="networkidle")
    await env_a.page.wait_for_selector(".detail-page-description", timeout=15_000)
    rendered = await env_a.page.evaluate(
        """
        () => ({
          branches: [...document.querySelectorAll('.detail-page-description > a.gl-font-monospace')]
            .map((element) => element.outerText),
          reviewers: document.querySelector('.block.reviewer')?.outerText || '',
        })
        """
    )
    if rendered["branches"][:2] != ["dialog-component", "dialog"]:
        raise AssertionError(f"merge request branches rendered incorrectly: {rendered}")
    if "Caroline Stewart" not in rendered["reviewers"]:
        raise AssertionError(f"merge request reviewer rendered incorrectly: {rendered}")
    if "/primer/design/-/merge_requests/" not in env_a.page.url:
        raise AssertionError(f"merge request URL is incorrect: {env_a.page.url}")
    environment_project = project_api(args.shared_gitlab_url, "primer/design")
    env_a.config.evaluation.enabled = False
    await env_a.reset()
    if await status(env_a.page, f"{environment_project}/merge_requests/{merge_request['iid']}") != 404:
        raise AssertionError("merge request survived reset")
    return {
        "task_id": 666,
        "state_oracle": True,
        "iid": merge_request["iid"],
        "branches": rendered["branches"][:2],
        "reviewer": "Caroline Stewart",
    }


async def run_import(env_a: WebAgentEnv, env_b: WebAgentEnv, args: argparse.Namespace) -> dict[str, Any]:
    assert env_a.page and env_b.page
    imported = await api(
        env_a.page,
        f"{args.shared_gitlab_url}/api/v4/projects",
        "POST",
        {
            "name": "web_agent_import_probe",
            "visibility": "private",
            "import_url": args.import_url,
        },
        (201,),
    )
    target = project_api(args.shared_gitlab_url, "byteblaze/web_agent_import_probe")
    finished = await poll(
        lambda: api(env_a.page, target),
        lambda project: project.get("import_status") == "finished",
        "repository import",
    )
    await wait_for_jobs(env_a)
    files = await api(env_a.page, f"{target}/repository/tree?ref=master")
    if not files or not any(item["name"] in {"README", "README.md"} for item in files):
        raise AssertionError(f"imported Gitaly repository has unexpected tree: {files}")
    if await status(env_b.page, target) != 404:
        raise AssertionError("imported project is visible in agent B")
    await reset_and_assert_absent(env_a, target)
    return {
        "task_id": 475,
        "project_id": imported["id"],
        "import_status": finished["import_status"],
        "tree_entries": len(files),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    with urllib.request.urlopen(args.registry_health_url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("route registry is not healthy")
    runtime_config = json.loads(Path(args.runtime_config).read_text())
    source_tasks = json.loads(Path(args.tasks).read_text())
    tasks = {int(task["task_id"]): task for task in source_tasks}
    workflows = [
        ("fork", 394, run_fork),
        ("template", 748, run_template),
        ("merge_request", 666, run_merge_request),
        ("import", 475, run_import),
    ]
    selected = {item.strip() for item in args.workflows.split(",") if item.strip()}
    known = {name for name, _, _ in workflows}
    unknown = selected.difference(known)
    if unknown:
        raise ValueError(f"unknown workflows: {sorted(unknown)}")
    workflows = [workflow for workflow in workflows if workflow[0] in selected]
    results: dict[str, Any] = {}
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="gitlab_background_smoke_") as directory:
        root = Path(directory)
        for name, task_id, workflow in workflows:
            task = replace_url(tasks[task_id], args.task_gitlab_url, args.shared_gitlab_url)
            assert isinstance(task, dict)
            env_a = WebAgentEnv(build_environment_config(args, runtime_config, f"workflow_{name}_a", root))
            env_b = WebAgentEnv(build_environment_config(args, runtime_config, f"workflow_{name}_b", root))
            try:
                await asyncio.gather(env_a.setup(task), env_b.setup(task))
                results[name] = await workflow(env_a, env_b, args)
            finally:
                await asyncio.gather(env_a.close(), env_b.close(), return_exceptions=True)

        pool_task = replace_url(tasks[418], args.task_gitlab_url, args.shared_gitlab_url)
        assert isinstance(pool_task, dict)
        results["rails_pool_cleanup"] = await validate_pool_cleanup(
            args, runtime_config, pool_task, root
        )

    return {
        "success": True,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "workflows": results,
        "validated": [
            "fork_evaluator",
            "template_project_evaluator",
            "merge_request_state_oracle",
            "repository_import",
            "sidekiq_drain",
            "gitaly_content",
            "two_environment_isolation",
            "reset",
            "rails_pool_cleanup",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate routed GitLab background workflows")
    parser.add_argument("--tasks", default="thirdparty/webarena/config_files/test.raw.json")
    parser.add_argument("--runtime-config", default="runtime/gitlab_nondb_config.json")
    parser.add_argument("--gitlab-container", default="shared-gitlab-nondb")
    parser.add_argument("--admin-dsn", default="postgresql://postgres:postgres@127.0.0.1:55433/postgres")
    parser.add_argument("--template-db", default="gitlab_base_template")
    parser.add_argument("--route-registry-path", default="runtime/db_routes_nondb.sqlite3")
    parser.add_argument("--clone-strategy", default="FILE_COPY")
    parser.add_argument("--task-gitlab-url", default="http://metis.lti.cs.cmu.edu:8023")
    parser.add_argument("--shared-gitlab-url", default="http://127.0.0.1:8023")
    parser.add_argument("--shared-gitlab-host", default="127.0.0.1:8023")
    parser.add_argument("--registry-health-url", default="http://127.0.0.1:8765/health")
    parser.add_argument(
        "--import-url",
        default="https://github.com/octocat/Hello-World.git",
    )
    parser.add_argument(
        "--workflows",
        default="fork,template,merge_request,import",
        help="comma-separated subset of fork,template,merge_request,import",
    )
    parser.add_argument("--output", default="results/gitlab_background_workflow_smoke.json")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
