#!/usr/bin/env python3
"""
Batch Agent - Run multiple WebAgent tasks concurrently
Run with: python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3,4,5 --max_concurrent 3 llm.provider=openai environment.browser.headless=false

Use --agent_type to choose between regular (default) and tool agents:
python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3 --agent_type tool

Control browser launch concurrency separately from task concurrency:
python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3 --max_concurrent 10 --max_concurrent_launch 2
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Any

import click
import httpx
from hydra import compose, initialize
from hydra.core.global_hydra import GlobalHydra
from omegaconf import OmegaConf

from rl_web_agent.agent import create_web_agent
from rl_web_agent.config_store import ConfigStore
from rl_web_agent.env import WebAgentEnv
from rl_web_agent.tool_agent import create_tool_web_agent

# Logger will be configured in main() after loading config
logger = logging.getLogger(__name__)


def _openai_health_config(cfg: Any) -> Any | None:
    if str(cfg.llm.provider).lower() != "openai":
        return None
    health_cfg = cfg.llm.openai.get("health_check")
    if not health_cfg or not bool(health_cfg.get("enabled", True)):
        return None
    return health_cfg


async def wait_for_openai_health(
    cfg: Any, *, timeout_seconds: float, purpose: str
) -> None:
    """Wait until the configured OpenAI-compatible endpoint is actually ready."""
    if _openai_health_config(cfg) is None:
        return

    base_url = str(cfg.llm.openai.get("base_url", "")).rstrip("/")
    if not base_url:
        raise RuntimeError("OpenAI provider health check requires llm.openai.base_url")
    health_url = f"{base_url}/models"
    interval = float(cfg.llm.openai.health_check.get("interval_seconds", 5))
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    last_error: Exception | None = None
    logged_wait = False

    async with httpx.AsyncClient(timeout=min(10.0, float(timeout_seconds))) as client:
        while True:
            try:
                response = await client.get(health_url)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not payload.get("data"):
                    raise RuntimeError("/models response contains no models")
                if logged_wait:
                    logger.info("OpenAI-compatible endpoint recovered for %s", purpose)
                return
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                last_error = exc
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RuntimeError(
                        f"OpenAI-compatible endpoint is unavailable for {purpose} at "
                        f"{health_url}: {last_error}"
                    ) from last_error
                if not logged_wait:
                    logger.warning(
                        "OpenAI-compatible endpoint unavailable for %s; waiting up to %.0fs: %s",
                        purpose,
                        timeout_seconds,
                        exc,
                    )
                    logged_wait = True
                await asyncio.sleep(min(interval, remaining))


async def verify_browser_preflight(cfg: Any) -> None:
    """Launch the configured Chromium once before benchmark files are created."""
    browsers_path = cfg.environment.browser.get("browsers_path")
    playwright = await WebAgentEnv._ensure_playwright(browsers_path)
    browser = None
    try:
        browser = await playwright.chromium.launch(headless=True)
    except Exception as exc:
        raise RuntimeError(
            f"Playwright Chromium preflight failed for {browsers_path}: {exc}"
        ) from exc
    finally:
        if browser is not None:
            await browser.close()
        await WebAgentEnv._cleanup_playwright()


def _is_infrastructure_error(result: dict) -> bool:
    error = str(result.get("error") or "").lower()
    markers = (
        "connection error",
        "net::err_",
        "endpoint is unavailable",
        "returned no choices",
        "playwright chromium preflight",
        "magento_state_lifecycle.py",
        "executable doesn't exist",
        "could not load credentials",
        "unable to locate credentials",
        "evaluator endpoint",
    )
    return bool(error) and any(marker in error for marker in markers)


class TaskTracer:
    """Tracks task execution with observation and action traces"""

    def __init__(self, task_id: str, output_dir: Path, hydra_config: Any = None):
        self.task_id = task_id
        self.output_dir = output_dir
        self.hydra_config = hydra_config
        self.trace = []
        self.conversation_history = None
        self.start_time = None
        self.end_time = None
        self.task_config = None
        self.result = None

    def start_task(self, task_config: dict):
        """Initialize task tracking"""
        self.start_time = datetime.now()
        self.task_config = task_config
        self.trace = []

    def add_step(self, step_num: int, observation: dict, action: dict, llm_response: str):
        """Add a step to the trace"""
        step_data = {"step": step_num, "timestamp": datetime.now().isoformat(), "observation": observation, "action": action, "llm_response": llm_response}
        self.trace.append(step_data)

    def finish_task(self, result: dict):
        """Finalize task tracking"""
        self.end_time = datetime.now()
        self.result = result

    def save_results(self):
        """Save trace and results to files"""
        task_dir = self.output_dir / f"task_{self.task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)

        # Save trace
        trace_file = task_dir / "trace.json"
        trace_data = {
            "task_id": self.task_id,
            "task_config": self.task_config,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": (self.end_time - self.start_time).total_seconds() if self.start_time and self.end_time else None,
            "trace": self.trace,
        }

        with open(trace_file, "w") as f:
            json.dump(trace_data, f, indent=2, default=str)

        # Save result
        result_file = task_dir / "result.json"
        result_data = {
            "task_id": self.task_id,
            "task_config": self.task_config,
            "result": self.result,
            "execution_time": {"start": self.start_time.isoformat() if self.start_time else None, "end": self.end_time.isoformat() if self.end_time else None, "duration_seconds": (self.end_time - self.start_time).total_seconds() if self.start_time and self.end_time else None},
            "trace_summary": {"total_steps": len(self.trace), "final_score": self.result.get("score", 0.0) if self.result else 0.0, "success": self.result.get("success", False) if self.result else False, "terminated": self.result.get("terminated", False) if self.result else False},
        }

        with open(result_file, "w") as f:
            json.dump(result_data, f, indent=2, default=str)

        # Save Hydra config if available
        if self.hydra_config is not None:
            config_file = task_dir / "config.yaml"
            with open(config_file, "w") as f:
                OmegaConf.save(self.hydra_config, f)

        # Save conversation history if available
        if self.conversation_history is not None:
            session_file = task_dir / "session.json"
            with open(session_file, "w") as f:
                json.dump(self.conversation_history, f, indent=2, default=str)

        logger.info(f"Saved results for task {self.task_id} to {task_dir}")


async def run_single_task(task_id: str, task_config: dict, cfg: Any, output_dir: Path, semaphore: asyncio.Semaphore, launch_semaphore: asyncio.Semaphore, retry_count: int = 3, agent_type: str = "regular") -> dict:
    """Run a single task with tracing and result saving, with retry on agent error

    Args:
        semaphore: Semaphore for overall task concurrency
        launch_semaphore: Semaphore for browser launch concurrency (protects WebAgentEnv.setup)
        agent_type: Type of agent to use - "regular" for WebAgent or "tool" for ToolWebAgent
    """

    async with semaphore:  # Control overall task concurrency
        tracer = TaskTracer(task_id, output_dir, cfg)
        try:
            logger.info(f"Starting task {task_id} with {agent_type} agent: {task_config.get('intent', 'Unknown intent')}")

            # Start tracing once per task
            tracer.start_task(task_config)

            import copy

            last_result = None
            final_attempt = 1

            for attempt in range(1, retry_count + 1):
                health_cfg = _openai_health_config(cfg)
                if health_cfg is not None:
                    await wait_for_openai_health(
                        cfg,
                        timeout_seconds=float(
                            health_cfg.get("recovery_timeout_seconds", 900)
                        ),
                        purpose=f"task {task_id} attempt {attempt}",
                    )

                # Clone config for this attempt to avoid conflicts
                task_cfg = copy.deepcopy(cfg)
                task_cfg.environment.browser.user_data_dir = None
                task_cfg.environment.browser.cache_dir = None

                # Enable tracing for batch tasks and set output to task-specific trace file
                task_cfg.environment.tracing.enabled = True
                task_cfg.environment.tracing.output_path = str(output_dir / f"task_{task_id}" / "trace.zip")

                # Create environment and agent using the proper factory functions
                env = WebAgentEnv(task_cfg.environment)

                # Create agent based on agent_type
                if agent_type == "tool":
                    agent = await create_tool_web_agent(task_cfg.llm, task_cfg.agent)
                else:
                    agent = await create_web_agent(task_cfg.llm, task_cfg.agent)

                try:
                    # Setup environment with task - protected by launch semaphore
                    async with launch_semaphore:
                        await env.setup(task_config)
                        logger.info(f"Task {task_id}: Environment setup complete (attempt {attempt}/{retry_count})")

                    # Run task with appropriate method based on agent type
                    if agent_type == "tool":
                        result = await agent.run_task_with_tools(env, task_config["intent"])
                    else:
                        result = await agent.run_task(env, task_config["intent"])

                    # Extract trace information from agent's action history
                    trace_steps = []

                    # Create trace steps from action history only
                    for step_num, action in enumerate(agent.action_history, 1):
                        step_data = {
                            "step": step_num,
                            "timestamp": datetime.now().isoformat(),  # Approximate timestamp
                            "action": action,
                        }
                        trace_steps.append(step_data)

                    # Add all trace steps to tracer (keep latest attempt)
                    tracer.trace = trace_steps

                    # Add conversation history to tracer (keep latest attempt)
                    tracer.conversation_history = agent.llm_session.to_json()

                    last_result = result
                    metrics = dict(env.rollout_metrics)
                    metrics["agent_time_s"] = float(result.get("agent_time_s", 0.0))
                    metrics["env_time_s"] = round(float(metrics["env_time_s"]), 6)
                    metrics["reward_time_s"] = round(float(metrics["reward_time_s"]), 6)
                    metrics["env_time_per_step_s"] = round(
                        metrics["env_time_s"] / metrics["env_steps"], 6
                    ) if metrics["env_steps"] else 0.0
                    metrics["task_id"] = task_id
                    metrics["score"] = result.get("score", 0.0)
                    last_result["timing"] = metrics
                    final_attempt = attempt

                    # Retry only if agent returned an error, except for ValidationException
                    if ("error" in result) and result["error"]:
                        if any(error_text in result["error"] for error_text in ["ValidationException"]):
                            logger.warning(f"Task {task_id} attempt {attempt} encountered ValidationException, not retrying.")
                            break
                        if attempt < retry_count:
                            logger.warning(f"Task {task_id} attempt {attempt} returned error: {result['error']}. Retrying...")
                            continue
                        logger.warning(f"Task {task_id} attempt {attempt} returned error and no retries left.")

                    # Either success or no error key: finish
                    break

                finally:
                    await env.close()
                    await agent.close()  # Clean up agent resources

            # If we somehow did not set last_result, mark as failure
            if last_result is None:
                last_result = {"success": False, "score": 0.0, "answer": "", "steps": 0, "terminated": False, "error": "Unknown error"}

            # Add number of attempts to result
            last_result["num_attempts"] = final_attempt

            # Finish tracing and return
            tracer.finish_task(last_result)
            logger.info(f"Task {task_id} completed - Success: {last_result['success']}, Score: {last_result['score']}, Attempts: {final_attempt}")
            return last_result

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            # Save error result (use retry_count as num_attempts if we failed completely)
            error_result = {"success": False, "score": 0.0, "answer": "", "steps": 0, "terminated": False, "error": str(e), "num_attempts": retry_count}
            tracer.finish_task(error_result)
            return error_result

        finally:
            # Save results regardless of success/failure
            try:
                tracer.save_results()
            except Exception as e:
                logger.error(f"Failed to save results for task {task_id}: {e}")

async def run_batch_tasks(task_ids: list[str], tasks_dir: Path, output_dir: Path, max_concurrent: int = 3, max_concurrent_launch: int = 1, retry_count: int = 3, agent_type: str = "regular", config_overrides: list[str] = None):
    """Run multiple tasks concurrently

    Args:
        max_concurrent: Maximum number of tasks running concurrently
        max_concurrent_launch: Maximum number of browser launches happening concurrently
        agent_type: Type of agent to use - "regular" for WebAgent or "tool" for ToolWebAgent
    """

    # Load configuration
    config_dir = "../../"
    config_name = "config"

    if GlobalHydra().is_initialized():
        GlobalHydra.instance().clear()

    with initialize(version_base=None, config_path=config_dir):
        overrides = config_overrides or []
        cfg = compose(config_name=config_name, overrides=overrides)
    ConfigStore.set(cfg)

    # Configure logging
    log_level = getattr(logging, cfg.log_level.upper())
    logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # Suppress verbose logging
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    # Do not create task result files if the model service is already down.
    # An unavailable model is a batch-level infrastructure failure, not a
    # collection of benchmark failures.
    health_cfg = _openai_health_config(cfg)
    if health_cfg is not None:
        await wait_for_openai_health(
            cfg,
            timeout_seconds=float(health_cfg.get("preflight_timeout_seconds", 15)),
            purpose="batch preflight",
        )
    await verify_browser_preflight(cfg)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save config before starting the run
    config_file = output_dir / "batch_config.yaml"
    with open(config_file, "w") as f:
        OmegaConf.save(cfg, f)
    logger.info(f"Saved batch configuration to {config_file}")

    # Load task configurations
    task_configs = {}
    for task_id in task_ids:
        task_file = tasks_dir / f"{task_id}.json"
        if not task_file.exists():
            logger.error(f"Task file not found: {task_file}")
            continue

        with open(task_file) as f:
            task_configs[task_id] = json.load(f)

    if not task_configs:
        logger.error("No valid task configurations found")
        return

    logger.info(f"Starting batch execution of {len(task_configs)} tasks with agent_type={agent_type}, max_concurrent={max_concurrent}, max_concurrent_launch={max_concurrent_launch}, retry_count={retry_count}")

    # Create semaphores for concurrency control
    semaphore = asyncio.Semaphore(max_concurrent)  # Overall task concurrency
    launch_semaphore = asyncio.Semaphore(max_concurrent_launch)  # Browser launch concurrency

    # Create tasks
    tasks = []
    for task_id, task_config in task_configs.items():
        task = asyncio.create_task(run_single_task(task_id, task_config, cfg, output_dir, semaphore, launch_semaphore, retry_count, agent_type))
        tasks.append((task_id, task))

    # Run all tasks
    results = {}
    completed = 0
    total = len(tasks)

    for task_id, task in tasks:
        try:
            result = await task
            results[task_id] = result
            completed += 1
            logger.info(f"Progress: {completed}/{total} tasks completed")
        except Exception as e:
            logger.error(f"Task {task_id} failed with exception: {e}")
            results[task_id] = {"success": False, "error": str(e)}
            completed += 1

    # Save batch summary
    summary_file = output_dir / "batch_summary.json"
    infrastructure_error_count = sum(
        1 for result in results.values() if _is_infrastructure_error(result)
    )
    scored_tasks = total - infrastructure_error_count
    success_count = sum(1 for r in results.values() if r.get("success", False))
    summary = {
        "total_tasks": total,
        "completed_tasks": completed,
        "agent_type": agent_type,
        "max_concurrent": max_concurrent,
        "max_concurrent_launch": max_concurrent_launch,
        "results": results,
        "success_count": success_count,
        "infrastructure_error_count": infrastructure_error_count,
        "scored_tasks": scored_tasks,
        "success_rate_scored": success_count / scored_tasks if scored_tasks else None,
        "execution_time": datetime.now().isoformat(),
    }

    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"Batch execution completed with {agent_type} agent. Results saved to {output_dir}")
    if infrastructure_error_count:
        logger.error(
            "Infrastructure-invalid tasks: %d/%d; these are excluded from the scored denominator",
            infrastructure_error_count,
            total,
        )
    logger.info(
        "Scored success rate: %d/%d (%.1f%%)",
        success_count,
        scored_tasks,
        success_count / scored_tasks * 100 if scored_tasks else 0.0,
    )


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully"""
    logger.info(f"Received signal {signum}. Cleaning up...")
    sys.exit(0)


