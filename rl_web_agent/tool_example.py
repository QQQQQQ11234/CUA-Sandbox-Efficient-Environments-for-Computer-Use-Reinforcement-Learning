"""
Example usage of the tool-enabled web agent with function calling.
Demonstrates how to use the step_browser tool for automated web interactions.
"""

import asyncio
import logging

import hydra
from omegaconf import DictConfig

from rl_web_agent.env import WebAgentEnv
from rl_web_agent.tool_agent import create_tool_web_agent

# Example task configuration
EXAMPLE_TASK_CONFIG = {"sites": ["shopping"], "task_id": 1, "require_login": False, "start_url": "http://metis.lti.cs.cmu.edu:7770", "intent": "Find the price of Bliss Lemon Sage Hand Cream", "eval": {"eval_types": ["string_match"], "reference_answers": {"exact_match": "$24.00"}}}


async def run_tool_agent_example(cfg: DictConfig):
    """
    Example of running a tool-enabled web agent.

    Args:
        cfg: Hydra configuration containing environment and LLM settings
    """
    logger = logging.getLogger(__name__)

    # Create environment and agent
    env = WebAgentEnv(cfg.environment)
    agent = await create_tool_web_agent(cfg.llm, cfg.agent)

    try:
        # Setup environment with task
        logger.info("Setting up environment...")
        await env.setup(EXAMPLE_TASK_CONFIG)

        # Run task with tool-enabled agent
        logger.info("Starting tool-enabled agent task...")
        objective = EXAMPLE_TASK_CONFIG["intent"]
        result = await agent.run_task_with_tools(env, objective, max_steps=20)

        # Display results
        print("\n" + "=" * 60)
        print("🏁 TASK COMPLETED")
        print("=" * 60)
        print(f"Success: {result['success']}")
        print(f"Score: {result['score']}")
        print(f"Answer: {result['answer']}")
        print(f"Steps taken: {result['steps']}")
        print(f"Terminated: {result['terminated']}")
        if result.get("error"):
            print(f"Error: {result['error']}")
        print("=" * 60)

        return result

    except Exception as e:
        logger.error(f"Error during task execution: {e}")
        return {"success": False, "error": str(e)}
    finally:
        # Cleanup
        await agent.close()
        await env.close()


@hydra.main(version_base=None, config_path="conf", config_name="config")
def main(cfg: DictConfig) -> None:
    """Main entry point for tool agent example"""
    logging.basicConfig(level=cfg.log_level)

    # Suppress verbose logging
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)

    # Run the tool agent example
    result = asyncio.run(run_tool_agent_example(cfg))

    if result["success"]:
        print("✅ Tool agent completed successfully!")
    else:
        print("❌ Tool agent failed!")
        if result.get("error"):
            print(f"Error: {result['error']}")


if __name__ == "__main__":
    main()
