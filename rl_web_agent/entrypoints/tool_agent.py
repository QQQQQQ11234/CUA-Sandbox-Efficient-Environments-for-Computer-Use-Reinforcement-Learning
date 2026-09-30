#!/usr/bin/env python3
"""
Tool Agent - Main entry module for running ToolWebAgent tasks
Run with: python -m rl_web_agent.entrypoints.tool_agent
Override config: python -m rl_web_agent.entrypoints.tool_agent environment.recording.enabled=true
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import hydra
from omegaconf import DictConfig

from rl_web_agent.config_store import ConfigStore
from rl_web_agent.env import WebAgentEnv
from rl_web_agent.tool_agent import create_tool_web_agent

# Logger will be configured after Hydra setup
logger = logging.getLogger(__name__)


async def run_tool_agent_task(cfg: DictConfig):
    """Run the ToolWebAgent until task completion (terminated=True)."""
    temp_user_data_dir = None
    temp_cache_dir = None

    try:
        # Create temporary directories for browser data
        temp_user_data_dir = tempfile.mkdtemp(prefix="webagent_userdata_")
        temp_cache_dir = tempfile.mkdtemp(prefix="webagent_cache_")
        logger.info("Created temporary browser directories:")
        logger.info(f"  User data: {temp_user_data_dir}")
        logger.info(f"  Cache: {temp_cache_dir}")

        # Override browser directories to use temporary ones
        cfg.environment.browser.user_data_dir = temp_user_data_dir
        cfg.environment.browser.cache_dir = temp_cache_dir

        # Load task configuration
        task_config = None
        if hasattr(cfg, "task_config") and cfg.task_config:
            # Load task from specified file
            task_file = Path(cfg.task_config)
            if task_file.exists():
                with open(task_file) as f:
                    task_config = json.load(f)
                logger.info(f"Loaded task from: {task_file}")
            else:
                logger.error(f"Task file not found: {task_file}")
                return
        else:
            # Load default test task
            project_root = Path(__file__).parent.parent.parent
            test_task_path = project_root / "thirdparty" / "webarena" / "config_files" / "0.json"
            if test_task_path.exists():
                with open(test_task_path) as f:
                    task_config = json.load(f)
                logger.info(f"Loaded default test task: {test_task_path}")
            else:
                logger.error(f"Default test task not found: {test_task_path}")
                return

        # Create environment and agent
        env = WebAgentEnv(cfg.environment)
        agent = await create_tool_web_agent(cfg.llm, cfg.agent)

        try:
            # Setup environment with task
            await env.setup(task_config)
            logger.info(f"Environment setup complete for task: {task_config['intent']}")

            # Run the task until completion (terminated=True)
            logger.info("Starting tool agent task execution - will run until terminated")
            result = await agent.run_task_with_tools(env, task_config["intent"])

            # Print final results
            logger.info("=" * 60)
            logger.info("TOOL AGENT TASK COMPLETION SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Success: {result['success']}")
            logger.info(f"Score: {result['score']}")
            logger.info(f"Answer: {result['answer']}")
            logger.info(f"Steps: {result['steps']}")
            logger.info(f"Terminated: {result['terminated']}")
            logger.info(f"Max steps reached: {result['max_steps_reached']}")
            logger.info("=" * 60)

            if result["terminated"]:
                if result["success"]:
                    logger.info("✅ Task completed successfully!")
                else:
                    logger.info("❌ Task terminated but failed")
            else:
                logger.info("⚠️  Task stopped without termination (max steps reached)")

        except Exception as e:
            import traceback

            traceback.print_exc()
            logger.error(f"Task execution failed: {e}")
            raise
        finally:
            # Cleanup environment and agent
            await env.close()
            await agent.close()
            logger.info("Environment and agent closed")

    except Exception as e:
        logger.error(f"Tool agent execution failed: {e}")
        raise
    finally:
        # Clean up temporary directories
        for temp_dir, name in [(temp_user_data_dir, "user data"), (temp_cache_dir, "cache")]:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temporary {name} directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {name} directory: {e}")


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully"""
    logger.info(f"Received signal {signum}. Cleaning up...")
    sys.exit(0)


@hydra.main(version_base=None, config_path="../../", config_name="config")
def main(cfg: DictConfig) -> None:
    """Main entry point with Hydra configuration"""
    # Save config globally for singleton access
    ConfigStore.set(cfg)

    # Configure logging based on config
    log_level = getattr(logging, cfg.log_level.upper())
    logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # Suppress verbose logging
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Log configuration info
    logger.info("Starting ToolWebAgent with configuration:")
    logger.info(f"  Recording enabled: {cfg.environment.recording.enabled}")
    logger.info(f"  Browser headless: {cfg.environment.browser.launch_options.headless}")
    logger.info(f"  Proxy enabled: {cfg.environment.proxy.enabled}")
    logger.info(f"  LLM provider: {cfg.llm.provider}")

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(run_tool_agent_task(cfg))
    except KeyboardInterrupt:
        logger.info("Tool agent interrupted by user")
    except Exception as e:
        logger.error(f"Tool agent execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
