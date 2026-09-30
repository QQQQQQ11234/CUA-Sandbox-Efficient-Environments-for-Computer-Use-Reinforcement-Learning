from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class UnsupportedTaskStateError(RuntimeError):
    """Raised when a task touches state the selected sandbox cannot isolate."""


@dataclass(frozen=True)
class StateComponentPolicy:
    name: str
    state_class: str
    strategy: str
    status: str
    reset: str
    fork: str
    notes: str = ""

    @property
    def is_supported(self) -> bool:
        return self.status == "implemented"


@dataclass(frozen=True)
class AppStateAudit:
    app: str
    schema_version: int
    components: dict[str, StateComponentPolicy]

    @classmethod
    def load(cls, path: str | Path) -> "AppStateAudit":
        source = Path(path)
        payload = json.loads(source.read_text())
        if int(payload.get("schema_version", 0)) != 1:
            raise ValueError(f"unsupported state-audit schema in {source}")
        app = str(payload.get("app", "")).strip()
        if not app:
            raise ValueError(f"state audit {source} has no app name")

        policies: dict[str, StateComponentPolicy] = {}
        for raw in payload.get("components", []):
            policy = StateComponentPolicy(
                name=str(raw["name"]),
                state_class=str(raw["state_class"]),
                strategy=str(raw["strategy"]),
                status=str(raw["status"]),
                reset=str(raw["reset"]),
                fork=str(raw["fork"]),
                notes=str(raw.get("notes", "")),
            )
            if policy.name in policies:
                raise ValueError(f"duplicate component {policy.name!r} in {source}")
            if policy.state_class not in {"authoritative", "derived", "ephemeral"}:
                raise ValueError(
                    f"invalid state_class {policy.state_class!r} for {policy.name!r}"
                )
            if policy.status not in {"implemented", "unsupported"}:
                raise ValueError(f"invalid status {policy.status!r} for {policy.name!r}")
            policies[policy.name] = policy
        if not policies:
            raise ValueError(f"state audit {source} has no components")
        return cls(app=app, schema_version=1, components=policies)


@dataclass(frozen=True)
class TaskStateContract:
    task_id: int
    mutable_components: frozenset[str]
    exclusions: frozenset[str]
    review_status: str


class StateCapabilityGate:
    """Fail-closed task admission based on per-app state audits.

    An exclusion is a claim about task scope, not an isolation mechanism. The
    gate therefore admits only explicitly reviewed task IDs and verifies that
    every unimplemented app component is listed as excluded by that task.
    """

    def __init__(
        self,
        audits: Iterable[AppStateAudit],
        contracts: Iterable[TaskStateContract],
    ) -> None:
        self.audits: dict[str, AppStateAudit] = {}
        for audit in audits:
            if audit.app in self.audits:
                raise ValueError(f"duplicate state audit for app {audit.app!r}")
            self.audits[audit.app] = audit
        self.contracts: dict[int, TaskStateContract] = {}
        for contract in contracts:
            if contract.task_id in self.contracts:
                raise ValueError(f"duplicate state contract for task {contract.task_id}")
            self.contracts[contract.task_id] = contract

    @classmethod
    def from_paths(
        cls,
        audit_paths: Iterable[str | Path],
        task_manifest_paths: Iterable[str | Path],
    ) -> "StateCapabilityGate":
        audits = [AppStateAudit.load(path) for path in audit_paths]
        contracts: list[TaskStateContract] = []
        for path in task_manifest_paths:
            source = Path(path)
            payload = json.loads(source.read_text())
            for raw in payload.get("tasks", []):
                contracts.append(
                    TaskStateContract(
                        task_id=int(raw["task_id"]),
                        mutable_components=frozenset(
                            str(value) for value in raw.get("mutable_components", [])
                        ),
                        exclusions=frozenset(
                            str(value) for value in raw.get("exclusions", [])
                        ),
                        review_status=str(raw.get("review_status", "unreviewed")),
                    )
                )
        return cls(audits, contracts)

    def assert_supported(self, task: dict[str, Any]) -> TaskStateContract:
        if task.get("task_id") is None:
            raise UnsupportedTaskStateError(
                "DB sandbox requires a task_id with a reviewed state contract"
            )
        task_id = int(task["task_id"])
        contract = self.contracts.get(task_id)
        if contract is None:
            raise UnsupportedTaskStateError(
                f"task {task_id} is not in an approved state manifest"
            )

        sites = [str(site) for site in task.get("sites", [])]
        if not sites:
            raise UnsupportedTaskStateError(f"task {task_id} declares no app site")
        for site in sites:
            audit = self.audits.get(site)
            if audit is None:
                raise UnsupportedTaskStateError(
                    f"task {task_id} uses {site!r}, which has no state audit"
                )
            unknown = (contract.mutable_components | contract.exclusions).difference(
                audit.components
            )
            if unknown:
                raise UnsupportedTaskStateError(
                    f"task {task_id} references unknown {site} state components: "
                    f"{sorted(unknown)}"
                )
            overlap = contract.mutable_components.intersection(contract.exclusions)
            if overlap:
                raise UnsupportedTaskStateError(
                    f"task {task_id} classifies {site} state as both mutable and excluded: "
                    f"{sorted(overlap)}"
                )
            unclassified = set(audit.components).difference(
                contract.mutable_components | contract.exclusions
            )
            if unclassified:
                raise UnsupportedTaskStateError(
                    f"task {task_id} does not classify {site} state components: "
                    f"{sorted(unclassified)}"
                )
            if contract.review_status == "unreviewed":
                raise UnsupportedTaskStateError(
                    f"task {task_id} state contract is unreviewed"
                )
            unsupported_mutations = {
                name
                for name in contract.mutable_components
                if not audit.components[name].is_supported
            }
            if unsupported_mutations:
                raise UnsupportedTaskStateError(
                    f"task {task_id} mutates unsupported {site} state: "
                    f"{sorted(unsupported_mutations)}"
                )
            missing_exclusions = {
                name
                for name, policy in audit.components.items()
                if not policy.is_supported and name not in contract.exclusions
            }
            if missing_exclusions:
                raise UnsupportedTaskStateError(
                    f"task {task_id} does not exclude unsupported {site} state: "
                    f"{sorted(missing_exclusions)}"
                )
        return contract
