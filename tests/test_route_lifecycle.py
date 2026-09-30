from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from omegaconf import OmegaConf

from rl_web_agent.entrypoints.route_registry_server import make_handler
from rl_web_agent.isolation.db_isolation import DBIsolationManager
from rl_web_agent.isolation.db_routing import (
    ASGITrustedDBRouterMiddleware,
    TrustedDBRouter,
)
from rl_web_agent.isolation.route_registry import (
    ACTIVE,
    FROZEN,
    RouteFrozenError,
    SQLiteRouteRegistry,
)
from rl_web_agent.isolation.route_token import RouteTokenSigner


class RouteLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.registry_path = str(
            Path(self.temporary_directory.name) / "routes.sqlite3"
        )
        self.registry = SQLiteRouteRegistry(self.registry_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_route_lifecycle_and_generation(self) -> None:
        route = self.registry.set_route(
            "agent_a",
            "agent_a_db",
            environment_id="environment_a",
            site="gitlab",
        )
        self.assertEqual(route.lifecycle_state, ACTIVE)
        self.assertEqual(route.generation, 1)
        self.assertEqual(route.inflight_requests, 0)
        self.assertEqual(route.pending_background_jobs, 0)
        self.assertEqual(
            route.to_dict()["redis_cache_prefix"],
            "cache:{webagent:environment_a:gitlab:root:g1}:",
        )
        self.assertEqual(
            route.to_dict()["opensearch_index"],
            "webagent_environment_a_gitlab_root",
        )
        self.assertEqual(
            route.to_dict()["uploads_root"],
            "/var/opt/gitlab/web-agent-state/environments/environment_a/branches/root/uploads",
        )
        self.assertEqual(
            route.to_dict()["artifacts_root"],
            "/var/opt/gitlab/web-agent-state/environments/environment_a/branches/root/artifacts",
        )

        acquired = self.registry.acquire("agent_a")
        self.assertEqual(acquired.inflight_requests, 1)
        self.registry.enqueue_background_job("agent_a")
        frozen = self.registry.freeze("agent_a")
        self.assertEqual(frozen.lifecycle_state, FROZEN)
        with self.assertRaises(RouteFrozenError):
            self.registry.acquire("agent_a")
        with self.assertRaises(TimeoutError):
            self.registry.wait_for_drained(
                "agent_a", timeout_seconds=0.01, poll_interval_seconds=0.001
            )

        self.registry.release("agent_a")
        with self.assertRaises(TimeoutError):
            self.registry.wait_for_drained(
                "agent_a", timeout_seconds=0.01, poll_interval_seconds=0.001
            )
        self.registry.complete_background_job("agent_a")
        idle = self.registry.wait_for_background_jobs(
            "agent_a", timeout_seconds=0.1
        )
        self.assertEqual(idle.pending_background_jobs, 0)
        drained = self.registry.wait_for_drained("agent_a", timeout_seconds=0.1)
        self.assertEqual(drained.inflight_requests, 0)
        self.assertEqual(drained.pending_background_jobs, 0)
        activated = self.registry.activate(
            "agent_a",
            db_name="agent_a_branch_db",
            branch_id="left",
            generation=2,
        )
        self.assertEqual(activated.lifecycle_state, ACTIVE)
        self.assertEqual(activated.db_name, "agent_a_branch_db")
        self.assertEqual(activated.branch_id, "left")
        self.assertEqual(activated.generation, 2)
        self.assertIn("environment_a:gitlab:left:2", activated.pool_key)

    def test_existing_v1_schema_is_migrated(self) -> None:
        legacy_path = str(Path(self.temporary_directory.name) / "legacy.sqlite3")
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE agent_routes (
                    agent_id TEXT PRIMARY KEY,
                    db_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "INSERT INTO agent_routes(agent_id, db_name) VALUES (?, ?)",
                ("legacy_agent", "legacy_db"),
            )

        migrated = SQLiteRouteRegistry(legacy_path).resolve_route("legacy_agent")
        self.assertEqual(migrated.environment_id, "legacy_agent")
        self.assertEqual(migrated.branch_id, "root")
        self.assertEqual(migrated.generation, 1)
        self.assertEqual(migrated.lifecycle_state, ACTIVE)


class TrustedRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_releases_lease_when_application_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SQLiteRouteRegistry(str(Path(directory) / "routes.sqlite3"))
            registry.set_route("agent_a", "agent_a_db", site="gitlab")
            signer = RouteTokenSigner("test-secret-that-is-longer-than-32-bytes")
            router = TrustedDBRouter(registry, signer)

            async def failing_application(scope, receive, send):
                self.assertEqual(scope["web_agent_db"], "agent_a_db")
                self.assertEqual(
                    scope["web_agent_environment"]["branch_id"], "root"
                )
                raise RuntimeError("application failed")

            middleware = ASGITrustedDBRouterMiddleware(failing_application, router)
            scope = {
                "type": "http",
                "headers": [
                    (b"x-agent-route", signer.issue("agent_a").encode()),
                    (b"x-agent-db", b"spoofed_db"),
                ],
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                return None

            with self.assertRaisesRegex(RuntimeError, "application failed"):
                await middleware(scope, receive, send)
            self.assertEqual(registry.resolve_route("agent_a").inflight_requests, 0)


class RouteRegistryHTTPTest(unittest.TestCase):
    def test_http_acquire_release_and_frozen_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SQLiteRouteRegistry(str(Path(directory) / "routes.sqlite3"))
            registry.set_route("agent_a", "agent_a_db", site="gitlab")
            secret = "registry-secret-that-is-longer-than-32-bytes"
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(registry, secret)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}/v1/routes/agent_a"

            def post(action: str) -> dict:
                request = urllib.request.Request(
                    f"{base_url}/{action}",
                    method="POST",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    return json.loads(response.read())

            try:
                list_request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/routes",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                with urllib.request.urlopen(list_request, timeout=2) as response:
                    listed = json.loads(response.read())["routes"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["agent_id"], "agent_a")
                self.assertEqual(
                    listed[0]["pool_key"], registry.resolve_route("agent_a").pool_key
                )
                self.assertEqual(post("acquire")["inflight_requests"], 1)
                self.assertEqual(post("release")["inflight_requests"], 0)
                self.assertEqual(post("enqueue-job")["pending_background_jobs"], 1)
                self.assertEqual(post("complete-job")["pending_background_jobs"], 0)
                registry.freeze("agent_a")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    post("acquire")
                self.assertEqual(raised.exception.code, 423)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class FakeDBIsolationManager(DBIsolationManager):
    def __init__(self, registry_path: str) -> None:
        config = OmegaConf.create(
            {
                "admin_dsn": "postgresql://unused",
                "base_template_db": "base_template",
                "clone_strategy": "FILE_COPY",
                "route_token_secret": "route-secret-that-is-longer-than-32-bytes",
                "route_registry_path": registry_path,
                "drop_on_close": True,
                "reset_on_setup": True,
                "route_drain_timeout_seconds": 1,
                "rails_pool_release_enabled": False,
                "shared_site_hosts": {"gitlab": "127.0.0.1:8023"},
            }
        )
        self.operations: list[tuple[str, str, str | None]] = []
        super().__init__(config, Mock())

    def drop_db(self, db_name: str) -> None:
        self.operations.append(("drop", db_name, None))

    def clone_db(self, source_db: str, target_db: str) -> None:
        self.operations.append(("clone", source_db, target_db))


class RecordingStateCoordinator:
    def __init__(self, fail_phase: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_phase = fail_phase

    def _record(self, phase: str) -> None:
        self.events.append(phase)
        if phase == self.fail_phase:
            raise RuntimeError(f"state {phase} failed")

    def prepare(self, identity) -> None:
        self._record("prepare")

    def quiesce(self, identity) -> None:
        self._record("quiesce")

    def checkpoint(self, identity, checkpoint_id: str) -> None:
        self._record("checkpoint")

    def fork(self, source, checkpoint_id: str, target) -> None:
        self._record("fork")

    def reset(self, source, target) -> None:
        self._record("reset")

    def activate(self, identity) -> None:
        self._record("activate")

    def cleanup(self, identity) -> None:
        self._record("cleanup")


class DBIsolationLifecycleTest(unittest.TestCase):
    def test_checkpoint_fork_and_reset_publish_new_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeDBIsolationManager(str(Path(directory) / "routes.sqlite3"))
            session = manager.prepare_for_task(
                "environment_a", {"task_id": 418, "sites": ["gitlab"]}
            )
            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual((route.branch_id, route.generation), ("root", 1))

            checkpoint = manager.checkpoint(session, 3)
            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual((route.branch_id, route.generation), ("root", 2))

            branch_db = manager.fork(session, checkpoint, "left")
            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual(route.db_name, branch_db)
            self.assertEqual((route.branch_id, route.generation), ("left", 3))

            manager.reset(session)
            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual(route.db_name, session.db_name)
            self.assertEqual((route.branch_id, route.generation), ("root", 4))
            self.assertEqual(route.lifecycle_state, ACTIVE)

    def test_non_db_failure_keeps_route_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeDBIsolationManager(str(Path(directory) / "routes.sqlite3"))
            session = manager.prepare_for_task(
                "environment_a", {"task_id": 418, "sites": ["gitlab"]}
            )
            coordinator = RecordingStateCoordinator(fail_phase="reset")
            manager.non_db_state = coordinator

            with self.assertRaisesRegex(RuntimeError, "state reset failed"):
                manager.reset(session)

            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual(route.lifecycle_state, FROZEN)
            self.assertEqual(coordinator.events, ["quiesce", "reset"])


if __name__ == "__main__":
    unittest.main()
