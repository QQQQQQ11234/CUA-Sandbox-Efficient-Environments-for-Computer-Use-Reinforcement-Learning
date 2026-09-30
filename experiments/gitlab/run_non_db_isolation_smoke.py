from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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


async def fetch_json(page, url: str, method: str = "GET", body: dict | None = None) -> dict[str, Any]:
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
    if not 200 <= result["status"] < 300:
        raise RuntimeError(f"{method} {url} returned {result['status']}: {result['text']}")
    return json.loads(result["text"])


async def fetch_text(page, url: str) -> tuple[int, str]:
    result = await page.evaluate(
        """
        async (url) => {
          const response = await fetch(url, {headers: {Accept: 'text/plain'}});
          return {status: response.status, text: await response.text()};
        }
        """,
        url,
    )
    return int(result["status"]), str(result["text"])


async def upload_text(page, url: str, filename: str, content: str) -> dict[str, Any]:
    result = await page.evaluate(
        """
        async ({url, filename, content}) => {
          const body = new FormData();
          body.append('file', new File([content], filename, {type: 'text/plain'}));
          const headers = {Accept: 'application/json'};
          const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
          if (csrf) headers['X-CSRF-Token'] = csrf;
          const response = await fetch(url, {method: 'POST', headers, body});
          return {status: response.status, text: await response.text()};
        }
        """,
        {"url": url, "filename": filename, "content": content},
    )
    if not 200 <= result["status"] < 300:
        raise RuntimeError(f"upload returned {result['status']}: {result['text']}")
    return json.loads(result["text"])


