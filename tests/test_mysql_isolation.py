from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omegaconf import OmegaConf

from rl_web_agent.isolation.db_isolation import DBIsolationManager
from rl_web_agent.isolation.db_routing import RoutedMySQLConnector, TrustedDBRouter
from rl_web_agent.isolation.mysql_xfs_isolation import (
    MySQLBranchResource,
    MySQLXFSReflinkManager,
)
from rl_web_agent.isolation.route_registry import SQLiteRouteRegistry
from rl_web_agent.isolation.route_token import RouteTokenSigner


class FakeMySQLManager:
    def __init__(self) -> None:
        self.resource = MySQLBranchResource(
            environment_id="environment",
            branch_id="root",
            datadir="/tmp/mysql/environment/root",
            host="127.0.0.1",
            port=13306,
            database_name="magentodb",
            process_id=100,
        )
        self.events: list[tuple[str, str]] = []

    def prepare(self, environment_id: str, branch_id: str = "root"):
        self.events.append(("prepare", branch_id))
        return self.resource

    def checkpoint(self, resource, checkpoint_id: str):
        self.events.append(("checkpoint", checkpoint_id))
        return resource

    def fork(self, resource, checkpoint_id: str, target_branch: str):
        self.events.append(("fork", target_branch))
        return MySQLBranchResource(
            environment_id=resource.environment_id,
            branch_id=target_branch,
            datadir=f"/tmp/mysql/environment/{target_branch}",
            host=resource.host,
            port=13307,
            database_name=resource.database_name,
            process_id=101,
        )

    def reset(self, resource):
        self.events.append(("reset", "root"))
        return self.resource

    def cleanup(self, resource) -> None:
        self.events.append(("cleanup", resource.branch_id))


