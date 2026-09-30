"""
Tool-enabled Web Agent that uses function calling for browser automation.
Integrates with the WebAgentEnv using LLM function calling capabilities.
"""

import asyncio
import json
import logging
import time
from typing import Any

from omegaconf import DictConfig

from rl_web_agent.env import WebAgentEnv
from rl_web_agent.llm import get_llm_client
from rl_web_agent.prompts import load_prompt
from rl_web_agent.utils import format_llm_observation


class ToolWebAgent:
    """
    Web agent that uses LLM function calling to interact with browser environment.
    """

    def __init__(self, llm_config: DictConfig, agent_config: DictConfig):
        """
        Initialize the tool-enabled web agent.

        Args:
            llm_config: Configuration for LLM provider
            agent_config: Configuration for agent behavior
        """
        self.llm_config = llm_config
        self.agent_config = agent_config
        self.llm_provider = None
        self.llm_session = None
        self.logger = logging.getLogger(__name__)
        self.max_steps = agent_config["max_steps"]
        self.env = None

        # Action history for tracking previous actions
        self.action_history = []

    async def setup(self):
        """Initialize the LLM provider"""
        self.llm_provider = get_llm_client()

    async def close(self):
        """Clean up resources"""
        pass

    def get_browser_tools(self) -> list[dict[str, Any]]:
        """
        Define the browser step tool for function calling.

        Returns:
            List of tool definitions following OpenAI function calling format
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "step_browser",
                    "description": "Execute an action in the web browser environment and get the resulting observation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "object",
                                "description": """Browser action to execute. Available actions:

1. CLICK ELEMENT - Click on any clickable element
   {"action": "click", "target": "semantic_id"}
   - target: The semantic ID of the element to click (REQUIRED)

2. TYPE TEXT - Type text into input fields with optional Enter key press
   {"action": "type", "target": "semantic_id", "text": "content", "enter": true}
   - target: The semantic ID of the input element (REQUIRED)
   - text: The text content to type (REQUIRED)
   - enter: Whether to press Enter after typing (OPTIONAL, default: false)

3. HOVER ELEMENT - Hover over an element to trigger tooltips or dropdowns
   {"action": "hover", "target": "semantic_id"}
   - target: The semantic ID of the element to hover over (REQUIRED)

4. SELECT OPTION - Select an option from dropdown/select elements
   {"action": "select", "target": "semantic_id", "value": "option_value"}
   - target: The semantic ID of the select element (REQUIRED)
   - value: The value of the option to select (REQUIRED)

5. CLEAR INPUT - Clear the content of an input element
   {"action": "clear", "target": "semantic_id"}
   - target: The semantic ID of the input element to clear (REQUIRED)

6. KEY PRESS - Press keyboard keys, optionally on specific elements
   {"action": "key_press", "key": "Enter", "target": "semantic_id"}
   - key: The key to press (e.g., "Enter", "Escape", "Tab", "ArrowDown") (REQUIRED)
   - target: The semantic ID of element to focus before key press (OPTIONAL)

7. NAVIGATE TO URL - Navigate to a specific URL in current tab
   {"action": "goto_url", "url": "https://example.com"}
   - url: The URL to navigate to (REQUIRED)

8. GO BACK - Navigate back in browser history
   {"action": "back"}
   - No parameters required

9. GO FORWARD - Navigate forward in browser history
   {"action": "forward"}
   - No parameters required

10. REFRESH PAGE - Reload the current page
    {"action": "refresh"}
    - No parameters required

11. NEW TAB - Open a new browser tab
    {"action": "new_tab", "url": "https://example.com"}
    - url: URL to open in the new tab (OPTIONAL, opens blank tab if not provided)

12. SWITCH TAB - Switch to a different browser tab
    {"action": "switch_tab", "tab_id": 0}
    - tab_id: The ID number of the tab to switch to (REQUIRED, starts from 0)

13. CLOSE TAB - Close a specific browser tab
    {"action": "close_tab", "tab_id": 0}
    - tab_id: The ID number of the tab to close (REQUIRED, starts from 0)