def build_environment_config(args: argparse.Namespace, runtime_config: dict, uuid: str, root: Path):
    overrides = [
        "environment.isolation.mode=db",
        f"environment.db_isolation.admin_dsn={args.admin_dsn}",
        "environment.db_isolation.admin_docker_container=",
        f"environment.db_isolation.base_template_db={args.template_db}",
        f"environment.db_isolation.clone_strategy={args.clone_strategy}",
        f"environment.db_isolation.route_token_secret={runtime_config['route_secret']}",
        f"environment.db_isolation.route_registry_path={args.route_registry_path}",
        f"environment.db_isolation.shared_site_hosts.gitlab={args.shared_gitlab_host}",
        "environment.db_isolation.state_audit.enabled=false",
        "environment.db_isolation.non_db_state.enabled=true",
        "environment.db_isolation.non_db_state.gitlab_state.enabled=true",
        f"environment.db_isolation.non_db_state.gitlab_state.docker_container={args.gitlab_container}",
        f"environment.sites.gitlab={args.shared_gitlab_host}",
        "environment.proxy.enabled=false",
        "environment.evaluation.enabled=false",
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


async def run(args: argparse.Namespace) -> dict[str, Any]:
    with urllib.request.urlopen(args.registry_health_url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("route registry is not healthy")

    runtime_config = json.loads(Path(args.runtime_config).read_text())
    raw_task = json.loads(Path(args.task).read_text())
    task = replace_url(raw_task, args.task_gitlab_url, args.shared_gitlab_url)
    assert isinstance(task, dict)
    project = str(task["instantiation_dict"]["repo"])
    project_api = urllib.parse.quote(project, safe="")
    file_name = "LICENSE.txt"
    file_api = f"{args.shared_gitlab_url}/api/v4/projects/{project_api}/repository/files/{urllib.parse.quote(file_name, safe='')}"
    raw_api = f"{file_api}/raw?ref=master"
    upload_api = f"{args.shared_gitlab_url}/api/v4/projects/{project_api}/uploads"
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="gitlab_nondb_smoke_") as directory:
        root = Path(directory)
        env_a = WebAgentEnv(build_environment_config(args, runtime_config, "nondb_a", root))
        env_b = WebAgentEnv(build_environment_config(args, runtime_config, "nondb_b", root))
        try:
            await asyncio.gather(env_a.setup(task), env_b.setup(task))
            assert env_a.page and env_b.page
            assert env_a.db_agent_session and env_b.db_agent_session

            baseline_a = await fetch_text(env_a.page, raw_api)
            baseline_b = await fetch_text(env_b.page, raw_api)
            if baseline_a != baseline_b or baseline_a[0] != 200:
                raise AssertionError(f"baseline repository mismatch: {baseline_a[0]}, {baseline_b[0]}")

            probe_a1 = await fetch_json(env_a.page, f"{args.shared_gitlab_url}/__web_agent_probe")
            probe_b1 = await fetch_json(env_b.page, f"{args.shared_gitlab_url}/__web_agent_probe")
            probe_a2 = await fetch_json(env_a.page, f"{args.shared_gitlab_url}/__web_agent_probe")
            redis_fields = ("shared_state", "cache", "sessions", "trace_chunks", "rate_limiting")
            if any(probe_a1["redis_previous"][field] is not None for field in redis_fields):
                raise AssertionError("agent A Redis namespace was not empty")
            if any(probe_b1["redis_previous"][field] is not None for field in redis_fields):
                raise AssertionError("agent B Redis namespace observed agent A")
            if any(probe_a2["redis_previous"][field] != probe_a1["agent_id"] for field in redis_fields):
                raise AssertionError("agent A Redis namespace did not retain its own values")
            rails_cache_disabled = probe_a1["rails_cache_store"].endswith("NullStore")
            rails_cache_values = (
                probe_a1["redis_previous"]["rails_cache"],
                probe_b1["redis_previous"]["rails_cache"],
                probe_a2["redis_previous"]["rails_cache"],
            )
            expected_rails_cache = (None, None, None) if rails_cache_disabled else (None, None, probe_a1["agent_id"])
            if rails_cache_values != expected_rails_cache:
                raise AssertionError(f"unexpected Rails cache behavior: {rails_cache_values}")
            if probe_a1["artifacts_root"] == probe_b1["artifacts_root"]:
                raise AssertionError("artifact roots are shared")
            if probe_a1["uploads_component_root"] == probe_b1["uploads_component_root"]:
                raise AssertionError("upload roots are shared")
            if probe_a1["file_uploads_root"] != probe_a1["uploads_component_root"]:
                raise AssertionError("FileUploader did not consume the routed upload root")
            if probe_a1["elasticsearch_search"] or probe_a1["elasticsearch_indexing"]:
                raise AssertionError("OpenSearch/Elasticsearch is enabled")

            artifact = await fetch_json(
                env_a.page,
                f"{args.shared_gitlab_url}/__web_agent_probe/artifact",
                "POST",
            )
            artifact_url = urllib.parse.urljoin(args.shared_gitlab_url, artifact["url"])
            artifact_a = await fetch_text(env_a.page, artifact_url)
            artifact_b = await fetch_text(env_b.page, artifact_url)
            if artifact_a != (200, probe_a1["agent_id"]) or artifact_b[0] != 404:
                raise AssertionError(
                    f"artifact isolation failed: A={artifact_a}, B={artifact_b}"
                )

            upload = await upload_text(
                env_a.page,
                upload_api,
                "web-agent-isolation.txt",
                "agent A private upload",
            )
            upload_url = urllib.parse.urljoin(args.shared_gitlab_url, upload["full_path"])
            upload_a = await fetch_text(env_a.page, upload_url)
            upload_b = await fetch_text(env_b.page, upload_url)
            if upload_a != (200, "agent A private upload") or upload_b[0] != 404:
                raise AssertionError(
                    f"upload isolation failed: upload={upload}, url={upload_url}, "
                    f"A={upload_a}, B={upload_b}"
                )

            marker_one = "MIT license\n\nWebAgent isolated repository marker one"
            await fetch_json(
                env_a.page,
                file_api,
                "PUT",
                {"branch": "master", "content": marker_one, "commit_message": "Set isolated MIT license"},
            )
            await wait_for_jobs(env_a)
            committed_a = await fetch_text(env_a.page, raw_api)
            untouched_b = await fetch_text(env_b.page, raw_api)
            if marker_one not in committed_a[1] or marker_one in untouched_b[1]:
                raise AssertionError("Gitaly repository content crossed environments")

            await fetch_json(
                env_a.page,
                f"{args.shared_gitlab_url}/__web_agent_probe/sidekiq",
                "POST",
            )
            await wait_for_jobs(env_a)
            sidekiq_a = await fetch_json(env_a.page, f"{args.shared_gitlab_url}/__web_agent_probe")
            sidekiq_b = await fetch_json(env_b.page, f"{args.shared_gitlab_url}/__web_agent_probe")
            if sidekiq_a["sidekiq_value"] != probe_a1["agent_id"] or sidekiq_b["sidekiq_value"] is not None:
                raise AssertionError("Sidekiq route context crossed Redis namespaces")

            assert env_a.db_isolation_manager and env_a.db_agent_session
            checkpoint = await asyncio.to_thread(
                env_a.db_isolation_manager.checkpoint,
                env_a.db_agent_session,
                1,
            )
            marker_two = "MIT license\n\nWebAgent isolated repository marker two"
            await fetch_json(
                env_a.page,
                file_api,
                "PUT",
                {"branch": "master", "content": marker_two, "commit_message": "Advance isolated license"},
            )
            await wait_for_jobs(env_a)
            await asyncio.to_thread(
                env_a.db_isolation_manager.fork,
                env_a.db_agent_session,
                checkpoint,
                "left",
            )
            forked = await fetch_text(env_a.page, raw_api)
            forked_upload = await fetch_text(env_a.page, upload_url)
            if marker_one not in forked[1] or marker_two in forked[1] or forked_upload[0] != 200:
                raise AssertionError("checkpoint/fork did not restore repository and upload state")

            env_a.config.evaluation.enabled = False
            await env_a.reset()
            reset_content = await fetch_text(env_a.page, raw_api)
            reset_upload = await fetch_text(env_a.page, upload_url)
            if reset_content != baseline_a or reset_upload[0] != 404:
                raise AssertionError("reset did not restore base repository/upload state")

            env_a.model_answer = ""
            await fetch_json(
                env_a.page,
                file_api,
                "PUT",
                {"branch": "master", "content": marker_one, "commit_message": "Evaluator parity MIT license"},
            )
            await wait_for_jobs(env_a)
            reward = await env_a.evaluate_task()
            if reward != 1.0:
                raise AssertionError(f"WebArena evaluator returned {reward}")

            return {
                "success": True,
                "task_id": task["task_id"],
                "reward": reward,
                "rails_cache_policy": "disabled" if rails_cache_disabled else "namespace",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "agents": [probe_a1["agent_id"], probe_b1["agent_id"]],
                "validated": [
                    "postgresql",
                    "gitaly",
                    "uploads",
                    "artifacts",
                    "redis_shared_state",
                    "redis_cache",
                    "redis_sessions",
                    "redis_trace_chunks",
                    "redis_rate_limiting",
                    "sidekiq",
                    "opensearch_disabled",
                    "checkpoint",
                    "fork",
                    "reset",
                ],
            }
        finally:
            await asyncio.gather(env_a.close(), env_b.close(), return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate shared GitLab non-DB state isolation")
    parser.add_argument("--task", default="runtime/gitlab_eval_tasks/411.json")
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
    parser.add_argument("--output", default="results/gitlab_non_db_isolation_smoke.json")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
