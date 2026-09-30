from __future__ import annotations

import argparse
import json
from pathlib import Path


ALL_COMPONENTS = frozenset(
    {"postgresql", "gitaly", "uploads", "artifacts", "redis", "sidekiq", "opensearch"}
)

# Reviewed against WebArena's 41 pure-GitLab intent templates. Components list
# persistent or derived state that an execution of the template can mutate.
TEMPLATE_CONTRACTS: dict[int, tuple[str, frozenset[str]]] = {
    303: ("todos_read", frozenset()),
    300: ("recent_issues_read", frozenset()),
    349: ("issues_by_label_read", frozenset()),
    322: ("repository_commits_read", frozenset()),
    290: ("assigned_merge_requests_read", frozenset()),
    289: ("contributed_projects_read", frozenset()),
    310: ("latest_updated_issue_read", frozenset()),
    500: ("latest_created_issue_read", frozenset()),
    320: ("user_commits_read", frozenset()),
    325: ("public_projects_read", frozenset()),
    312: ("rss_token_read", frozenset()),
    329: ("clone_command_read", frozenset()),
    321: ("period_commits_read", frozenset()),
    323: ("top_contributor_read", frozenset()),
    324: ("top_contributors_read", frozenset()),
    299: ("opened_issues_read", frozenset()),
    298: ("project_members_read", frozenset()),
    291: ("review_merge_requests_read", frozenset()),
    348: ("merge_request_comment", frozenset({"postgresql", "redis", "sidekiq"})),
    352: ("project_fork", frozenset({"postgresql", "gitaly", "redis", "sidekiq"})),
    355: ("repository_license_commit", frozenset({"postgresql", "gitaly", "redis", "sidekiq"})),
    360: ("merge_request_reply", frozenset({"postgresql", "redis", "sidekiq"})),
    361: ("user_status", frozenset({"postgresql", "redis"})),
    308: ("project_title", frozenset({"postgresql", "redis", "sidekiq"})),
    999: ("issue_assignee", frozenset({"postgresql", "redis", "sidekiq"})),
    331: ("profile_homepage", frozenset({"postgresql", "redis"})),
    292: ("empty_project", frozenset({"postgresql", "gitaly", "redis", "sidekiq"})),
    293: ("project_collaborator", frozenset({"postgresql", "redis", "sidekiq"})),
    294: ("project_guest", frozenset({"postgresql", "redis", "sidekiq"})),
    354: ("project_star", frozenset({"postgresql", "redis"})),
    330: ("user_follow", frozenset({"postgresql", "redis"})),
    351: ("project_members", frozenset({"postgresql", "redis", "sidekiq"})),
    339: ("milestone_create", frozenset({"postgresql", "redis", "sidekiq"})),
    327: ("assigned_issue_create", frozenset({"postgresql", "redis", "sidekiq"})),
    328: ("issue_create", frozenset({"postgresql", "redis", "sidekiq"})),
    335: ("merge_request_create", frozenset({"postgresql", "redis", "sidekiq"})),
    337: ("discussion_issue_create", frozenset({"postgresql", "redis", "sidekiq"})),
    332: ("project_create", frozenset({"postgresql", "gitaly", "redis", "sidekiq"})),
    2100: ("template_project_create", frozenset({"postgresql", "gitaly", "redis", "sidekiq"})),
    316: ("branch_contributor_read", frozenset()),
    600: ("group_create", frozenset({"postgresql", "redis", "sidekiq"})),
}


def build_contract(task: dict) -> dict:
    template_id = int(task["intent_template_id"])
    try:
        category, mutable = TEMPLATE_CONTRACTS[template_id]
    except KeyError as exc:
        raise ValueError(
            f"unreviewed GitLab intent template {template_id} for task {task['task_id']}"
        ) from exc
    if not mutable.issubset(ALL_COMPONENTS):
        raise ValueError(f"template {template_id} references unknown state components")
    return {
        "task_id": int(task["task_id"]),
        "intent_template_id": template_id,
        "category": category,
        "intent": str(task["intent"]),
        "eval_types": list(task["eval"]["eval_types"]),
        "mutable_components": sorted(mutable),
        "exclusions": sorted(ALL_COMPONENTS.difference(mutable)),
        "review_status": "intent_template_reviewed",
    }


def build_manifest(tasks: list[dict]) -> dict:
    gitlab_tasks = [task for task in tasks if task.get("sites") == ["gitlab"]]
    source_templates = {int(task["intent_template_id"]) for task in gitlab_tasks}
    missing_templates = source_templates.difference(TEMPLATE_CONTRACTS)
    stale_templates = set(TEMPLATE_CONTRACTS).difference(source_templates)
    if missing_templates or stale_templates:
        raise ValueError(
            f"template review mismatch: missing={sorted(missing_templates)}, "
            f"stale={sorted(stale_templates)}"
        )
    contracts = sorted((build_contract(task) for task in gitlab_tasks), key=lambda item: item["task_id"])
    if len(contracts) != 180:
        raise ValueError(f"expected 180 pure GitLab tasks, found {len(contracts)}")
    return {
        "selection_policy": "All pure GitLab tasks reviewed by intent template and state component.",
        "task_count": len(contracts),
        "intent_template_count": len(source_templates),
        "components": sorted(ALL_COMPONENTS),
        "tasks": contracts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the reviewed pure-GitLab task manifest")
    parser.add_argument("--tasks", default="thirdparty/webarena/config_files/test.raw.json")
    parser.add_argument("--output", default="experiments/gitlab/gitlab_task_contracts.json")
    args = parser.parse_args()
    manifest = build_manifest(json.loads(Path(args.tasks).read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"wrote {manifest['task_count']} tasks across "
        f"{manifest['intent_template_count']} reviewed templates to {output}"
    )


if __name__ == "__main__":
    main()