@click.command(context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--task_ids", help="Comma-separated list of task IDs")
@click.option("--sites", help="Comma-separated list of sites")
@click.option("--tasks_dir", default="thirdparty/webarena/config_files", help="Directory containing task JSON files")
@click.option("--output_dir", default="results", help="Output directory for results and traces")
@click.option("--max_concurrent", type=int, default=3, help="Maximum number of concurrent tasks")
@click.option("--max_concurrent_launch", type=int, default=1, help="Maximum number of concurrent browser launches (protects WebAgentEnv.setup)")
@click.option("--retry_count", type=int, default=3, help="Number of retries when agent returns error")
@click.option("--agent_type", type=click.Choice(["regular", "tool"], case_sensitive=False), default="regular", help="Type of agent to use: 'regular' for WebAgent (default) or 'tool' for ToolWebAgent")
@click.pass_context
def main(ctx, task_ids, sites, tasks_dir, output_dir, max_concurrent, max_concurrent_launch, retry_count, agent_type):
    """Main entry point for batch agent execution

    Additional config overrides can be passed as arguments:
    python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3 llm.provider=openai environment.browser.headless=false

    Use --agent_type to switch between regular (default) and tool agents:
    python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3 --agent_type tool

    Control browser launch concurrency separately from task concurrency:
    python -m rl_web_agent.entrypoints.batch_agent --task_ids 1,2,3 --max_concurrent 10 --max_concurrent_launch 2
    """
    # Parse task IDs
    task_ids_list = [tid.strip() for tid in task_ids.split(",")] if task_ids else None
    sites_list = [site.strip() for site in sites.split(",")] if sites else None
    if not task_ids_list and not sites_list:
        click.echo("Either task_ids or sites must be provided")
        sys.exit(1)
    if task_ids_list and sites_list:
        click.echo("Either task_ids or sites must be provided")
        sys.exit(1)

    # Convert paths
    tasks_dir_path = Path(tasks_dir)
    output_dir_path = Path(output_dir)

    if sites_list:
        all_tasks = []
        for i in glob(f"{tasks_dir}/*.json"):
            task = json.load(open(i))
            if len(task["sites"]) == 1 and task["sites"][0] in sites_list:
                all_tasks.append(task)
        task_ids_list = [str(task["task_id"]) for task in all_tasks]
        click.echo(f"Found {len(task_ids_list)} tasks for sites {sites_list}")

    # Parse config overrides from extra arguments
    config_overrides = []
    if ctx.args:
        for arg in ctx.args:
            # Validate that it looks like a config override (contains =)
            if "=" in arg:
                config_overrides.append(arg)
            else:
                click.echo(f"Warning: Ignoring invalid config override '{arg}' (must contain '=')", err=True)

    if config_overrides:
        click.echo(f"Using config overrides: {config_overrides}")

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(run_batch_tasks(task_ids_list, tasks_dir_path, output_dir_path, max_concurrent, max_concurrent_launch, retry_count, agent_type, config_overrides))
    except KeyboardInterrupt:
        logger.info("Batch execution interrupted by user")
    except Exception as e:
        logger.error(f"Batch execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
