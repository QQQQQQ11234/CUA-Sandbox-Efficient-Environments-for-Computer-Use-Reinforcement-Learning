from __future__ import annotations

import argparse

from rl_web_agent.isolation.state_audit import StateCapabilityGate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and report selective app-state isolation coverage"
    )
    parser.add_argument(
        "--audit",
        action="append",
        default=["experiments/state_audits/gitlab.json"],
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=["experiments/gitlab/gitlab_task_contracts.json"],
    )
    args = parser.parse_args()

    gate = StateCapabilityGate.from_paths(args.audit, args.manifest)
    print("APP\tCOMPONENT\tCLASS\tSTATUS\tSTRATEGY")
    for app, audit in sorted(gate.audits.items()):
        for name, policy in audit.components.items():
            print(
                f"{app}\t{name}\t{policy.state_class}\t"
                f"{policy.status}\t{policy.strategy}"
            )
    print(f"\nReviewed task contracts: {len(gate.contracts)}")


if __name__ == "__main__":
    main()
