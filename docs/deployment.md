# Deployment guide

This guide separates the application image from the CUA-Sandbox control plane.
The application process is shared; state backends and the route registry are
private to the logical environment.

## PostgreSQL/GitLab

1. Build the shared GitLab image from the exact WebArena version. Record the
   GitLab, Rails, ActiveRecord, and PostgreSQL versions.
2. Export and restore the WebArena database into a template database. Do not
   copy an old PostgreSQL `PGDATA` directory into a different major version.
3. Start the route registry with a secret of at least 32 random bytes.
4. Configure the Rails middleware to verify `X-Agent-Route` and use the route's
   request-local pool. Never call global `ActiveRecord::Base.establish_connection`
   per request.
5. Regenerate and review the task manifest. DB mode admits only tasks whose
   database, Redis, queues, uploads, artifacts, Gitaly, and search state are
   covered by adapters or explicitly excluded by the contract.

The exact Rails adapter and smoke-test sequence are documented in
`gitlab_shared/README.md`.

## MySQL/Shopping

MySQL has no PostgreSQL-style `CREATE DATABASE ... TEMPLATE` primitive. The
MySQL backend therefore requires a quiesced template on XFS with reflink
support. `MySQLXFSReflinkManager` creates a private reflink branch and runs a
small mysqld instance for the active branch while the Magento/PHP service stays
shared.

Prepare the host and templates with the scripts under `scripts/` and read
`docs/operations/MYSQL_QUICKSTART.md` before enabling `mysql_xfs`. The application must consume
`db_engine`, `db_host`, `db_port`, and `db_name` from the trusted route. The
browser HTTP target remains the shared Magento service.

## Readiness and rollback

A capsule is not published until every contracted component reports ready and
its generation is recorded. If any adapter fails, leave the old route active,
clean the staged resources, and surface the failure to the batch runner. Never
publish a database branch without its matching file, cache, profile, and
background-worker bindings.

Use the smoke and lifecycle tests after each image or adapter change:

```bash
python -m pytest -q tests/test_route_lifecycle.py tests/test_state_audit.py
python scripts/verify_db_routing_mvp.py
```
