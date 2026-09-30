#!/usr/bin/env python3
"""Build immutable search and warm-cache templates for routed Magento.

The populated Docker image remains the authority.  Elasticsearch templates
are segment clones of its ``magento2_product_1`` index.  Redis templates are
warmed once against a clean reflink DB branch, rewritten to a reserved prefix,
and then cloned into each isolated environment during prepare/reset.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from experiments.system.benchmark_environment_metrics import (
    SITES,
    ours_manager,
    ours_probe,
    ours_task,
)
from rl_web_agent.isolation.non_db_state import StateIdentity
from scripts.magento_state_lifecycle import (
    cache_template_prefix,
    clone_redis_cache,
    ensure_search_template,
    magento_cache_prefix,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTAINERS = {
    "shopping_admin": "shared-shopping-admin",
    "shopping": "shared-shopping",
}


def build(site: str, force_search: bool) -> dict[str, object]:
    spec = SITES[site]
    container = CONTAINERS[site]
    started = time.perf_counter()
    search_template = ensure_search_template(
        container, site, 300, force=force_search
    )
    manager = ours_manager(spec)
    session = None
    try:
        session = manager.prepare_for_task(
            f"derived_state_template_{site}_{int(time.time())}",
            ours_task(spec),
        )
        probes = [ours_probe(spec, session) for _ in range(3)]
        identity = StateIdentity(
            environment_id=session.agent_id,
            site=site,
            branch_id="root",
            generation=1,
            database_name=session.db_name,
        )
        source_cache = magento_cache_prefix(
            identity.environment_id,
            identity.site,
            identity.branch_id,
            identity.generation,
        )
        target_cache = cache_template_prefix(site)
        copied = clone_redis_cache(
            container,
            source_cache,
            target_cache,
            120,
            source_state=identity.search_index,
            target_state=f"webagent_template_{site}",
        )
        if copied <= 0:
            raise RuntimeError(
                f"warming {site} produced no namespaced Redis cache keys"
            )
        return {
            "site": site,
            "container": container,
            "search_template": search_template,
            "redis_template_prefix": target_cache,
            "redis_keys_copied": copied,
            "warmup_probes": probes,
            "seconds": time.perf_counter() - started,
        }
    finally:
        if session is not None:
            manager.cleanup(session)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sites", default="shopping_admin,shopping"
    )
    parser.add_argument("--force-search", action="store_true")
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT / "runtime/magento_derived_state_templates.json"
        ),
    )
    args = parser.parse_args()
    sites = [item.strip() for item in args.sites.split(",") if item.strip()]
    unknown = set(sites).difference(CONTAINERS)
    if unknown:
        raise SystemExit(f"unknown Magento sites: {sorted(unknown)}")
    payload = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sites": [build(site, args.force_search) for site in sites],
    }
    destination = Path(args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(destination)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
