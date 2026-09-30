from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from rl_web_agent.agent import NoProgressLoopDetector, WebAgent
from rl_web_agent.env import WebAgentEnv
from rl_web_agent.utils import format_llm_observation


class ActionParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = WebAgent.__new__(WebAgent)

    def parse(self, response: str) -> dict:
        return json.loads(self.agent._parse_action(response))

    def test_extracts_only_first_action(self) -> None:
        parsed = self.parse(
            'THOUGHT: do one thing\nACTION: {"action":"click","target":"a1"}\n'
            'ACTION: {"action":"back"}'
        )
        self.assertEqual(parsed, {"action": "click", "target": "a1"})

    def test_normalizes_action_name_from_model_output(self) -> None:
        self.assertEqual(
            self.parse('ACTION: {"action":"SELECT OPTION","target":"s","value":"v"}'),
            {"action": "select", "target": "s", "value": "v"},
        )
        self.assertEqual(self.parse('ACTION: {"action":"go_back"}'), {"action": "back"})

    def test_accepts_qwen_zero_argument_action_label(self) -> None:
        self.assertEqual(
            self.parse("THOUGHT: done\nACTION: TERMINATE TASK\n"),
            {"action": "terminate", "answer": ""},
        )

    def test_does_not_invent_parameters_for_click_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "No action found"):
            self.parse("ACTION: CLICK ELEMENT\n")

    def test_rejects_missing_closing_brace(self) -> None:
        with self.assertRaisesRegex(ValueError, "No action found"):
            self.parse('ACTION: {"action":"terminate","answer":"done"')

    def test_field_validation_is_deferred_to_action_execution(self) -> None:
        self.assertEqual(self.parse('ACTION: {"action":"click"}'), {"action": "click"})


class ObservationFormattingTest(unittest.TestCase):
    def test_original_schema_requires_element_lists(self) -> None:
        with self.assertRaises(KeyError):
            format_llm_observation(
                {
                    "html": "<html><body></body></html>",
                    "tabs": [
                        {
                            "id": 0,
                            "title": "",
                            "url": "chrome-error://chromewebdata/",
                            "is_active": True,
                        }
                    ],
                    "error": "navigation failed",
                }
            )


class NoProgressLoopDetectorTest(unittest.TestCase):
    @staticmethod
    def observation(html: str = "<button>Write</button>", score: float = 0.0) -> dict:
        return {
            "html": html,
            "clickable_elements": ["write"],
            "input_elements": [],
            "select_elements": [],
            "tabs": [
                {
                    "id": 0,
                    "title": "Issue",
                    "url": "http://gitlab.test/group/project/-/issues/12",
                    "is_active": True,
                }
            ],
            "score": score,
        }

    def test_three_identical_no_progress_actions_trigger(self) -> None:
        detector = NoProgressLoopDetector(max_repetitions=3)
        observation = self.observation()

        self.assertFalse(detector.observe('{"target":"write","action":"click"}', observation))
        self.assertFalse(detector.observe('{"action":"click","target":"write"}', observation))
        self.assertTrue(detector.observe('{ "action": "click", "target": "write" }', observation))
        self.assertEqual(detector.repetitions, 3)

    def test_changed_page_does_not_change_official_action_repetition_rule(self) -> None:
        detector = NoProgressLoopDetector(max_repetitions=3)
        action = '{"action":"click","target":"write"}'

        detector.observe(action, self.observation())
        detector.observe(action, self.observation())
        self.assertTrue(detector.observe(action, self.observation("<textarea></textarea>")))
        self.assertEqual(detector.repetitions, 3)

    def test_changed_action_resets_repetitions(self) -> None:
        detector = NoProgressLoopDetector(max_repetitions=3)
        observation = self.observation()

        detector.observe('{"action":"click","target":"write"}', observation)
        detector.observe('{"action":"click","target":"write"}', observation)
        self.assertFalse(detector.observe('{"action":"click","target":"preview"}', observation))
        self.assertEqual(detector.repetitions, 1)

    def test_disabled_detector_never_triggers(self) -> None:
        detector = NoProgressLoopDetector(enabled=False, max_repetitions=3)
        for _ in range(10):
            self.assertFalse(
                detector.observe(
                    '{"action":"click","target":"write"}',
                    self.observation(),
                )
            )

    def test_new_detector_resets_state_between_tasks(self) -> None:
        first = NoProgressLoopDetector(max_repetitions=3)
        observation = self.observation()
        action = '{"action":"click","target":"write"}'
        first.observe(action, observation)
        first.observe(action, observation)

        second = NoProgressLoopDetector(max_repetitions=3)
        self.assertFalse(second.observe(action, observation))
        self.assertEqual(second.repetitions, 1)


class ActionExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = WebAgentEnv.__new__(WebAgentEnv)
        self.env.logger = Mock()
        self.env.config = SimpleNamespace(
            browser=SimpleNamespace(
                timeouts=SimpleNamespace(
                    element_wait=10_000,
                    page_load_domcontent=30_000,
                )
            )
        )
        self.env.page = Mock()
        self.env.page.url = "http://127.0.0.1:8023/group/project/-/issues"
        self.env.page.goto = AsyncMock()
        self.env.page.keyboard = Mock()
        self.env.page.keyboard.press = AsyncMock()

    async def test_goto_url_passes_model_url_directly(self) -> None:
        await self.env.goto_url("/group/project/-/project_members")

        self.env.page.goto.assert_awaited_once_with(
            "/group/project/-/project_members",
            wait_until="domcontentloaded",
        )

    async def test_type_enter_uses_element_locator(self) -> None:
        locator = Mock()
        locator.scroll_into_view_if_needed = AsyncMock()
        locator.fill = AsyncMock()
        locator.press = AsyncMock()
        self.env.page.locator.return_value = locator

        await self.env.type("search_gitlab", "primer design", press_enter=True)

        locator.fill.assert_awaited_once_with("primer design", force=True)
        locator.press.assert_awaited_once_with("Enter")
        self.env.page.keyboard.press.assert_not_awaited()

    async def test_key_press_on_element_uses_supported_locator_signature(self) -> None:
        locator = Mock()
        locator.scroll_into_view_if_needed = AsyncMock()
        locator.press = AsyncMock()
        self.env.page.locator.return_value = locator

        await self.env.key_press("ArrowDown", "menu")

        locator.press.assert_awaited_once_with("ArrowDown")


if __name__ == "__main__":
    unittest.main()
