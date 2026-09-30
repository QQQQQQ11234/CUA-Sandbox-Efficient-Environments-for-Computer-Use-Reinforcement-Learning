# Selective non-database state isolation

The shared-runtime backend uses a per-app state audit instead of claiming that
database cloning captures an entire application. The machine-readable source
of truth is `experiments/state_audits/gitlab.json`.

## Current correctness boundary

All audited state components in the exact GitLab CE 15.7.5 image now have an
implemented isolation policy. DB mode admits all 180 pure GitLab task IDs in
`experiments/gitlab/gitlab_task_contracts.json`. The manifest is generated
from 41 reviewed intent-template contracts and fully partitions PostgreSQL,
Gitaly, uploads, artifacts, Redis, Sidekiq, and OpenSearch into mutable or
excluded state for every task. An exclusion is a reviewed task-scope claim,
not an isolation mechanism.

The capability gate runs before database creation and fails closed when:

- a task ID has no reviewed contract;
- an app has no state audit;
- a task mutates an unsupported component; or
- a contract overlaps, omits, or leaves a component unreviewed.

Run `python scripts/report_state_isolation.py` to validate and display current
coverage.

The control plane now has three concrete extension points in
`rl_web_agent/isolation/non_db_state.py`:

- `OverlayFilesystemBackend` creates per-environment upper/work/merged trees,
  snapshots upper layers with reflink-capable `cp`, and resets by deleting the
  environment tree. Actual mounts require a dedicated app/Gitaly worker.
- `StateContextPublisher` atomically publishes trusted Redis prefixes,
  OpenSearch index names, and filesystem paths for the selected branch.
- `CommandHookBackend` runs Sidekiq pause/drain/resume and app-specific search
  rebuild commands as argv arrays without a shell.

`DBIsolationManager` invokes these drivers inside the same frozen route window
as the PostgreSQL operation. A driver failure leaves the route frozen. Drivers
are disabled by default. The deployed `shared-gitlab-nondb` image consumes the
filesystem and namespace context when both non-DB flags are enabled.

## Implemented policies for GitLab CE 15.7.5

The shared-worker implementation does not mount OverlayFS per request. Mount
namespaces are process-scoped, so one Puma or Gitaly process cannot safely
switch mounts for concurrent requests.

| Component | Selected policy | Reason |
| --- | --- | --- |
| Gitaly | application-routed relative path plus XFS reflink tree | Preserves one shared Gitaly process while giving every branch a physical repository tree. |
| uploads/artifacts | uploader root routing plus XFS reflink tree | GitLab uploaders resolve a root per operation; reflink preserves the 3.4 GiB base without full copies. |
| Redis business state/session/trace | branch namespace | Authoritative keys survive checkpoint/fork and are deleted on reset/cleanup. |
| Redis cache/rate limit | branch+generation namespace | Derived keys are discarded by generation changes and never cloned. |
| Sidekiq | shared queue with route claims and pending-job barrier | Jobs execute against the routed DB/files/Redis context of their originating request. |
| OpenSearch | require disabled | This CE image has search and indexing disabled; startup fails if that assumption changes. |

Disabling only Rails cache is not a complete Redis policy: GitLab also uses
SharedState, Sessions, TraceChunks, RateLimiting, and direct Cache clients.
Those clients need namespaces even when a top-level cache store is disabled.
The optional `cache_policy=disabled` mode replaces only Rails cache with
NullStore. It passed the same smoke with reward 1.0 in 76.563 seconds; namespace
passed in 76.718 seconds and remains the default because it preserves GitLab
cache semantics. The one-run timing difference is not treated as a performance
result.

`experiments/gitlab/run_non_db_isolation_smoke.py` validates two live
environments with a real repository commit, project upload, artifact write,
all routed Redis classes, a Sidekiq job, checkpoint/fork/reset, and the original
task 411 evaluator. The final 2026-08-09 run completed in 76.718 seconds with
reward 1.0.

`experiments/gitlab/run_background_workflow_smoke.py` validates project fork,
Android template project creation, merge-request creation, repository import,
two-environment isolation, reset, and Rails pool cleanup. The 2026-08-10 run
completed in 229.968 seconds: fork and template evaluators returned 1.0, the
MR state oracle matched both branches and Caroline Stewart, import reached
`finished` with a real Gitaly tree, and 15 revisited Puma workers each retained
exactly one current pool after episode cleanup. The evaluator, agent, and
action parser remain byte-for-byte aligned with `the reference checkout`; full-container baseline
is not a runtime dependency.

## Backend contract

All future state backends must implement the same lifecycle around one route
identity `(environment_id, site, branch_id, generation)`:

