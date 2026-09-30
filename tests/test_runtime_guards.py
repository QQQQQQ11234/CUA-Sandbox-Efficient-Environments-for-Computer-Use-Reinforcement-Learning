from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

import tiktoken

from rl_web_agent.agent import WebAgent
from rl_web_agent.entrypoints.batch_agent import _is_infrastructure_error
from rl_web_agent.helper_functions import HelperFunctions
from rl_web_agent.llm import LLMProtocolError, OpenAISession, _first_openai_choice


class OpenAIResponseGuardTest(TestCase):
    def test_missing_choices_is_protocol_error(self):
        for choices in (None, []):
            with self.assertRaisesRegex(LLMProtocolError, "no choices"):
                _first_openai_choice(SimpleNamespace(choices=choices), "test")

    def test_valid_choice_is_returned(self):
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        self.assertIs(
            _first_openai_choice(SimpleNamespace(choices=[choice]), "test"),
            choice,
        )

    def test_invalid_evaluator_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "no OpenAI-compatible choices"):
            HelperFunctions._evaluator_response_content(
                SimpleNamespace(choices=None)
            )


class BatchSummaryGuardTest(TestCase):
    def test_infrastructure_failures_are_not_scored(self):
        self.assertTrue(_is_infrastructure_error({"error": "Connection error."}))
        self.assertTrue(
            _is_infrastructure_error(
                {"error": "OpenAI-compatible endpoint returned no choices"}
            )
        )
        self.assertTrue(
            _is_infrastructure_error(
                {"error": "Page.goto: net::ERR_PROXY_CONNECTION_FAILED"}
            )
        )
        self.assertTrue(
            _is_infrastructure_error(
                {
                    "error": "Command ['python3', "
                    "'scripts/magento_state_lifecycle.py', 'prepare'] failed"
                }
            )
        )
        self.assertFalse(
            _is_infrastructure_error({"error": "No action found in response"})
        )
        self.assertFalse(_is_infrastructure_error({"score": 0.0}))


class OfficialPromptAlignmentTest(TestCase):
    def test_openai_session_can_drop_prior_browser_turns(self):
        session = OpenAISession(SimpleNamespace(), {}, "system")
        session.messages.extend(
            [
                {"role": "user", "content": "old observation"},
                {"role": "assistant", "content": "old action"},
            ]
        )
        session.reset_turn_history()
        self.assertEqual(
            session.messages, [{"role": "system", "content": "system"}]
        )

    def test_observation_uses_official_token_limit(self):
        agent = WebAgent.__new__(WebAgent)
        agent.logger = Mock()
        agent.max_obs_length = 32
        agent.obs_tokenizer = tiktoken.get_encoding("cl100k_base")
        agent.action_history = ['{"action":"click","target":"old"}']
        observation = {
            "html": "<div>" + ("content " * 200) + "</div>",
            "clickable_elements": [],
            "input_elements": [],
            "select_elements": [],
            "tabs": [
                {
                    "id": 0,
                    "title": "Page",
                    "url": "http://gitlab.test/project",
                    "is_active": True,
                }
            ],
        }
        formatted = agent._format_observation(observation)
        observation_part, previous_action = formatted.rsplit(
            "\n\nPREVIOUS ACTION: ", 1
        )
        self.assertLessEqual(len(agent.obs_tokenizer.encode(observation_part)), 32)
        self.assertEqual(previous_action, agent.action_history[-1])
