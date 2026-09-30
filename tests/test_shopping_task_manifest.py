from __future__ import annotations

import json
import unittest
from pathlib import Path

from experiments.shopping.build_task_manifest import build_manifest
from rl_web_agent.isolation.state_audit import (
    StateCapabilityGate,
    UnsupportedTaskStateError,
)


class ShoppingTaskManifestTest(unittest.TestCase):
    def test_manifest_covers_all_pure_shopping_tasks(self) -> None:
        tasks = json.loads(
            Path("thirdparty/webarena/config_files/test.raw.json").read_text()
        )
        manifest = build_manifest(tasks)
        self.assertEqual(manifest["task_count"], 369)
        self.assertEqual(
            manifest["site_counts"],
            {"shopping": 187, "shopping_admin": 182},
        )

    def test_reviewed_mysql_task_is_admitted(self) -> None:
        gate = StateCapabilityGate.from_paths(
            [
                "experiments/state_audits/shopping.json",
                "experiments/state_audits/shopping_admin.json",
            ],
            ["experiments/shopping/shopping_task_contracts.json"],
        )
        contract = gate.assert_supported(
            {"task_id": 423, "sites": ["shopping_admin"]}
        )
        self.assertIn("mysql", contract.mutable_components)

    def test_contact_draft_tasks_are_admitted_without_mail_mutation(self) -> None:
        tasks = json.loads(
            Path("thirdparty/webarena/config_files/test.raw.json").read_text()
        )
        draft_tasks = [
            task
            for task in tasks
            if task.get("sites") == ["shopping"]
            and int(task["intent_template_id"]) == 163
        ]
        self.assertEqual(
            [int(task["task_id"]) for task in draft_tasks],
            [689, 690, 691, 692, 693],
        )
        gate = StateCapabilityGate.from_paths(
            ["experiments/state_audits/shopping.json"],
            ["experiments/shopping/shopping_task_contracts.json"],
        )
        for task in draft_tasks:
            contract = gate.assert_supported(task)
            self.assertNotIn("mail", contract.mutable_components)
            self.assertIn("mail", contract.exclusions)

    def test_actual_mail_mutation_remains_fail_closed(self) -> None:
        gate = StateCapabilityGate.from_paths(
            ["experiments/state_audits/shopping.json"],
            ["experiments/shopping/shopping_task_contracts.json"],
        )
        contract = gate.contracts[689]
        gate.contracts[689] = type(contract)(
            task_id=contract.task_id,
            mutable_components=frozenset({"mail"}),
            exclusions=contract.exclusions.difference({"mail"}),
            review_status=contract.review_status,
        )
        with self.assertRaisesRegex(UnsupportedTaskStateError, "mail"):
            gate.assert_supported({"task_id": 689, "sites": ["shopping"]})


if __name__ == "__main__":
    unittest.main()