```text
freeze HTTP route
-> drain admitted HTTP requests
-> pause branch queue and drain committed jobs
-> close branch DB/cache/storage handles
-> reset, checkpoint, or fork every authoritative backend
-> invalidate or rebuild every derived backend
-> publish the new branch and generation atomically
-> activate queue and HTTP route
```

A lifecycle operation remains frozen if any authoritative backend fails. A
derived rebuild may run before activation or the app must report the index as
not ready; it must never silently query another environment's index.

## GitLab lifecycle policies

| Component | Class | First implementation | Reset | Fork |
| --- | --- | --- | --- | --- |
| Gitaly | authoritative | per-environment repository storage | replace from base snapshot | CoW snapshot/reflink |
| uploads/artifacts | authoritative | dedicated-worker OverlayFS; shared-worker CAS/manifest | delete upper or reset manifest | reflink upper or clone manifest root |
| Redis sessions/business state | authoritative | migrate to PostgreSQL, otherwise explicit namespace | clone with DB or discard namespace | clone authoritative keys |
| Redis cache | derived | `cache:{environment}:{branch}:g{generation}:...` | increment generation | empty child generation |
| OpenSearch | derived | one logical index per environment branch | rebuild from DB | rebuild from child DB |
| Sidekiq | ephemeral/authoritative effects | branch-tagged queues and deterministic drain | drain then discard | drain parent then create child queue |

Redis prefixes contain a hash tag, for example
`cache:{webagent:agent_42:gitlab:left:g7}:`, so multi-key operations remain in
one Redis Cluster slot. Cache reset is a generation change. Authoritative keys
must be cloned by a lifecycle hook during fork, or moved into PostgreSQL; an
empty child prefix is not equivalent to cloning session/cart state.

The middleware exposes the following trusted request values to app-specific
adapters:

```text
web_agent.state_namespace
web_agent.redis_cache_prefix
web_agent.redis_state_prefix
web_agent.opensearch_index
```

The route response also contains these values. They are derived from the
signed route, never from an agent-supplied raw ID.

Do not mount a different OverlayFS view for each request in one shared app
worker. Mount namespaces are process-scoped. A legacy storage path therefore
requires a lightweight worker per environment/branch; the higher-throughput
path is a shared worker with an application-level CAS/manifest storage shim.

For Gitaly, isolating Rails alone is insufficient. The repository upper layer
must be mounted into a dedicated Gitaly process or selected through a
branch-specific Gitaly storage name. Database repository metadata and the
Gitaly storage snapshot must transition under the same lifecycle barrier.

## Enabling compatibility mode

The following shape enables OverlayFS only after a dedicated worker launcher
has been connected to the published merged paths:

```yaml
non_db_state:
  enabled: true
  worker_mode: dedicated
  runtime_root: ./runtime/non_db_state
  overlay:
    enabled: true
    mount_enabled: true
    runtime_root: ./runtime/non_db_overlay
    components:
      uploads: /var/opt/gitlab/gitlab-rails/uploads
      artifacts: /var/opt/gitlab/gitlab-rails/shared/artifacts
      gitaly: /var/opt/gitlab/git-data/repositories
  command_hooks:
    - name: gitlab_jobs_and_search
      enabled: true
      timeout_seconds: 120
      commands:
        quiesce:
          - [/opt/web-agent/bin/pause-and-drain-sidekiq]
        reset:
          - [/opt/web-agent/bin/reset-branch-jobs]
          - [/opt/web-agent/bin/rebuild-search-index]
        fork:
          - [/opt/web-agent/bin/clone-authoritative-redis-keys]
          - [/opt/web-agent/bin/rebuild-search-index]
        activate:
          - [/opt/web-agent/bin/resume-sidekiq]
```

Those executables are deliberately app/version-specific. Enabling the config
without installing them fails the lifecycle and keeps the route frozen.
They receive both source and target context, including
`WEB_AGENT_DATABASE_NAME`, `WEB_AGENT_SOURCE_DATABASE_NAME`, namespace/index
names, lifecycle phase, and checkpoint ID.

## Task expansion rule

A component changes from `unsupported` to `implemented` only after:

1. reset and fork tests cover its state;
2. two or more concurrent environments pass cross-contamination tests;
3. deterministic evaluator and state-oracle checks match the task contract;
   an offline single-tenant trace may be used as an additional regression
   baseline but is not a runtime dependency; and
4. the audit records the exact app/version-specific implementation.

Component support and task admission are separate. After a component is
implemented, each task still needs a reviewed `mutable_components` contract and
a deterministic evaluator/state-oracle check before the task manifest expands.
