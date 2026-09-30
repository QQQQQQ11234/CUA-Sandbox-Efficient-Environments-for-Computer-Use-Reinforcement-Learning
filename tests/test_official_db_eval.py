from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from experiments.gitlab.run_official_db_eval import (
    build_db_environment,
    make_task_configs,
    official_namespace,
)


class OfficialDBEvalTest(TestCase):
    def test_observation_retries_transient_official_page_load_race(self):
        class FakePlaywrightTimeout(Exception):
            pass

        class FakeOfficialEnvironment:
            def _get_obs(self):
                self.fetch_count += 1
                if self.fetch_count == 1:
                    raise FakePlaywrightTimeout("official 500ms timeout")
                return {"text": "stable accessibility tree"}

        page = Mock()
        page.is_closed.return_value = False
        environment_cls = build_db_environment(
            FakeOfficialEnvironment,
            sync_playwright_fn=Mock(),
            playwright_timeout_cls=FakePlaywrightTimeout,
            db_manager_cls=Mock(),
            logger=Mock(),
        )
        environment = object.__new__(environment_cls)
        environment.page = page
        environment.fetch_count = 0
        environment.observation_retry_timeout_seconds = 1
        environment.observation_retry_interval_seconds = 0

        observation = environment._get_obs()

        self.assertEqual(
            observation, {"text": "stable accessibility tree"}
        )
        self.assertEqual(environment.fetch_count, 2)
        page.wait_for_load_state.assert_called_once()
        args, kwargs = page.wait_for_load_state.call_args
        self.assertEqual(args, ("domcontentloaded",))
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 1000)

    def test_official_namespace_matches_qwen_webarena_settings(self):
        args = argparse.Namespace(
            task_ids_file="ids.txt",
            result_dir="results",
            instruction_path="agent/prompts/jsons/p_cot_id_actree_2s.json",
            model="Qwen/Qwen3.5-9B",
            temperature=1.0,
            top_p=0.9,
            max_tokens=384,
            max_steps=30,
            max_retry=1,
            max_obs_length=1920,
        )

        official = official_namespace(args)

        self.assertEqual(official.action_set_tag, "id_accessibility_tree")
        self.assertEqual(official.observation_type, "accessibility_tree")
        self.assertTrue(official.current_viewport_only)
        self.assertEqual(
            (official.viewport_width, official.viewport_height), (1280, 720)
        )
        self.assertEqual(
            official.instruction_path,
            "agent/prompts/jsons/p_cot_id_actree_2s.json",
        )
        self.assertEqual(official.parsing_failure_th, 3)
        self.assertEqual(official.repeating_action_failure_th, 3)
        self.assertEqual(official.temperature, 1.0)
        self.assertEqual(official.top_p, 0.9)
        self.assertEqual(official.top_k, -1)
        self.assertEqual(official.max_tokens, 384)
        self.assertEqual(official.max_steps, 30)
        self.assertEqual(official.max_retry, 1)
        self.assertEqual(official.max_obs_length, 1920)
        self.assertEqual((official.test_start_idx, official.test_end_idx), (0, 1000))
        self.assertEqual(official.sleep_after_execution, 2.0)
        self.assertTrue(official.save_trace_enabled)
        self.assertTrue(official.render_screenshot)

    def test_make_task_configs_accepts_comma_and_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            tasks_dir = tmp_path / "tasks"
            tasks_dir.mkdir()
            for task_id in (44, 448):
                (tasks_dir / f"{task_id}.json").write_text(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "storage_state": "./.auth/gitlab_state.json",
                        }
                    )
                )
            ids_file = tmp_path / "ids.txt"
            ids_file.write_text("44,\n448\n")

            configs = make_task_configs(ids_file, tasks_dir)

            self.assertEqual(
                [json.loads(Path(path).read_text())["task_id"] for path in configs],
                [44, 448],
            )
            self.assertTrue(
                all(
                    json.loads(Path(path).read_text())["storage_state"] is None
                    for path in configs
                )
            )

    def test_make_task_configs_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            ids_file = tmp_path / "ids.txt"
            ids_file.write_text("44 44")

            with self.assertRaisesRegex(ValueError, "duplicate"):
                make_task_configs(ids_file, tmp_path)