class MySQLLifecycleTest(unittest.TestCase):
    def test_each_branch_uses_a_private_mysql_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "environment" / "branches" / "root"
            datadir = root / "mysql"
            datadir.mkdir(parents=True)
            resource = MySQLBranchResource(
                environment_id="environment",
                branch_id="root",
                datadir=str(datadir),
                host="127.0.0.1",
                port=13306,
                database_name="magentodb",
            )
            manager = MySQLXFSReflinkManager.__new__(MySQLXFSReflinkManager)
            manager.bind_host = "127.0.0.1"

            config = manager._write_config(resource)

            self.assertTrue((root / "tmp").is_dir())
            self.assertIn(f"tmpdir={root / 'tmp'}", config.read_text())

    def test_shopping_never_falls_back_to_postgresql_when_mysql_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = OmegaConf.create(
                {
                    "admin_dsn": "postgresql://unused",
                    "route_token_secret": "route-secret-that-is-longer-than-32-bytes",
                    "route_registry_path": str(Path(directory) / "routes.sqlite3"),
                    "non_db_state": {"enabled": False},
                    "shared_site_hosts": {"shopping": "127.0.0.1:7772"},
                    "mysql_xfs": {"enabled": False, "sites": ["shopping"]},
                }
            )
            manager = DBIsolationManager(config, Mock())
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                manager.prepare_for_task(
                    "environment", {"task_id": 1, "sites": ["shopping"]}
                )

    def test_sites_use_distinct_mysql_templates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = OmegaConf.create(
                {
                    "admin_dsn": "",
                    "route_token_secret": "route-secret-that-is-longer-than-32-bytes",
                    "route_registry_path": str(Path(directory) / "routes.sqlite3"),
                    "non_db_state": {"enabled": False},
                    "shared_site_hosts": {},
                    "mysql_xfs": {
                        "enabled": True,
                        "sites": ["shopping", "shopping_admin"],
                        "templates": {
                            "shopping": "storefront",
                            "shopping_admin": "admin",
                        },
                    },
                }
            )
            with patch(
                "rl_web_agent.isolation.db_isolation.MySQLXFSReflinkManager"
            ) as constructor:
                constructor.side_effect = [Mock(), Mock()]
                manager = DBIsolationManager(config, Mock())
                storefront = manager._ensure_mysql_manager("shopping")
                admin = manager._ensure_mysql_manager("shopping_admin")

            self.assertIsNot(storefront, admin)
            self.assertEqual(
                constructor.call_args_list[0].args[0].template_name, "storefront"
            )
            self.assertEqual(
                constructor.call_args_list[1].args[0].template_name, "admin"
            )

    def test_mysql_branch_lifecycle_updates_trusted_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeMySQLManager()
            config = OmegaConf.create(
                {
                    "admin_dsn": "",
                    "route_token_secret": "route-secret-that-is-longer-than-32-bytes",
                    "route_registry_path": str(Path(directory) / "routes.sqlite3"),
                    "rails_pool_release_enabled": False,
                    "non_db_state": {"enabled": False},
                    "shared_site_hosts": {"shopping": "127.0.0.1:7770"},
                    "mysql_xfs": {"enabled": True, "sites": ["shopping"]},
                }
            )
            with patch(
                "rl_web_agent.isolation.db_isolation.MySQLXFSReflinkManager",
                return_value=fake,
            ):
                manager = DBIsolationManager(config, Mock())
                session = manager.prepare_for_task(
                    "environment",
                    {"task_id": 1, "sites": ["shopping"]},
                )

                route = manager.route_registry.resolve_route(session.agent_id)
                self.assertEqual(route.db_engine, "mysql")
                self.assertEqual(route.db_host, "127.0.0.1")
                self.assertEqual(route.db_port, 13306)

                checkpoint = manager.checkpoint(session, 1)
                self.assertIn("_ckpt_1", checkpoint)
                self.assertEqual(
                    manager.route_registry.resolve_route(session.agent_id).generation,
                    2,
                )

                manager.fork(session, checkpoint, "left")
                route = manager.route_registry.resolve_route(session.agent_id)
                self.assertEqual(route.branch_id, "left")
                self.assertEqual(route.generation, 3)
                self.assertEqual(route.db_port, 13307)

                manager.reset(session)
                route = manager.route_registry.resolve_route(session.agent_id)
                self.assertEqual(route.branch_id, "root")
                self.assertEqual(route.generation, 4)

                manager.cleanup(session)
                with self.assertRaises(KeyError):
                    manager.route_registry.resolve_route(session.agent_id)

            self.assertEqual(
                [event[0] for event in fake.events],
                ["prepare", "checkpoint", "fork", "reset", "cleanup"],
            )

    def test_drain_timeout_still_removes_mysql_process_and_freezes_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeMySQLManager()
            config = OmegaConf.create(
                {
                    "admin_dsn": "",
                    "route_token_secret": "route-secret-that-is-longer-than-32-bytes",
                    "route_registry_path": str(Path(directory) / "routes.sqlite3"),
                    "route_drain_timeout_seconds": 0.01,
                    "rails_pool_release_enabled": False,
                    "non_db_state": {"enabled": False},
                    "shared_site_hosts": {"shopping": "127.0.0.1:7770"},
                    "mysql_xfs": {"enabled": True, "sites": ["shopping"]},
                }
            )
            with patch(
                "rl_web_agent.isolation.db_isolation.MySQLXFSReflinkManager",
                return_value=fake,
            ):
                manager = DBIsolationManager(config, Mock())
                session = manager.prepare_for_task(
                    "environment", {"task_id": 1, "sites": ["shopping"]}
                )
                manager.route_registry.acquire(session.agent_id)

                with self.assertRaises(TimeoutError):
                    manager.cleanup(session)

            self.assertIn(("cleanup", "root"), fake.events)
            route = manager.route_registry.resolve_route(session.agent_id)
            self.assertEqual(route.lifecycle_state, "FROZEN")


class RoutedMySQLConnectorTest(unittest.TestCase):
    def test_route_owns_endpoint_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SQLiteRouteRegistry(str(Path(directory) / "routes.sqlite3"))
            registry.set_route(
                "agent",
                "magentodb",
                environment_id="environment",
                site="shopping",
                db_engine="mysql",
                db_host="127.0.0.1",
                db_port=13306,
            )
            signer = RouteTokenSigner("route-secret-that-is-longer-than-32-bytes")
            router = TrustedDBRouter(registry, signer)
            connector = RoutedMySQLConnector(user="magento", password="secret")

            with router.request_scope({"X-Agent-Route": signer.issue("agent")}):
                parameters = connector._route_kwargs(
                    {"host": "attacker", "port": 1, "database": "other"}
                )

            self.assertEqual(parameters["host"], "127.0.0.1")
            self.assertEqual(parameters["port"], 13306)
            self.assertEqual(parameters["database"], "magentodb")
            self.assertEqual(parameters["user"], "magento")


if __name__ == "__main__":
    unittest.main()
