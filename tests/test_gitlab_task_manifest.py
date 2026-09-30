from __future__ import annotations

import json
import unittest
from pathlib import Path

from experiments.gitlab.build_task_manifest import ALL_COMPONENTS, TEMPLATE_CONTRACTS, build_manifest
from rl_web_agent.isolation.state_audit import StateCapabilityGate


class GitLabTaskManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = json.loads(
            Path("thirdparty/webarena/config_files/test.raw.json").read_text()
        )
        cls.manifest = json.loads(
            Path("experiments/gitlab/gitlab_task_contracts.json").read_text()
        )

    def test_manifest_is_reproducible_and_complete(self) -> None:
        self.assertEqual(self.manifest, build_manifest(self.source))
        self.assertEqual(self.manifest["task_count"], 180)
        self.assertEqual(self.manifest["intent_template_count"], 41)
        self.assertEqual(len(TEMPLATE_CONTRACTS), 41)

    def test_every_component_is_classified_exactly_once(self) -> None:
        for contract in self.manifest["tasks"]:
            mutable = set(contract["mutable_components"])
            exclusions = set(contract["exclusions"])
            self.assertFalse(mutable.intersection(exclusions), contract["task_id"])
            self.assertEqual(mutable | exclusions, set(ALL_COMPONENTS), contract["task_id"])
            self.assertEqual(contract["review_status"], "intent_template_reviewed")

    def test_every_pure_gitlab_task_is_admitted(self) -> None:
        gate = StateCapabilityGate.from_paths(
            ["experiments/state_audits/gitlab.json"],
            ["experiments/gitlab/gitlab_task_contracts.json"],
        )
        tasks = [task for task in self.source if task.get("sites") == ["gitlab"]]
        for task in tasks:
            gate.assert_supported(task)


if __name__ == "__main__":
    unittest.main()
