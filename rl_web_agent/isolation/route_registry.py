from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


ACTIVE = "ACTIVE"
FROZEN = "FROZEN"
_VALID_STATES = {ACTIVE, FROZEN}


class RouteFrozenError(RuntimeError):
    pass


class RouteBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteRecord:
    agent_id: str
    environment_id: str
    site: str
    branch_id: str
    generation: int
    db_name: str
    db_engine: str
    db_host: str
    db_port: int
    lifecycle_state: str
    inflight_requests: int
    pending_background_jobs: int

    @property
    def pool_key(self) -> str:
        return (
            f"{self.environment_id}:{self.site}:{self.branch_id}:"
            f"{self.generation}:{self.db_name}"
        )

    @property
    def state_namespace(self) -> str:
        return (
            f"webagent:{self.environment_id}:{self.site}:{self.branch_id}:"
            f"g{self.generation}"
        )

    @property
    def search_index(self) -> str:
        # Search is derived but expensive. It is isolated per logical branch
        # and survives cache-generation bumps within that branch.
        return (
            f"webagent_{self.environment_id}_{self.site}_{self.branch_id}"
            .replace(":", "_")
            .replace(".", "_")
            .lower()
        )

    @property
    def state_path_token(self) -> str:
        # Environment and branch IDs are normalized by DBIsolationManager.
        return f"environments/{self.environment_id}/branches/{self.branch_id}"

    @property
    def redis_state_namespace(self) -> str:
        return f"webagent:{self.environment_id}:{self.site}:{self.branch_id}"

    def to_dict(self) -> dict[str, str | int]:
        return {
            "agent_id": self.agent_id,
            "environment_id": self.environment_id,
            "site": self.site,
            "branch_id": self.branch_id,
            "generation": self.generation,
            "db_name": self.db_name,
            "db_engine": self.db_engine,
            "db_host": self.db_host,
            "db_port": self.db_port,
            "lifecycle_state": self.lifecycle_state,
            "inflight_requests": self.inflight_requests,
            "pending_background_jobs": self.pending_background_jobs,
            "pool_key": self.pool_key,
            "state_namespace": self.state_namespace,
            "redis_cache_prefix": f"cache:{{{self.state_namespace}}}:",
            "redis_state_prefix": f"state:{{{self.redis_state_namespace}}}:",
            "opensearch_index": self.search_index,
            "uploads_root": f"/var/opt/gitlab/web-agent-state/{self.state_path_token}/uploads",
            "artifacts_root": f"/var/opt/gitlab/web-agent-state/{self.state_path_token}/artifacts",
            "gitaly_relative_prefix": f".web-agent/{self.state_path_token}/gitaly",
        }


