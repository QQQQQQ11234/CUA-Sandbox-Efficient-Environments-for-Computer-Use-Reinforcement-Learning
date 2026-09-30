from __future__ import annotations

import unittest
from dataclasses import replace

from rl_web_agent.isolation.state_audit import (
    AppStateAudit,
    StateCapabilityGate,
    TaskStateContract,
    UnsupportedTaskStateError,
)


class StateCapabilityGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = AppStateAudit.load("experiments/state_audits/gitlab.json")

    def test_reviewed_task_is_admitted(self) -> None:
        gate = StateCapabilityGate.from_paths(
            ["experiments/state_audits/gitlab.json"],
            ["experiments/gitlab/gitlab_task_contracts.json"],
        )
        contract = gate.assert_supported({"task_id": 418, "sites": ["gitlab"]})
        self.assertEqual(
            contract.mutable_components, frozenset({"postgresql", "redis"})
        )

    def test_unreviewed_task_is_rejected(self) -> None:
        gate = StateCapabilityGate([self.audit], [])
        with self.assertRaisesRegex(UnsupportedTaskStateError, "approved state manifest"):
            gate.assert_supported({"task_id": 999, "sites": ["gitlab"]})

    def audit_with_unsupported_component(self, name: str) -> AppStateAudit:
        components = dict(self.audit.components)
        components[name] = replace(components[name], status="unsupported")
        return replace(self.audit, components=components)

    def test_task_that_mutates_unsupported_state_is_rejected(self) -> None:
        contract = TaskStateContract(
            task_id=1,
            mutable_components=frozenset({"postgresql", "uploads"}),
            exclusions=frozenset(
                {"gitaly", "redis", "sidekiq", "artifacts", "opensearch"}
            ),
            review_status="manual",
        )
        gate = StateCapabilityGate(
            [self.audit_with_unsupported_component("uploads")], [contract]
        )
        with self.assertRaisesRegex(UnsupportedTaskStateError, "uploads"):
            gate.assert_supported({"task_id": 1, "sites": ["gitlab"]})

    def test_incomplete_exclusion_claim_is_rejected(self) -> None:
        contract = TaskStateContract(
            task_id=2,
            mutable_components=frozenset({"postgresql"}),
            exclusions=frozenset(
                {"gitaly", "redis", "sidekiq", "uploads", "artifacts"}
            ),
            review_status="manual",
        )
        gate = StateCapabilityGate(
            [self.audit_with_unsupported_component("opensearch")], [contract]
        )
        with self.assertRaisesRegex(UnsupportedTaskStateError, "opensearch"):
            gate.assert_supported({"task_id": 2, "sites": ["gitlab"]})


if __name__ == "__main__":
    unittest.main()
