"""
Web Agent that uses LLM providers to complete web-based tasks with chain-of-thought reasoning.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

import tiktoken
from omegaconf import DictConfig

from rl_web_agent.env import WebAgentEnv
from rl_web_agent.llm import get_llm_client
from rl_web_agent.utils import format_llm_observation


class Colors:
    """ANSI color codes for terminal output"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    RED = "\033[91m"

    @classmethod
    def highlight_action(cls, text: str) -> str:
        """Highlight action text with colors"""
        return f"{cls.BOLD}{cls.CYAN}🤖 ACTION: {cls.YELLOW}{text}{cls.RESET}"

    @classmethod
    def highlight_step(cls, step: int, text: str) -> str:
        """Highlight step information"""
        return f"{cls.BOLD}{cls.BLUE}📍 Step {step}: {cls.RESET}{text}"

    @classmethod
    def highlight_result(cls, text: str, success: bool = True) -> str:
        """Highlight result text"""
        color = cls.GREEN if success else cls.RED
        icon = "✅" if success else "❌"
        return f"{cls.BOLD}{color}{icon} {text}{cls.RESET}"


class NoProgressLoopDetector:
    """WebArena-compatible repeated-action early stopping."""

    def __init__(self, enabled: bool = True, max_repetitions: int = 3):
        self.enabled = enabled
        self.max_repetitions = max(2, max_repetitions)
        self._actions: list[str] = []
        self.repetitions = 0

    @staticmethod
    def _canonical_action(action_json: str) -> str:
        try:
            action = json.loads(action_json)
        except (TypeError, json.JSONDecodeError):
            return str(action_json).strip()
        return json.dumps(action, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _active_url(observation: dict[str, Any]) -> str:
        tabs = observation.get("tabs") or []
        active_tab = next((tab for tab in tabs if tab.get("is_active")), None)
        return str((active_tab or (tabs[0] if tabs else {})).get("url", ""))

    def observe(self, action_json: str, observation: dict[str, Any]) -> bool:
        if not self.enabled:
            return False

        canonical = self._canonical_action(action_json)
        self._actions.append(canonical)
        try:
            action_name = json.loads(action_json).get("action")
        except (AttributeError, TypeError, json.JSONDecodeError):
            action_name = None
        if action_name == "type":
            self.repetitions = self._actions.count(canonical)
        else:
            self.repetitions = 0
            for previous in reversed(self._actions):
                if previous != canonical:
                    break
                self.repetitions += 1
        return self.repetitions >= self.max_repetitions


class WebAgent:
    """
    LLM-powered web agent that can complete tasks using chain-of-thought reasoning.
    """

    def __init__(self, llm_config: DictConfig, agent_config: DictConfig):
        """
        Initialize the web agent with LLM and agent configuration.

        Args:
            llm_config: Configuration for LLM provider
            agent_config: Configuration for agent behavior
        """
        self.llm_config = llm_config
        self.agent_config = agent_config
        self.llm_provider = None
        self.llm_session = None
        self.logger = logging.getLogger(__name__)
        self.max_steps = agent_config.max_steps
        self.max_obs_length = int(agent_config.get("max_obs_length", 0) or 0)
        self.stateless_observations = bool(
            agent_config.get("stateless_observations", True)
        )
        # Official WebArena falls back to cl100k_base for unknown OpenAI model
        # names such as Qwen/Qwen3.5-9B.
        self.obs_tokenizer = tiktoken.get_encoding("cl100k_base")

        # Action history for tracking previous actions
        self.action_history = []

        # Load prompt templates
        from rl_web_agent.prompts import load_prompt

        self.system_prompt_template = load_prompt("system_cot")

    async def setup(self):
        """Initialize the LLM provider"""
        self.llm_provider = get_llm_client()

    async def close(self):
        """Clean up resources"""
        pass

    async def _initialize_session(self, objective: str) -> None:
        """
        Initialize LLM session with system message containing objective and instructions.

        Args:
            objective: The task objective
        """
        system_message = self.system_prompt_template.format(objective=objective)
        self.llm_session = await self.llm_provider.create_session(system_message)

    def _format_observation(self, observation: dict[str, Any]) -> str:
        """
        Format observation for LLM consumption.

        Args:
            observation: Current page observation

        Returns:
            Formatted observation string
        """
        # Debug: Check observation structure
        self.logger.debug(f"Observation keys: {list(observation.keys())}")
        if "error" in observation and observation["error"]:
            self.logger.warning(f"Observation contains error: {observation['error']}")

        # Build simplified observation representation
        try:
            obs_text = format_llm_observation(observation)
        except Exception as e:
            self.logger.error(f"Error building observation text: {e}")
            self.logger.error(f"Observation content: {observation}")
            raise

        if self.max_obs_length:
            token_ids = self.obs_tokenizer.encode(obs_text)
            if len(token_ids) > self.max_obs_length:
                obs_text = self.obs_tokenizer.decode(
                    token_ids[: self.max_obs_length]
                )
                self.logger.debug(
                    "Truncated observation from %d to %d tokens",
                    len(token_ids),
                    self.max_obs_length,
                )

        previous_action = self.action_history[-1] if self.action_history else "None"
        obs_text += f"\n\nPREVIOUS ACTION: {previous_action}"

        return obs_text

    _ACTION_ALIASES = {
        "click element": "click",
        "type text": "type",
        "hover element": "hover",
        "select option": "select",
        "clear input": "clear",
        "key press": "key_press",
        "navigate to url": "goto_url",
        "go back": "back",
        "go forward": "forward",
        "refresh page": "refresh",
        "new tab": "new_tab",
        "switch tab": "switch_tab",
        "close tab": "close_tab",
        "terminate task": "terminate",
    }

    @classmethod
    def _normalize_action(cls, candidate: dict[str, Any]) -> dict[str, Any]:
        raw_name = candidate.get("action")
        if not isinstance(raw_name, str):
            raise ValueError("Action object has no string action name")
        key = re.sub(r"[\s_-]+", " ", raw_name.strip().lower())
        normalized = dict(candidate)
        normalized["action"] = cls._ACTION_ALIASES.get(key, key.replace(" ", "_"))
        return normalized

    @staticmethod
    def _json_objects(text: str):
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                yield candidate

    def _parse_action(self, response: str) -> str:
        """
        Parse the LLM response to extract the JSON action.

        Args:
            response: LLM response containing thought and action

        Returns:
            JSON action string
        """
        action_match = re.search(r"ACTION:\s*(.*)", response, re.IGNORECASE | re.DOTALL)
        action_text = action_match.group(1) if action_match else response
        for candidate in self._json_objects(action_text):
            if "action" in candidate:
                return json.dumps(
                    self._normalize_action(candidate),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

        # Qwen sometimes emits the documented action label but exhausts its
        # token budget before repeating the zero-argument action as JSON.
        label_match = re.match(r"\s*([A-Za-z][A-Za-z _-]*)\s*(?:\n|$)", action_text)
        if label_match:
            label = re.sub(r"[\s_-]+", " ", label_match.group(1).strip().lower())
            action_name = self._ACTION_ALIASES.get(label, label.replace(" ", "_"))
            if action_name in {"terminate", "back", "forward", "refresh"}:
                action = {"action": action_name}
                if action_name == "terminate":
                    action["answer"] = ""
                return json.dumps(action, separators=(",", ":"))

        raise ValueError("No action found in response")

    async def run_task(self, env: WebAgentEnv, objective: str, max_steps: int = None) -> dict[str, Any]:
        """
        Run a complete task using the web agent with conversation context.

        Args:
            env: WebAgentEnv instance (already set up)
            objective: Task objective description
            max_steps: Maximum number of steps (overrides default)

        Returns:
            Dictionary with task results including final score, answer, and step count
        """
        if not self.llm_provider:
            raise RuntimeError("LLM provider not initialized. Call setup() first.")

        max_steps = max_steps or self.max_steps
        step_count = 0
        self.action_history = []
        loop_config = self.agent_config.get("loop_detection", {})
        loop_detector = NoProgressLoopDetector(
            enabled=bool(loop_config.get("enabled", True)),
            max_repetitions=int(loop_config.get("max_repetitions", 3)),
        )
        loop_detected = False
        parsing_failures = 0

        # Highlight task start
        print("\n" + "=" * 60)
        print(f"{Colors.BOLD}{Colors.MAGENTA}🚀 STARTING WEB AGENT TASK{Colors.RESET}")
        print(f"{Colors.BOLD}🎯 Objective:{Colors.RESET} {objective}")
        print(f"{Colors.BOLD}📏 Max Steps:{Colors.RESET} {max_steps}")
        print("=" * 60 + "\n")

        self.logger.info(f"Starting task: {objective}")

        # Initialize LLM session with system message
        await self._initialize_session(objective)

        # Get initial observation
        observation = await env.observation()

        try:
            for step in range(max_steps):
                step_count += 1
                print(Colors.highlight_step(step_count, "Processing observation"))
                self.logger.info(f"Step {step_count}: Processing observation")

                # Check if task is already terminated
                if observation.get("terminated", False):
                    print(Colors.highlight_result("Task already terminated by environment"))
                    self.logger.info("Task already terminated by environment")
                    break

                # Format observation for LLM
                formatted_observation = self._format_observation(observation)

                # Get LLM response via session
                print(Colors.highlight_step(step_count, "Querying LLM via session"))
                self.logger.info(f"Step {step_count}: Querying LLM via session")
                if self.stateless_observations:
                    self.llm_session.reset_turn_history()
                _agent_started = time.perf_counter()
                response = await self.llm_session.chat_complete(user_message=formatted_observation)
                agent_time_s = getattr(self, "_agent_time_s", 0.0)
                self._agent_time_s = agent_time_s + time.perf_counter() - _agent_started

                self.logger.info(f"LLM Response: {response['content']}")
                if response.get("reasoning_content"):
                    self.logger.debug(f"Reasoning: {response['reasoning_content']}")

                # Parse action from response
                try:
                    action_json = self._parse_action(response["content"])
                except Exception as e:
                    print(Colors.highlight_result(f"Error parsing action from response: {e}", success=False))
                    self.logger.error(f"Error parsing action from response: {e}")
                    self.logger.error(f"Full LLM response: {response}")
                    parsing_failures += 1
                    if parsing_failures >= 3:
                        self.logger.warning("Failed to parse actions for 3 consecutive steps")
                        break
                    continue
                parsing_failures = 0

                # Highlight the action being executed
                print(Colors.highlight_action(action_json))
                self.logger.info(f"Step {step_count}: Executing action: {action_json}")

                # Track action history
                self.action_history.append(action_json)

                # Execute action and get next observation
                observation = await env.step(action_json)

                # Check if task is terminated after step
                if observation["terminated"]:
                    print(Colors.highlight_result("Task terminated"))
                    self.logger.info("Task terminated")
                    break

                if loop_detector.observe(action_json, observation):
                    loop_detected = True
                    active_url = loop_detector._active_url(observation)
                    self.logger.warning(
                        "Repeated-action early stop after %d repetitions: "
                        "action=%s url=%s",
                        loop_detector.repetitions,
                        loop_detector._canonical_action(action_json),
                        active_url,
                    )
                    print(
                        Colors.highlight_result(
                            f"Repeated-action early stop after {loop_detector.repetitions} repetitions",
                            success=False,
                        )
                    )
                    break

                # Brief pause to allow page to update
                await asyncio.sleep(0.5)

            # Get final results
            final_observation = await env.observation(skip_evaluation=False)
            final_score = final_observation["score"]
            final_answer = final_observation["model_answer"]
            terminated = final_observation["terminated"]

            result = {
                "success": final_score == 1.0,
                "score": final_score,
                "answer": final_answer,
                "steps": step_count,
                "terminated": terminated,
                "max_steps_reached": step_count >= max_steps and not loop_detected,
                "loop_detected": loop_detected,
                "loop_repetitions": loop_detector.repetitions if loop_detected else 0,
                "agent_time_s": round(getattr(self, "_agent_time_s", 0.0), 6),
            }

            # Highlight final results
            success = result["success"]
            print("\n" + "=" * 60)
            print(Colors.highlight_result(f"TASK COMPLETED - {'SUCCESS' if success else 'FAILED'}", success=success))
            print(f"{Colors.BOLD}📊 Final Score:{Colors.RESET} {final_score}")
            print(f"{Colors.BOLD}📝 Final Answer:{Colors.RESET} {final_answer}")
            print(f"{Colors.BOLD}👣 Steps Taken:{Colors.RESET} {step_count}")
            print(f"{Colors.BOLD}🏁 Terminated:{Colors.RESET} {terminated}")
            if result["max_steps_reached"]:
                print(f"{Colors.YELLOW}⚠️  Max steps reached{Colors.RESET}")
            print("=" * 60 + "\n")

            self.logger.info(f"Task completed: {result}")
            return result

        except Exception as e:
            import traceback

            # Highlight error
            print("\n" + "=" * 60)
            print(Colors.highlight_result("TASK FAILED WITH ERROR", success=False))
            print(f"{Colors.RED}💥 Error: {str(e)}{Colors.RESET}")
            print("=" * 60 + "\n")

            self.logger.error(f"Error during task execution: {e}")
            self.logger.error(f"Full traceback: {traceback.format_exc()}")
            return {"success": False, "score": 0.0, "answer": f"Error: {str(e)}", "steps": step_count, "terminated": False, "max_steps_reached": False, "error": str(e)}


async def create_web_agent(llm_config: DictConfig, agent_config: DictConfig) -> WebAgent:
    """
    Create and initialize a WebAgent.

    Args:
        llm_config: LLM configuration
        agent_config: Agent behavior configuration

    Returns:
        Initialized WebAgent instance
    """
    agent = WebAgent(llm_config, agent_config)
    await agent.setup()
    return agent
