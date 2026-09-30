from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SUPPORTED_RULES = {
    "issue_assignee": re.compile(r"^Assign the issue .* to .+\.$", re.IGNORECASE),
    "project_member": re.compile(r"^Invite .* as collaborator to .+$", re.IGNORECASE),
    "project_star": re.compile(r"^Star the top .* repos? in Gitlab$", re.IGNORECASE),
    "user_follow": re.compile(r"^Follow .* on Gitlab$", re.IGNORECASE),
    "user_status": re.compile(r"^Set my gitlab status as .+\.$", re.IGNORECASE),
}

REPOSITORY_STATE_MARKERS = (
    "/-/raw/",
    "/-/blob/",
    "/-/tree/",
)


def classify(task: dict) -> dict | None:
    if "gitlab" not in task.get("sites", []):
        return None
    intent = str(task.get("intent", ""))
    eval_payload = json.dumps(task.get("eval", {}), sort_keys=True)
    if any(marker in eval_payload for marker in REPOSITORY_STATE_MARKERS):
        return None
    for category, pattern in SUPPORTED_RULES.items():
        if pattern.match(intent):
            return {
                "task_id": int(task["task_id"]),
                "category": category,
                "intent": intent,
                "mutable_components": ["postgresql"],
                "exclusions": [
                    "gitaly",
                    "redis",
                    "sidekiq",
                    "uploads",
                    "artifacts",
                    "opensearch",
                ],
                "review_status": "conservative_auto_selected",
            }
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tasks",
        default="thirdparty/webarena/config_files/test.raw.json",
    )
    parser.add_argument(
        "--output",
        default="experiments/gitlab/gitlab_db_only_tasks.json",
    )
    args = parser.parse_args()

    tasks = json.loads(Path(args.tasks).read_text())
    selected = [entry for task in tasks if (entry := classify(task)) is not None]
    selected.sort(key=lambda entry: entry["task_id"])
    output = {
        "selection_policy": "Conservative DB-state subset; manually review before publication.",
        "task_count": len(selected),
        "tasks": selected,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2) + "\n")
    print(f"wrote {len(selected)} tasks to {output_path}")


if __name__ == "__main__":
    main()
