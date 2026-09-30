# Shared WebArena GitLab

This directory derives a shared GitLab image from the exact WebArena image and
provides the trusted routing components required by the database-per-agent
backend. It does not claim that arbitrary GitLab versions support dynamic
database pools without adaptation.

## Required order

1. Run `scripts/inspect_gitlab_image.sh` on the exact WebArena image.
2. Record GitLab, Ruby, Rails, ActiveRecord, and embedded PostgreSQL versions.
3. Verify the included `GitLab157ActiveRecordAdapter` against the exact image.
4. Prove two concurrent requests never see the other agent database.
5. Only then install `DatabaseRouter` as the first Rails middleware.

Never call global `ActiveRecord::Base.establish_connection` per request. It is
not a request-local operation and will cross-route concurrent agents.

## Request routing and lifecycle barrier

The browser never selects a database. It presents an HMAC-signed
`X-Agent-Route` capability. The first Rails middleware verifies that capability,
then atomically acquires a request lease from the registry. The acquired route
contains:

```text
environment_id, site, branch_id, generation, db_name, lifecycle_state
```

ActiveRecord pools are keyed by
`environment:site:branch:generation:database`. Request-local state and the
lease are always cleared when the Rack call returns. A frozen route rejects new
requests with HTTP 503 while already admitted requests drain.

Database reset/checkpoint/fork uses this ordering:

```text
freeze route -> wait for in-flight requests = 0 -> terminate idle DB sessions
-> clone database -> publish branch/database/generation -> activate route
```

The registry's `POST /v1/routes/:agent/acquire` operation performs the
ACTIVE check and request-counter increment in one SQLite write transaction.
This prevents a request from entering between the freeze and drain steps.
PgBouncer remains an optional downstream connection pool; it does not infer an
HTTP environment identity.

For the local browser compatibility path, CUA-Sandbox injects the signed header
only for configured task origins. A production deployment should move header
injection into a trusted ingress and strip any client-supplied routing headers.

The inspected WebArena image is GitLab CE 15.7.5 with ActiveRecord 6.1.6.1. The
included adapter registers a separate `role + shard` pool with the connection
handler and wraps requests in `ActiveRecord::Base.connected_to`. Its embedded
PostgreSQL is older than PostgreSQL 15, so evaluation clones use
`CREATE DATABASE ... TEMPLATE ...` without a `STRATEGY` clause.

GitLab resolves routes through the internal HTTP registry service because the
Omnibus image does not ship a loadable `sqlite3` Ruby gem. Start it with:

```bash
WEB_AGENT_ROUTE_REGISTRY_SECRET='<at least 32 bytes>' \
python -m rl_web_agent.entrypoints.route_registry_server \
  --registry-path ./runtime/db_routes.sqlite3 \
  --host 0.0.0.0 --port 8765
```

Omnibus runit services do not inherit arbitrary `docker -e` variables. Mount a
root-readable config at `/etc/web-agent/config.json`:

```json
{
  "route_secret": "at-least-32-bytes",
  "route_registry_url": "http://host.docker.internal:8765",
  "route_registry_secret": "another-at-least-32-byte-secret"
}
```

The active capability boundary is the generated 180-task manifest at
`experiments/gitlab/gitlab_task_contracts.json`. The legacy 25-task DB-only
manifest remains only as a historical conservative baseline.

DB mode enforces this boundary with the machine-readable per-app state audit
and task capability gate. See [NON_DB_STATE.md](NON_DB_STATE.md) for the current
coverage, lifecycle contract, and backend expansion criteria.

Non-DB drivers are coordinated with PostgreSQL reset/checkpoint/fork, but are
disabled by default. Do not mark a component implemented merely because its
driver exists: the exact GitLab worker, Redis client, Gitaly storage, and queue
consumer must also consume the trusted route context.

The optional Magento/MySQL path follows the same state-vector model. MySQL has
no PostgreSQL-18 database CoW primitive, so `mysql_xfs` creates a reflink copy
of a quiesced template and starts one small host `mysqld` only for the active
logical branch. The shared Magento/PHP service must consume the trusted route's
`db_engine`, `db_host`, `db_port`, and `db_name`; the database endpoint is never
used as the browser HTTP target. Checkpoint, fork, reset, and cleanup are
implemented at the branch-tree level, but Shopping/CMS admission remains
fail-closed until an app-specific Magento adapter and state audit cover media,
Redis, queues, and search. See `docs/operations/MYSQL_XFS_CONFIG_EXAMPLE.yaml`.

## Template database

Export the quiesced WebArena GitLab database with
`scripts/export_webarena_gitlab_db.sh`, then import it into PostgreSQL 18 and
create `gitlab_base_template` with `scripts/create_template_db.sh`. These scripts
use logical dump/restore and never copy an old PostgreSQL `PGDATA` directory into
PostgreSQL 18.

## Reward correctness

Regenerate the reviewed task list with
`python experiments/gitlab/build_task_manifest.py`. Validate shared-runtime
execution against the same evaluator used by `the reference checkout` and a
deterministic state oracle for each admitted task. A recorded single-tenant
Docker/full-container baseline trace can be replayed as an optional migration regression
baseline, but the container-free runtime does not depend on either backend.

## Local DB-isolated accuracy run

The local WebArena GitLab image uses its embedded PostgreSQL server. Create the
`gitlab_base_template` database once, keep the `web-agent-route-registry`
sidecar healthy, and run only the database-isolation backend with:

```bash
OPENAI_API_KEY='...' \
LLM_PROVIDER=openai \
OPENAI_MODEL=gpt-4o \
./scripts/run_gitlab_db_accuracy.sh
```

Set `TASK_IDS=418,419,420,421,422` for a small status-task run. By default the
wrapper runs all 180 reviewed pure GitLab tasks and writes the measured success
rate to `batch_summary.json`. It never starts the full-container baseline backend.
