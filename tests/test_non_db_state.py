from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omegaconf import OmegaConf

from rl_web_agent.isolation.non_db_state import (
    NonDBStateCoordinator,
    OverlayFilesystemBackend,
    StateContextPublisher,
    StateIdentity,
)


class StateIdentityTest(unittest.TestCase):
    def test_cache_generation_and_branch_stable_state_namespaces(self) -> None:
        identity = StateIdentity("agent_42", "gitlab", "left", 7, "agent_42_db")
        self.assertEqual(
            identity.namespace,
            "webagent:agent_42:gitlab:left:g7",
        )
        environment = identity.command_environment()
        self.assertEqual(
            environment["WEB_AGENT_REDIS_CACHE_PREFIX"],
            "cache:{webagent:agent_42:gitlab:left:g7}:",
        )
        self.assertEqual(
            environment["WEB_AGENT_SEARCH_INDEX"],
            "webagent_agent_42_gitlab_left",
        )
        self.assertEqual(environment["WEB_AGENT_DATABASE_NAME"], "agent_42_db")
        self.assertEqual(
            environment["WEB_AGENT_REDIS_STATE_PREFIX"],
            "state:{webagent:agent_42:gitlab:left}:",
        )


class StateContextPublisherTest(unittest.TestCase):
    def test_publish_atomically_replaces_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = StateContextPublisher(directory)
            first = StateIdentity("agent_42", "gitlab", "root", 1)
            second = StateIdentity("agent_42", "gitlab", "root", 2)
            publisher.publish(first, {"custom": "value"})
            publisher.publish(second, {})
            payload = json.loads(publisher.path_for(second).read_text())
            self.assertEqual(payload["generation"], 2)
            self.assertNotIn("custom", payload)


class OverlayFilesystemBackendTest(unittest.TestCase):
    def test_checkpoint_fork_reset_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lower = root / "lower"
            lower.mkdir()
            (lower / "base.txt").write_text("base")
            backend = OverlayFilesystemBackend(
                root / "runtime",
                {"uploads": lower},
                mount_enabled=False,
                worker_mode="dedicated",
            )
            parent = StateIdentity("agent_42", "gitlab", "root", 1)
            child = StateIdentity("agent_42", "gitlab", "left", 2)

            backend.prepare(parent)
            parent_upper = Path(
                backend.describe(parent)["filesystem"]["uploads"]["merged"]
            ).parent / "upper"
            (parent_upper / "upload.txt").write_text("before checkpoint")
            backend.checkpoint(parent, "checkpoint_1")
            (parent_upper / "upload.txt").write_text("after checkpoint")
            backend.fork(parent, "checkpoint_1", child)

            child_upper = Path(
                backend.describe(child)["filesystem"]["uploads"]["merged"]
            ).parent / "upper"
            self.assertEqual(
                (child_upper / "upload.txt").read_text(),
                "before checkpoint",
            )

            reset = StateIdentity("agent_42", "gitlab", "root", 3)
            backend.reset(child, reset)
            reset_upper = Path(
                backend.describe(reset)["filesystem"]["uploads"]["merged"]
            ).parent / "upper"
            self.assertEqual(list(reset_upper.iterdir()), [])
            backend.cleanup(reset)
            self.assertFalse(backend._environment_root(reset).exists())

    def test_overlay_rejects_shared_worker_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lower = Path(directory) / "lower"
            lower.mkdir()
            with self.assertRaisesRegex(ValueError, "dedicated"):
                OverlayFilesystemBackend(
                    Path(directory) / "runtime",
                    {"uploads": lower},
                    mount_enabled=False,
                    worker_mode="shared",
                )


class NonDBStateCoordinatorTest(unittest.TestCase):
    def test_disabled_configuration_is_noop(self) -> None:
        coordinator = NonDBStateCoordinator.from_config(None)
        identity = StateIdentity("agent", "gitlab", "root", 1)
        coordinator.prepare(identity)
        coordinator.quiesce(identity)
        coordinator.checkpoint(identity, "checkpoint")
        coordinator.reset(identity, identity)
        coordinator.cleanup(identity)

    def test_string_false_from_environment_is_disabled(self) -> None:
        coordinator = NonDBStateCoordinator.from_config(
            OmegaConf.create(
                {
                    "enabled": "false",
                    "gitlab_state": {"enabled": "false"},
                }
            )
        )

        self.assertEqual(coordinator.backends, [])
        self.assertIsNone(coordinator.publisher)


if __name__ == "__main__":
    unittest.main()
