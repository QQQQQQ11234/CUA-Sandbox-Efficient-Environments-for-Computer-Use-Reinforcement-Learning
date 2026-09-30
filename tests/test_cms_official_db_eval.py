from __future__ import annotations

import json
import itertools
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from experiments.shopping.run_official_cms_db_eval import (
    CMS_AUTHORITY,
    assert_authenticated_cms_page,
    build_cms_db_environment,
    guard_empty_id_action,
    has_busy_root_observation,
    is_loading_only_observation,
    make_task_configs,
    with_top_k_default,
)


def fake_page(*, url: str, content: str, login_count: int, menu_count: int):
    page = Mock()
    page.url = url
    page.content.return_value = content

    def locator(selector: str):
        result = Mock()
        result.count.return_value = {
            "#login-form": login_count,
            ".admin__menu": menu_count,
        }[selector]
        return result

    page.locator.side_effect = locator
    return page


class CMSOfficialDBEvalTest(TestCase):
    def setUp(self):
        self.clock = 0
        clock_patch = patch(
            "experiments.shopping.run_official_cms_db_eval.time.monotonic",
            side_effect=lambda: self.clock,
        )
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def observation_environment(self, observations, timeout=10):
        getter = Mock(side_effect=itertools.chain(observations, itertools.repeat(observations[-1])))
        base_environment = type("BaseEnvironment", (), {"_get_obs": getter})
        environment_type = build_cms_db_environment(
            base_environment, Mock(), TimeoutError, Mock(), Mock()
        )
        environment = environment_type.__new__(environment_type)
        environment.page = Mock()
        environment.page.is_closed.return_value = False
        environment.page.evaluate.return_value = ""
        def advance_clock(milliseconds):
            self.clock += milliseconds / 1000
        environment.page.wait_for_timeout.side_effect = advance_clock
        environment.observation_retry_timeout_seconds = timeout
        environment.observation_retry_interval_seconds = 0.25
        return environment, getter

    def test_loading_placeholder_is_retried_before_agent_observation(self):
        loading = {"text": "Tab {idx}\n\n[1204] RootWebArea 'Orders' focused: True busy: 1"}
        ready = {"text": "[1204] RootWebArea 'Orders'\n\t[1210] button 'Filters'"}
        environment, getter = self.observation_environment([loading, loading, ready])

        self.assertIs(environment._get_obs(), ready)

        self.assertEqual(getter.call_count, 4)
        self.assertEqual(environment.page.wait_for_timeout.call_count, 3)

    def test_loaded_empty_and_error_pages_are_not_retried(self):
        for text in [
            "[1] RootWebArea 'Empty page'",
            "[1] RootWebArea 'Error'\n\t[2] StaticText 'Access denied'",
            "[1] RootWebArea 'Orders busy: 1' focused: True",
            "",
        ]:
            with self.subTest(text=text):
                observation = {"text": text}
                environment, getter = self.observation_environment([observation])
                self.assertIs(environment._get_obs(), observation)
                getter.assert_called_once()
                environment.page.wait_for_timeout.assert_not_called()

    def test_busy_loading_placeholder_timeout_is_an_infrastructure_error(self):
        loading = {"text": "[1] RootWebArea 'Orders' busy: 1"}
        environment, getter = self.observation_environment([loading, loading], timeout=1)
        with self.assertRaisesRegex(TimeoutError, "busy RootWebArea"):
            environment._get_obs()
        self.assertEqual(self.clock, 1)

    def test_closed_loading_page_does_not_retry(self):
        environment, getter = self.observation_environment([
            {"text": "[1] RootWebArea 'Orders' busy: 1"}
        ])
        environment.page.is_closed.return_value = True
        with self.assertRaises(TimeoutError):
            environment._get_obs()
        getter.assert_called_once()

    def test_existing_transient_observation_errors_still_retry(self):
        ready = {"text": "[1] RootWebArea 'Orders'\n\t[2] button 'Filters'"}
        for error in [ZeroDivisionError(), TimeoutError("DOM snapshot")]:
            with self.subTest(error=type(error).__name__):
                environment, getter = self.observation_environment([error, ready])
                self.assertIs(environment._get_obs(), ready)
                self.assertEqual(getter.call_count, 3)

    def test_populated_but_busy_grid_is_retried(self):
        partial = {"text": "[1] RootWebArea 'Products' busy: 1\n\t[2] link 'CATALOG'"}
        ready = {"text": "[1] RootWebArea 'Products'\n\t[3] table 'Products'"}
        environment, getter = self.observation_environment([partial, ready])
        self.assertIs(environment._get_obs(), ready)
        self.assertEqual(getter.call_count, 3)

    def test_loading_mask_blocks_even_a_non_busy_tree(self):
        ready = {"text": "[1] RootWebArea 'Orders'\n\t[2] table 'Orders'"}
        environment, getter = self.observation_environment([ready])
        environment.page.evaluate.side_effect = itertools.chain(
            ["visible Magento loading mask", "document interactive"],
            itertools.repeat(""),
        )
        self.assertIs(environment._get_obs(), ready)
        self.assertEqual(getter.call_count, 2)
        self.assertEqual(self.clock, 0.75)

    def test_loader_starting_during_snapshot_discards_stale_observation(self):
        stale = {"text": "[1] RootWebArea 'Orders'\n\t[2] button 'Old'"}
        ready = {"text": "[3] RootWebArea 'Orders'\n\t[4] button 'New'"}
        environment, getter = self.observation_environment([stale, ready])
        environment.page.evaluate.side_effect = itertools.chain(
            ["", "visible Magento loading mask"], itertools.repeat(""),
        )
        self.assertIs(environment._get_obs(), ready)
        self.assertEqual(getter.call_count, 3)

    def test_stuck_mask_times_out_without_reading_agent_observation(self):
        environment, getter = self.observation_environment([{}], timeout=1)
        environment.page.evaluate.return_value = "visible Magento loading mask"
        with self.assertRaisesRegex(TimeoutError, "loading mask"):
            environment._get_obs()
        getter.assert_not_called()
        self.assertEqual(self.clock, 1)

    def test_slow_ready_snapshot_does_not_cause_confirmation_timeout(self):
        ready = {"text": "[1] RootWebArea 'Orders'\n\t[2] table 'Orders'"}
        environment, getter = self.observation_environment([ready], timeout=1)
        environment.page.evaluate.side_effect = itertools.chain(
            ["document interactive"], itertools.repeat(""),
        )
        def slow_snapshot():
            self.clock += 2
            return ready
        getter.side_effect = slow_snapshot
        self.assertIs(environment._get_obs(), ready)
        getter.assert_called_once()

    def test_navigation_context_error_is_retried_but_other_errors_propagate(self):
        from playwright.sync_api import Error
        ready = {"text": "[1] RootWebArea 'Products'"}
        environment, _ = self.observation_environment([ready])
        environment.page.evaluate.side_effect = itertools.chain(
            [Error("Execution context was destroyed, most likely because of a navigation")],
            itertools.repeat(""),
        )
        self.assertIs(environment._get_obs(), ready)
        environment.page.evaluate.side_effect = Error("Unrelated browser error")
        with self.assertRaisesRegex(Error, "Unrelated"):
            environment._get_obs()

    def test_busy_root_flag_need_not_be_last_attribute(self):
        self.assertTrue(has_busy_root_observation({
            "text": "[1] RootWebArea 'Products' busy: True focused: True\n\t[2] link 'Menu'"
        }))
        self.assertFalse(has_busy_root_observation({
            "text": "[1] RootWebArea 'busy: True' focused: True"
        }))

    def test_non_transient_observation_error_is_not_swallowed(self):
        environment, getter = self.observation_environment([KeyError("node")])
        with self.assertRaises(KeyError):
            environment._get_obs()
        getter.assert_called_once()

    def test_busy_placeholder_detection_accepts_boolean_busy_flag(self):
        self.assertTrue(is_loading_only_observation({
            "text": "Tab 0 (current): Magento\n\n[1] RootWebArea 'Magento' busy: True"
        }))

    def test_partial_setup_always_exits_sync_playwright(self):
        base_environment = type("BaseEnvironment", (), {})
        environment_type = build_cms_db_environment(
            base_environment,
            Mock(),
            TimeoutError,
            Mock(),
            Mock(),
        )
        environment = environment_type.__new__(environment_type)
        context_manager = MagicMock()
        environment.context_manager = context_manager
        environment._playwright_entered = True
        environment.reset_finished = False

        environment._close_browser()
        environment._close_browser()

        context_manager.__exit__.assert_called_once_with(None, None, None)
        self.assertIsNone(environment.context_manager)
        self.assertFalse(environment._playwright_entered)

    def test_judge_adapter_supplies_top_k_default(self):
        generator = Mock(return_value="correct")

        result = with_top_k_default(generator)(model="judge", messages=[])

        self.assertEqual(result, "correct")
        generator.assert_called_once_with(
            model="judge", messages=[], top_k=-1
        )

    def test_judge_adapter_preserves_explicit_top_k(self):
        generator = Mock(return_value="correct")

        with_top_k_default(generator)(messages=[], top_k=7)

        generator.assert_called_once_with(messages=[], top_k=7)

    def test_empty_action_becomes_value_error(self):
        parser = Mock()

        with self.assertRaisesRegex(ValueError, "Empty action"):
            guard_empty_id_action(parser)("   ")

        parser.assert_not_called()

    def test_parser_index_error_becomes_value_error(self):
        parser = Mock(side_effect=IndexError("list index out of range"))

        with self.assertRaisesRegex(ValueError, "Malformed action"):
            guard_empty_id_action(parser)("click [1]")

    def test_missing_page_client_is_attached_lazily(self):
        base_environment = type("BaseEnvironment", (), {})
        environment_type = build_cms_db_environment(
            base_environment,
            Mock(),
            TimeoutError,
            Mock(),
            Mock(),
        )
        environment = environment_type.__new__(environment_type)
        environment.text_observation_type = "accessibility_tree"
        client = Mock()
        context = Mock()
        context.new_cdp_session.return_value = client
        page = SimpleNamespace(context=context)

        self.assertIs(environment.get_page_client(page), client)
        self.assertIs(page.client, client)
        context.new_cdp_session.assert_called_once_with(page)
        client.send.assert_called_once_with("Accessibility.enable")

    def test_authenticated_page_requires_admin_menu(self):
        page = fake_page(
            url=f"http://{CMS_AUTHORITY}/admin/admin/dashboard/",
            content="<nav class='admin__menu'></nav>",
            login_count=0,
            menu_count=1,
        )

        assert_authenticated_cms_page(page, "test")

    def test_dashboard_url_with_login_form_is_rejected(self):
        page = fake_page(
            url=f"http://{CMS_AUTHORITY}/admin/admin/dashboard/",
            content="Welcome, please sign in",
            login_count=1,
            menu_count=0,
        )

        with self.assertRaisesRegex(RuntimeError, "still on the login page"):
            assert_authenticated_cms_page(page, "test")

    def test_wrong_authority_is_rejected(self):
        page = fake_page(
            url="http://172.17.0.1:7780/admin/admin/dashboard/",
            content="<nav class='admin__menu'></nav>",
            login_count=0,
            menu_count=1,
        )

        with self.assertRaisesRegex(RuntimeError, "changed authority"):
            assert_authenticated_cms_page(page, "test")

    def test_magento_exception_page_is_rejected(self):
        page = fake_page(
            url=f"http://{CMS_AUTHORITY}/admin/catalog/product/edit/id/1481/",
            content="There has been an error processing your request",
            login_count=0,
            menu_count=0,
        )

        with self.assertRaisesRegex(RuntimeError, "Magento exception"):
            assert_authenticated_cms_page(page, "test")

    def test_task_configs_use_the_same_authority_as_the_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "157.json").write_text(
                json.dumps(
                    {
                        "task_id": 157,
                        "sites": ["shopping_admin"],
                        "storage_state": "./.auth/shopping_admin_state.json",
                        "start_url": "http://127.0.0.1:7780/admin",
                        "eval": {
                            "reference_url": (
                                "http://metis.lti.cs.cmu.edu:7780/admin/customer/"
                            )
                        },
                    }
                )
            )
            ids = root / "ids.txt"
            ids.write_text("157\n")

            [config_path] = make_task_configs(ids, tasks)
            config = json.loads(Path(config_path).read_text())

        self.assertEqual(
            config["start_url"], f"http://{CMS_AUTHORITY}/admin"
        )
        self.assertEqual(
            config["eval"]["reference_url"],
            f"http://{CMS_AUTHORITY}/admin/customer/",
        )
        self.assertIsNone(config["storage_state"])


if __name__ == "__main__":
    import unittest

    unittest.main()
