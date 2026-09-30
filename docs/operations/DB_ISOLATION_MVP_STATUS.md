# Database Isolation MVP Status

## Verified

- Multiple agents can share one ASGI application process.
- Each request verifies a short-lived HMAC `X-Agent-Route` token and resolves
  its signed agent ID through a cross-process SQLite registry.
- A client-supplied `X-Agent-DB` value is ignored.
- Updating one agent route switches only that agent to a forked database.
- Removing an agent route immediately prevents further resolution.
- CUA-Sandbox tracks the canonical database, checkpoints, and branches for cleanup.

Run the routing verification with:

```bash
uv run python scripts/verify_db_routing_mvp.py
```

## Not Yet Verified

- PostgreSQL 18 `FILE_COPY` clone behavior on XFS/Btrfs/ZFS reflink storage.
- A real shared WebArena application using the routed PostgreSQL connector.
- Magento/MySQL, GitLab, Redis, uploads, background workers, and search indexes.
- Request draining while reset/checkpoint terminates database connections.
- Deployment-level secret distribution and reverse-proxy boundary hardening.
- Reward equivalence against the original full-container baseline backend.

## Current Conclusion

The control-plane architecture is feasible for a shared Python/PostgreSQL web
application: CUA-Sandbox can publish per-agent routes, and a separate shared app
process can resolve them without trusting a client-selected database name.
This is an architectural MVP, not yet a complete WebArena replacement or a
production security boundary.