class SQLiteRouteRegistry:
    """Cross-process route registry with an atomic request-draining barrier."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_routes (
                    agent_id TEXT PRIMARY KEY,
                    db_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_routes)").fetchall()
            }
            additions = {
                "environment_id": "TEXT NOT NULL DEFAULT ''",
                "site": "TEXT NOT NULL DEFAULT ''",
                "branch_id": "TEXT NOT NULL DEFAULT 'root'",
                "generation": "INTEGER NOT NULL DEFAULT 1",
                "lifecycle_state": "TEXT NOT NULL DEFAULT 'ACTIVE'",
                "inflight_requests": "INTEGER NOT NULL DEFAULT 0",
                "pending_background_jobs": "INTEGER NOT NULL DEFAULT 0",
                "db_engine": "TEXT NOT NULL DEFAULT 'postgresql'",
                "db_host": "TEXT NOT NULL DEFAULT ''",
                "db_port": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, declaration in additions.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE agent_routes ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "UPDATE agent_routes SET environment_id = agent_id WHERE environment_id = ''"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> RouteRecord:
        return RouteRecord(
            agent_id=str(row["agent_id"]),
            environment_id=str(row["environment_id"]),
            site=str(row["site"]),
            branch_id=str(row["branch_id"]),
            generation=int(row["generation"]),
            db_name=str(row["db_name"]),
            db_engine=str(row["db_engine"]),
            db_host=str(row["db_host"]),
            db_port=int(row["db_port"]),
            lifecycle_state=str(row["lifecycle_state"]),
            inflight_requests=int(row["inflight_requests"]),
            pending_background_jobs=int(row["pending_background_jobs"]),
        )

    def set_route(
        self,
        agent_id: str,
        db_name: str,
        *,
        environment_id: str | None = None,
        site: str = "",
        branch_id: str = "root",
        generation: int = 1,
        lifecycle_state: str = ACTIVE,
        db_engine: str = "postgresql",
        db_host: str = "",
        db_port: int = 0,
    ) -> RouteRecord:
        """Publish a drained route. Existing in-flight routes cannot be replaced."""
        if lifecycle_state not in _VALID_STATES:
            raise ValueError(f"invalid lifecycle state: {lifecycle_state}")
        if generation <= 0:
            raise ValueError("route generation must be positive")
        environment_id = environment_id or agent_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT inflight_requests, pending_background_jobs
                FROM agent_routes WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if current is not None and (
                int(current["inflight_requests"]) != 0
                or int(current["pending_background_jobs"]) != 0
            ):
                raise RouteBusyError(f"agent route still has active requests: {agent_id}")
            connection.execute(
                """
                INSERT INTO agent_routes(
                    agent_id, environment_id, site, branch_id, generation,
                    db_name, db_engine, db_host, db_port,
                    lifecycle_state, inflight_requests,
                    pending_background_jobs, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(agent_id) DO UPDATE SET
                    environment_id = excluded.environment_id,
                    site = excluded.site,
                    branch_id = excluded.branch_id,
                    generation = excluded.generation,
                    db_name = excluded.db_name,
                    db_engine = excluded.db_engine,
                    db_host = excluded.db_host,
                    db_port = excluded.db_port,
                    lifecycle_state = excluded.lifecycle_state,
                    inflight_requests = 0,
                    pending_background_jobs = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    agent_id,
                    environment_id,
                    site,
                    branch_id,
                    generation,
                    db_name,
                    db_engine,
                    db_host,
                    db_port,
                    lifecycle_state,
                ),
            )
        return self.resolve_route(agent_id)

    def resolve(self, agent_id: str) -> str:
        return self.resolve_route(agent_id).db_name

    def resolve_route(self, agent_id: str) -> RouteRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_routes WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown agent route: {agent_id}")
        return self._record(row)

    def acquire(self, agent_id: str) -> RouteRecord:
        """Atomically admit one HTTP request only while the route is active."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_routes WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown agent route: {agent_id}")
            if str(row["lifecycle_state"]) != ACTIVE:
                raise RouteFrozenError(f"agent route is frozen: {agent_id}")
            connection.execute(
                """
                UPDATE agent_routes
                SET inflight_requests = inflight_requests + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
            updated = connection.execute(
                "SELECT * FROM agent_routes WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        assert updated is not None
        return self._record(updated)

    def release(self, agent_id: str) -> RouteRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT inflight_requests FROM agent_routes WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown agent route: {agent_id}")
            if int(row["inflight_requests"]) <= 0:
                raise RouteBusyError(f"route request counter underflow: {agent_id}")
            connection.execute(
                """
                UPDATE agent_routes
                SET inflight_requests = inflight_requests - 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
            updated = connection.execute(
                "SELECT * FROM agent_routes WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        assert updated is not None
        return self._record(updated)

    def enqueue_background_job(self, agent_id: str) -> RouteRecord:
        """Account for a job before Sidekiq publishes it to Redis.

        Enqueue remains legal while frozen because an already admitted HTTP
        request or a draining parent job may enqueue child work. New HTTP work
        is already blocked by acquire().
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lifecycle_state FROM agent_routes WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown agent route: {agent_id}")
            connection.execute(
                """
                UPDATE agent_routes
                SET pending_background_jobs = pending_background_jobs + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
        return self.resolve_route(agent_id)

    def complete_background_job(self, agent_id: str) -> RouteRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT pending_background_jobs FROM agent_routes WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown agent route: {agent_id}")
            if int(row["pending_background_jobs"]) <= 0:
                raise RouteBusyError(f"background job counter underflow: {agent_id}")
            connection.execute(
                """
                UPDATE agent_routes
                SET pending_background_jobs = pending_background_jobs - 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
        return self.resolve_route(agent_id)

    def freeze(self, agent_id: str) -> RouteRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_routes
                SET lifecycle_state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (FROZEN, agent_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown agent route: {agent_id}")
        return self.resolve_route(agent_id)

    def activate(
        self,
        agent_id: str,
        *,
        db_name: str | None = None,
        branch_id: str | None = None,
        generation: int | None = None,
        db_engine: str | None = None,
        db_host: str | None = None,
        db_port: int | None = None,
    ) -> RouteRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_routes WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown agent route: {agent_id}")
            if (
                int(row["inflight_requests"]) != 0
                or int(row["pending_background_jobs"]) != 0
            ):
                raise RouteBusyError(f"cannot activate a busy route: {agent_id}")
            next_generation = int(row["generation"]) if generation is None else generation
            if next_generation <= 0:
                raise ValueError("route generation must be positive")
            connection.execute(
                """
                UPDATE agent_routes
                SET db_name = ?, db_engine = ?, db_host = ?, db_port = ?,
                    branch_id = ?, generation = ?,
                    lifecycle_state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (
                    db_name or str(row["db_name"]),
                    db_engine or str(row["db_engine"]),
                    str(row["db_host"]) if db_host is None else db_host,
                    int(row["db_port"]) if db_port is None else db_port,
                    branch_id or str(row["branch_id"]),
                    next_generation,
                    ACTIVE,
                    agent_id,
                ),
            )
        return self.resolve_route(agent_id)

    def wait_for_drained(
        self,
        agent_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.05,
    ) -> RouteRecord:
        deadline = time.monotonic() + timeout_seconds
        while True:
            route = self.resolve_route(agent_id)
            if route.lifecycle_state != FROZEN:
                raise RuntimeError(f"route must be frozen before draining: {agent_id}")
            if route.inflight_requests == 0 and route.pending_background_jobs == 0:
                return route
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out draining {agent_id}; "
                    f"{route.inflight_requests} requests and "
                    f"{route.pending_background_jobs} background jobs remain"
                )
            time.sleep(poll_interval_seconds)

    def wait_for_background_jobs(
        self,
        agent_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.05,
    ) -> RouteRecord:
        """Wait for routed Sidekiq work without freezing evaluator HTTP traffic."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            route = self.resolve_route(agent_id)
            if route.pending_background_jobs == 0:
                return route
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for {agent_id}; "
                    f"{route.pending_background_jobs} background jobs remain"
                )
            time.sleep(poll_interval_seconds)

    def remove(self, agent_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT inflight_requests, pending_background_jobs
                FROM agent_routes WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is not None and (
                int(row["inflight_requests"]) != 0
                or int(row["pending_background_jobs"]) != 0
            ):
                raise RouteBusyError(f"cannot remove a busy route: {agent_id}")
            connection.execute("DELETE FROM agent_routes WHERE agent_id = ?", (agent_id,))

    def list_routes(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT agent_id, db_name FROM agent_routes").fetchall()
        return {str(row["agent_id"]): str(row["db_name"]) for row in rows}

    def list_route_records(self) -> dict[str, RouteRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM agent_routes").fetchall()
        return {str(row["agent_id"]): self._record(row) for row in rows}