14. TERMINATE TASK - End the task with final answer
    {"action": "terminate", "answer": "final answer or result"}
    - answer: Your final answer or result for the task (OPTIONAL, use empty string if no specific answer)""",
                                "properties": {
                                    "action": {"type": "string", "description": "Type of action to perform", "enum": ["click", "type", "hover", "select", "clear", "key_press", "goto_url", "back", "forward", "refresh", "new_tab", "switch_tab", "close_tab", "terminate"]},
                                    "target": {"type": "string", "description": "Semantic ID of the element to interact with (use data-semantic-id attribute)"},
                                    "text": {"type": "string", "description": "Text to type (for type action)"},
                                    "enter": {"type": "boolean", "description": "Whether to press Enter after typing (for type action)"},
                                    "value": {"type": "string", "description": "Value to select (for select action)"},
                                    "key": {"type": "string", "description": "Key to press (for key_press action) - e.g., 'Enter', 'Escape', 'Tab', 'ArrowDown'"},
                                    "url": {"type": "string", "description": "URL to navigate to (for goto_url/new_tab actions)"},
                                    "tab_id": {"type": "integer", "description": "Tab ID for tab operations (starts from 0)"},
                                    "answer": {"type": "string", "description": "Final answer (for terminate action)"},
                                },
                                "required": ["action"],
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
        ]

    async def step_browser(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a browser action and return the observation.

        Args:
            action: Dictionary containing browser action with 'action' field and parameters

        Returns:
            Dictionary containing the observation and execution status
        """
        if not self.env:
            return {"error": "Browser environment not initialized", "success": False}

        try:
            self.logger.info(f"Executing browser action: {action}")

            # Convert action dict to JSON string for the environment
            action_json = json.dumps(action)

            # Execute the action in the environment
            observation = await self.env.step(action_json)

            # Format the observation for the LLM
            formatted_obs = format_llm_observation(observation)

            return {"success": True, "observation": formatted_obs, "terminated": observation["terminated"], "score": observation["score"], "error": observation["error"]}

        except Exception as e:
            self.logger.error(f"Error executing browser action: {e}")
            return {"success": False, "error": str(e), "observation": None}

    async def get_function_by_name(self, name: str):
        """Get function implementation by name for tool calling."""
        if name == "step_browser":
            return self.step_browser
        else:
            raise ValueError(f"Unknown function: {name}")

    async def run_task_with_tools(self, env: WebAgentEnv, objective: str, max_steps: int = None) -> dict[str, Any]:
        """
        Run a task using function calling approach.

        Args:
            env: WebAgentEnv instance (already set up)
            objective: Task objective description
            max_steps: Maximum number of steps

        Returns:
            Dictionary with task results
        """
        if not self.llm_provider:
            raise RuntimeError("LLM provider not initialized. Call setup() first.")

        self.env = env
        max_steps = max_steps or self.max_steps
        step_count = 0

        print("\n🚀 Starting tool-enabled web agent task")
        print(f"🎯 Objective: {objective}")
        print(f"📏 Max Steps: {max_steps}\n")

        # Initialize session with system prompt and objective
        system_prompt = load_prompt("tool_system").format(objective=objective)
        self.llm_session = await self.llm_provider.create_session(system_prompt)

        tools = self.get_browser_tools()

        # Get initial observation
        observation = await env.observation()

        try:
            # Check if task is already terminated
            if observation.get("terminated", False):
                print("✅ Task already terminated")
                self.logger.info("Task already terminated by environment")
            else:
                # Format initial observation and send as first user message
                obs_text = format_llm_observation(observation)
                user_message = f"Here is the initial state of the browser:\n\n{obs_text}\n\nPlease start working towards the objective."

                # First step: send initial observation as user message
                _agent_started = time.perf_counter()
                response = await self.llm_session.chat_complete_with_tools(user_message=user_message, tools=tools)
                self._agent_time_s = time.perf_counter() - _agent_started

                for step in range(max_steps):
                    step_count += 1
                    print(f"📍 Step {step_count}: Processing tool call...")
                    self.logger.info(f"Step {step_count}: Processing tool call")

                    # Check if task is already terminated
                    if observation.get("terminated", False):
                        print("✅ Task terminated")
                        self.logger.info("Task terminated")
                        break

                    # Process tool calls - expect exactly one
                    tool_calls = response.get("tool_calls", [])
                    if len(tool_calls) != 1:
                        raise ValueError(f"Tool agent expected exactly 1 tool call but got {len(tool_calls)}. Response: {response}")

                    tool_call = tool_calls[0]
                    function_name = tool_call["function"]["name"]
                    function_args = json.loads(tool_call["function"]["arguments"])
                    if isinstance(function_args, str):
                        # load one more time just in case
                        function_args = json.loads(function_args)

                    print(f"🔧 Calling {function_name} with args: {function_args}")
                    self.logger.info(f"Step {step_count}: Calling {function_name} with args: {function_args}")

                    # Track action in history
                    action_json = json.dumps(function_args["action"]) if "action" in function_args else json.dumps(function_args)
                    self.action_history.append(action_json)

                    # Execute the function
                    func = await self.get_function_by_name(function_name)
                    result = await func(**function_args)

                    # Get updated observation after action
                    observation = await env.observation()

                    # Check if task is terminated after step
                    if observation["terminated"]:
                        print("✅ Task terminated")
                        self.logger.info("Task terminated")
                        break

                    formatted_obs = format_llm_observation(observation)

                    # Send updated observation as tool response and get next response
                    tool_response = {"content": formatted_obs, "tool_call_id": tool_call["id"]}
                    _agent_started = time.perf_counter()
                    response = await self.llm_session.chat_complete_with_tools(tool_response=tool_response, tools=tools)
                    self._agent_time_s += time.perf_counter() - _agent_started

                    # Brief pause to allow page to update
                    await asyncio.sleep(0.5)

            # Get final results
            final_observation = await env.observation(skip_evaluation=False)
            final_score = final_observation["score"]
            final_answer = final_observation["model_answer"]
            terminated = final_observation["terminated"]

            result = {"success": final_score == 1.0, "score": final_score, "answer": final_answer, "steps": step_count, "terminated": terminated, "max_steps_reached": step_count >= max_steps, "agent_time_s": round(getattr(self, "_agent_time_s", 0.0), 6)}

            self.logger.info(f"Task completed: {result}")
            return result

        except Exception as e:
            import traceback

            traceback.print_exc()
            self.logger.error(f"Error during task execution: {e}")
            self.logger.error(f"Full traceback: {traceback.format_exc()}")
            return {"success": False, "score": 0.0, "answer": f"Error: {str(e)}", "steps": step_count, "terminated": False, "max_steps_reached": False, "error": str(e)}


async def create_tool_web_agent(llm_config: DictConfig, agent_config: DictConfig) -> ToolWebAgent:
    """
    Create and initialize a ToolWebAgent.

    Args:
        llm_config: LLM configuration
        agent_config: Agent configuration

    Returns:
        Initialized ToolWebAgent instance
    """
    agent = ToolWebAgent(llm_config, agent_config)
    await agent.setup()
    return agent
