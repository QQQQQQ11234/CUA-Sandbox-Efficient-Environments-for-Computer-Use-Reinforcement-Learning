#!/usr/bin/env python3
"""
Replay Agent - Replay trajectories from batch agent traces
Run with: python -m rl_web_agent.entrypoints.replay --trace_file results/task_506/trace.json
"""

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from hydra import compose, initialize
from hydra.core.global_hydra import GlobalHydra

from rl_web_agent.env import WebAgentEnv

logger = logging.getLogger(__name__)


class TrajectoryReplayer:
    """Replays a trajectory from a batch agent trace file"""

    def __init__(self, trace_file: Path, config: Any, headless: bool = False, delay: float = 1.0):
        self.trace_file = trace_file
        self.config = config
        self.headless = headless
        self.delay = delay  # Delay between steps in seconds
        self.env = None
        self.trace_data = None

    async def load_trace(self) -> dict:
        """Load trace data from JSON file"""
        logger.info(f"Loading trace from {self.trace_file}")

        if not self.trace_file.exists():
            raise FileNotFoundError(f"Trace file not found: {self.trace_file}")

        with open(self.trace_file) as f:
            self.trace_data = json.load(f)

        logger.info(f"Loaded trace for task {self.trace_data['task_id']} with {len(self.trace_data['trace'])} steps")
        return self.trace_data

    async def setup_environment(self) -> None:
        """Setup the environment with the same configuration as the original task"""
        # Create temporary directories for browser data
        temp_user_data_dir = tempfile.mkdtemp(prefix="webagent_replay_userdata_")
        temp_cache_dir = tempfile.mkdtemp(prefix="webagent_replay_cache_")

        # Clone config and update for replay
        import copy

        replay_config = copy.deepcopy(self.config)

        # Set browser directories
        replay_config.environment.browser.user_data_dir = temp_user_data_dir
        replay_config.environment.browser.cache_dir = temp_cache_dir

        # Override headless setting if specified
        if self.headless:
            replay_config.environment.browser.launch_options.headless = True
        else:
            replay_config.environment.browser.launch_options.headless = False

        # Disable evaluation during replay (we're just replaying actions)
        replay_config.environment.evaluation.enabled = False

        # Create environment
        self.env = WebAgentEnv(replay_config.environment)

        # Setup environment with the original task config
        task_config = self.trace_data["task_config"]
        await self.env.setup(task_config)

        logger.info(f"Environment setup complete for task: {task_config.get('intent', 'Unknown')}")

    async def replay_trajectory(self, interactive: bool = False, compare_observations: bool = False) -> dict:
        """
        Replay the trajectory step by step

        Args:
            interactive: If True, wait for user input before each step
            compare_observations: If True, compare current observations with original trace

        Returns:
            dict: Summary of replay results
        """
        if not self.trace_data:
            raise ValueError("Trace data not loaded. Call load_trace() first.")

        trace_steps = self.trace_data["trace"]
        replay_results = {"task_id": self.trace_data["task_id"], "total_steps": len(trace_steps), "replayed_steps": 0, "errors": [], "observations_match": [], "success": False}

        logger.info(f"Starting replay of {len(trace_steps)} steps")

        # Get initial observation
        if compare_observations:
            _ = await self.env.observation()
            logger.info("Initial observation captured")

        for i, step_data in enumerate(trace_steps, 1):
            try:
                logger.info(f"=== Step {i}/{len(trace_steps)} ===")

                action = step_data["action"]
                original_llm_response = step_data.get("llm_response", "")

                logger.info(f"Action: {json.dumps(action, indent=2)}")
                if original_llm_response:
                    logger.info(f"Original LLM Response: {original_llm_response[:200]}...")

                # Interactive mode - wait for user input
                if interactive:
                    user_input = input("Press Enter to execute this step, 'q' to quit, 's' to skip: ").strip().lower()
                    if user_input == "q":
                        logger.info("Replay stopped by user")
                        break
                    elif user_input == "s":
                        logger.info("Step skipped by user")
                        continue

                # Execute the action
                try:
                    action_json = json.dumps(action)
                    new_obs = await self.env.step(action_json)

                    if new_obs.get("error"):
                        error_msg = f"Step {i}: Action execution error: {new_obs['error']}"
                        logger.error(error_msg)
                        replay_results["errors"].append(error_msg)
                    else:
                        logger.info(f"Step {i}: Action executed successfully")
                        replay_results["replayed_steps"] += 1

                    # Compare observations if requested
                    if compare_observations and i < len(trace_steps):
                        # Note: Original trace may have simplified observation data
                        # We can at least compare basic metrics like page title, URL, etc.
                        comparison = self._compare_observations(new_obs, step_data.get("observation", {}))
                        replay_results["observations_match"].append(comparison)

                        if comparison["matches"]:
                            logger.info("✅ Observation matches expected state")
                        else:
                            logger.warning(f"⚠️ Observation differences: {comparison['differences']}")

                except json.JSONDecodeError as e:
                    error_msg = f"Step {i}: Invalid action JSON: {e}"
                    logger.error(error_msg)
                    replay_results["errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"Step {i}: Action execution failed: {e}"
                    logger.error(error_msg)
                    replay_results["errors"].append(error_msg)

                # Add delay between steps
                if self.delay > 0:
                    await asyncio.sleep(self.delay)

            except Exception as e:
                error_msg = f"Step {i}: Unexpected error: {e}"
                logger.error(error_msg)
                replay_results["errors"].append(error_msg)

        # Check if we successfully replayed all steps
        replay_results["success"] = replay_results["replayed_steps"] == len(trace_steps) and len(replay_results["errors"]) == 0

        logger.info(f"Replay completed: {replay_results['replayed_steps']}/{replay_results['total_steps']} steps successful")
        if replay_results["errors"]:
            logger.warning(f"Encountered {len(replay_results['errors'])} errors during replay")

        return replay_results

    def _compare_observations(self, current_obs: dict, original_obs: dict) -> dict:
        """
        Compare current observation with original observation

        Returns:
            dict: Comparison results with matches and differences
        """
        comparison = {"matches": True, "differences": []}

        # Note: Original trace has simplified observation data
        # We can compare what's available

        # Compare basic page info if available
        if "url" in current_obs and "url" in original_obs:
            if current_obs["url"] != original_obs["url"]:
                comparison["matches"] = False
                comparison["differences"].append(f"URL differs: {current_obs['url']} vs {original_obs['url']}")

        # Compare page title if available in both
        _ = current_obs.get("tabs", [{}])[0].get("title", "") if current_obs.get("tabs") else ""
        # Original observation might not have structured data, so this is best-effort

        return comparison

    async def save_replay_results(self, results: dict, output_file: Path) -> None:
        """Save replay results to file"""
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w") as f:
            json.dump(results, f, indent=2, default=str)

        logger.info(f"Replay results saved to {output_file}")

    async def close(self) -> None:
        """Clean up resources"""
        if self.env:
            await self.env.close()


async def replay_trace(trace_file: Path, output_file: Path | None = None, headless: bool = False, interactive: bool = False, compare_observations: bool = False, delay: float = 1.0) -> None:
    """
    Main replay function

    Args:
        trace_file: Path to trace.json file to replay
        output_file: Optional path to save replay results
        headless: Run browser in headless mode
        interactive: Wait for user input before each step
        compare_observations: Compare observations with original trace
        delay: Delay between steps in seconds
    """
    # Load configuration
    config_dir = "../../rl_web_agent/conf"
    config_name = "config"

    if GlobalHydra().is_initialized():
        GlobalHydra.instance().clear()

    with initialize(version_base=None, config_path=config_dir):
        cfg = compose(config_name=config_name)

    # Configure logging
    log_level = getattr(logging, cfg.log_level.upper())
    logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # Suppress verbose logging
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    replayer = TrajectoryReplayer(trace_file, cfg, headless=headless, delay=delay)

    try:
        # Load trace data
        await replayer.load_trace()

        # Setup environment
        await replayer.setup_environment()

        # Replay trajectory
        results = await replayer.replay_trajectory(interactive=interactive, compare_observations=compare_observations)

        # Save results if output file specified
        if output_file:
            await replayer.save_replay_results(results, output_file)

        # Print summary
        print("\n=== REPLAY SUMMARY ===")
        print(f"Task ID: {results['task_id']}")
        print(f"Steps replayed: {results['replayed_steps']}/{results['total_steps']}")
        print(f"Success: {results['success']}")
        print(f"Errors: {len(results['errors'])}")

        if results["errors"]:
            print("\nErrors encountered:")
            for error in results["errors"][:5]:  # Show first 5 errors
                print(f"  - {error}")
            if len(results["errors"]) > 5:
                print(f"  ... and {len(results['errors']) - 5} more errors")

        return results

    finally:
        await replayer.close()


def main():
    """Main entry point for replay script"""
    parser = argparse.ArgumentParser(description="Replay WebAgent task trajectories")
    parser.add_argument("--trace_file", required=True, help="Path to trace.json file to replay")
    parser.add_argument("--output_file", help="Path to save replay results (optional)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode - wait for user input before each step")
    parser.add_argument("--compare_observations", action="store_true", help="Compare observations with original trace")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between steps in seconds (default: 1.0)")

    args = parser.parse_args()

    trace_file = Path(args.trace_file)
    output_file = Path(args.output_file) if args.output_file else None

    try:
        asyncio.run(replay_trace(trace_file=trace_file, output_file=output_file, headless=args.headless, interactive=args.interactive, compare_observations=args.compare_observations, delay=args.delay))
    except KeyboardInterrupt:
        logger.info("Replay interrupted by user")
    except Exception as e:
        logger.error(f"Replay failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
