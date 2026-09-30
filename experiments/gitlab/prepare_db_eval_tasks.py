from __future__ import annotations

import argparse
import json
from pathlib import Path


def replace_gitlab_url(value: object, gitlab_url: str) -> object:
    if isinstance(value, str):
        return value.replace("__GITLAB__", gitlab_url)
    if isinstance(value, list):
        return [replace_gitlab_url(item, gitlab_url) for item in value]
    if isinstance(value, dict):
        return {
            key: replace_gitlab_url(item, gitlab_url)
            for key, item in value.items()
        }
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write all reviewed pure-GitLab WebArena tasks as individual JSON files"
    )
    parser.add_argument(
        "--tasks",
        default="thirdparty/webarena/config_files/test.raw.json",
    )
    parser.add_argument(
        "--manifest",
        default="experiments/gitlab/gitlab_task_contracts.json",
    )
    parser.add_argument(
        "--output-dir",
        default="runtime/gitlab_eval_tasks",
    )
    parser.add_argument(
        "--gitlab-url",
        default="http://metis.lti.cs.cmu.edu:8023",
    )
    args = parser.parse_args()

    tasks = json.loads(Path(args.tasks).read_text())
    manifest = json.loads(Path(args.manifest).read_text())
    selected_ids = {int(entry["task_id"]) for entry in manifest["tasks"]}
    selected = {
        int(task["task_id"]): task
        for task in tasks
        if int(task["task_id"]) in selected_ids
    }

    missing = selected_ids.difference(selected)
    if missing:
        raise ValueError(f"tasks missing from source data: {sorted(missing)}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for task_id in sorted(selected):
        output_path = output_dir / f"{task_id}.json"
        task = replace_gitlab_url(selected[task_id], args.gitlab_url)
        output_path.write_text(json.dumps(task, indent=2) + "\n")

    print(f"wrote {len(selected)} reviewed GitLab tasks to {output_dir}")


if __name__ == "__main__":
    main()
