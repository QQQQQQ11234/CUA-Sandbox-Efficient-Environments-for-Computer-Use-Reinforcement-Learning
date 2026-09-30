from __future__ import annotations

import argparse
import json
from pathlib import Path


ALL_COMPONENTS = frozenset(
    {
        "mysql",
        "media",
        "redis_cache",
        "sessions",
        "queue",
        "opensearch",
        "mail",
    }
)

REVIEWED_TEMPLATES = {
    "shopping": frozenset(
        {
            134, 135, 136, 137, 138, 139, 145, 147, 151, 153, 154, 155,
            156, 159, 160, 161, 162, 163, 165, 169, 171, 172, 180, 182,
            186, 188, 189, 191, 193, 194, 196, 197, 199, 204, 206, 207,
            208, 210, 211, 212, 213, 214, 216, 222, 370, 666, 1355, 1356,
        }
    ),
    "shopping_admin": frozenset(
        {
            234, 237, 240, 241, 242, 243, 244, 245, 246, 247, 248, 249,
            250, 251, 252, 253, 255, 256, 257, 258, 266, 268, 270, 271,
            274, 275, 276, 277, 279, 280, 284, 285, 287, 288, 364, 366,
            367, 368, 742, 1001, 1002,
        }
    ),
}

# Reviewed against the pure Shopping and Shopping Admin WebArena intent
# templates. Empty entries are semantically read-only; request cache/session
# churn is isolated by the adapter but is not benchmark-authoritative state.
SHOPPING_MUTATIONS: dict[int, frozenset[str]] = {
    145: frozenset({"mysql", "sessions", "redis_cache"}),
    153: frozenset({"sessions"}),
    154: frozenset({"sessions"}),
    156: frozenset({"mysql", "sessions", "queue", "redis_cache"}),
    # The official template says "Draft an email" and its evaluator checks the
    # value of the contact-page textarea.  It neither submits the form nor
    # sends mail, so this is browser-local draft state rather than app state.
    163: frozenset(),
    165: frozenset({"mysql", "redis_cache"}),
    172: frozenset({"mysql", "sessions", "queue", "redis_cache"}),
    186: frozenset({"mysql", "redis_cache"}),
    189: frozenset({"mysql", "redis_cache"}),
    191: frozenset({"mysql", "queue", "redis_cache"}),
    194: frozenset({"mysql", "queue", "opensearch", "redis_cache"}),
    196: frozenset({"mysql", "redis_cache"}),
    199: frozenset({"mysql", "queue", "redis_cache"}),
}

ADMIN_CATALOG_MUTATIONS = frozenset(
    {237, 241, 242, 247, 251, 252, 256, 287, 742}
)
ADMIN_ORDER_MUTATIONS = frozenset({240, 257, 280, 284})
ADMIN_REVIEW_MUTATIONS = frozenset({243, 246})
ADMIN_OTHER_MUTATIONS = frozenset({258, 275})


def mutable_components(site: str, template_id: int) -> frozenset[str]:
    if site == "shopping":
        return SHOPPING_MUTATIONS.get(template_id, frozenset())
    if template_id in ADMIN_CATALOG_MUTATIONS:
        return frozenset(
            {"mysql", "queue", "opensearch", "redis_cache"}
        )
    if template_id in ADMIN_ORDER_MUTATIONS:
        return frozenset({"mysql", "queue", "redis_cache"})
    if template_id in ADMIN_REVIEW_MUTATIONS:
        return frozenset(
            {"mysql", "queue", "opensearch", "redis_cache"}
        )
    if template_id in ADMIN_OTHER_MUTATIONS:
        return frozenset({"mysql", "redis_cache"})
    if template_id == 266:  # theme preview, not persisted
        return frozenset({"sessions"})
    return frozenset()


def build_contract(task: dict) -> dict:
    sites = task.get("sites", [])
    if len(sites) != 1 or sites[0] not in {"shopping", "shopping_admin"}:
        raise ValueError(f"task {task.get('task_id')} is not a pure Shopping task")
    site = str(sites[0])
    template_id = int(task["intent_template_id"])
    mutable = mutable_components(site, template_id)
    return {
        "task_id": int(task["task_id"]),
        "site": site,
        "intent_template_id": template_id,
        "intent": str(task["intent"]),
        "eval_types": list(task["eval"]["eval_types"]),
        "mutable_components": sorted(mutable),
        "exclusions": sorted(ALL_COMPONENTS.difference(mutable)),
        "review_status": "intent_template_reviewed",
    }


def build_manifest(tasks: list[dict]) -> dict:
    reviewed_mutations = (
        set(SHOPPING_MUTATIONS)
        | set(ADMIN_CATALOG_MUTATIONS)
        | set(ADMIN_ORDER_MUTATIONS)
        | set(ADMIN_REVIEW_MUTATIONS)
        | set(ADMIN_OTHER_MUTATIONS)
        | {266}
    )
    all_reviewed = set().union(*REVIEWED_TEMPLATES.values())
    if not reviewed_mutations.issubset(all_reviewed):
        raise ValueError(
            f"mutation review references unknown templates: "
            f"{sorted(reviewed_mutations - all_reviewed)}"
        )
    selected = [
        task
        for task in tasks
        if task.get("sites") in (["shopping"], ["shopping_admin"])
    ]
    contracts = sorted(
        (build_contract(task) for task in selected),
        key=lambda item: item["task_id"],
    )
    for site, reviewed in REVIEWED_TEMPLATES.items():
        present = {
            int(task["intent_template_id"])
            for task in selected
            if task["sites"] == [site]
        }
        if present != reviewed:
            raise ValueError(
                f"{site} template review mismatch: "
                f"missing={sorted(present - reviewed)}, "
                f"stale={sorted(reviewed - present)}"
            )
    site_counts = {
        site: sum(contract["site"] == site for contract in contracts)
        for site in ("shopping", "shopping_admin")
    }
    if site_counts != {"shopping": 187, "shopping_admin": 182}:
        raise ValueError(f"unexpected Shopping task counts: {site_counts}")
    return {
        "selection_policy": (
            "All 369 pure Shopping/Shopping Admin tasks classified by reviewed "
            "intent template; browser-local contact drafts are read-only app "
            "tasks and actual unsupported mail mutations remain fail-closed."
        ),
        "task_count": len(contracts),
        "site_counts": site_counts,
        "components": sorted(ALL_COMPONENTS),
        "tasks": contracts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build reviewed WebArena Shopping state contracts"
    )
    parser.add_argument(
        "--tasks", default="thirdparty/webarena/config_files/test.raw.json"
    )
    parser.add_argument(
        "--output", default="experiments/shopping/shopping_task_contracts.json"
    )
    args = parser.parse_args()
    manifest = build_manifest(json.loads(Path(args.tasks).read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {manifest['task_count']} Shopping task contracts to {output}")


if __name__ == "__main__":
    main()
