from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from omegaconf import DictConfig, OmegaConf


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    token = re.sub(r"_+", "_", token).strip("_.")
    return token or "default"


@dataclass(frozen=True)
class StateIdentity:
    environment_id: str
    site: str
    branch_id: str
    generation: int
    database_name: str = ""

    @classmethod
    def from_route(cls, route: Any) -> "StateIdentity":
        return cls(
            environment_id=str(route.environment_id),
            site=str(route.site),
            branch_id=str(route.branch_id),
            generation=int(route.generation),
            database_name=str(route.db_name),
        )

    @property
    def namespace(self) -> str:
        return ":".join(
            (
                "webagent",
                _safe_token(self.environment_id),
                _safe_token(self.site),
                _safe_token(self.branch_id),
                f"g{self.generation}",
            )
        )

    def command_environment(self, prefix: str = "WEB_AGENT") -> dict[str, str]:
        return {
            f"{prefix}_ENVIRONMENT_ID": self.environment_id,
            f"{prefix}_SITE": self.site,
            f"{prefix}_BRANCH_ID": self.branch_id,
            f"{prefix}_GENERATION": str(self.generation),
            f"{prefix}_DATABASE_NAME": self.database_name,
            f"{prefix}_STATE_NAMESPACE": self.namespace,
            f"{prefix}_REDIS_CACHE_PREFIX": self.redis_cache_prefix,
            f"{prefix}_REDIS_STATE_PREFIX": self.redis_state_prefix,
            f"{prefix}_SEARCH_INDEX": self.search_index,
        }

    @property
    def search_index(self) -> str:
        # A search index belongs to the logical branch, not to a cache
        # generation.  Checkpointing an unchanged branch must not force an
        # expensive Magento full reindex; fork/reset hooks rebuild the target
        # branch before it is admitted.
        raw = "_".join(
            (
                "webagent",
                self.environment_id,
                self.site,
                self.branch_id,
            )
        ).lower()
        return _safe_token(raw).replace(".", "_")

    @property
    def redis_cache_prefix(self) -> str:
        return f"cache:{{{self.namespace}}}:"

    @property
    def redis_state_prefix(self) -> str:
        stable_namespace = ":".join(
            (
                "webagent",
                _safe_token(self.environment_id),
                _safe_token(self.site),
                _safe_token(self.branch_id),
            )
        )
        return f"state:{{{stable_namespace}}}:"


class StateBackend(Protocol):
    def prepare(self, identity: StateIdentity) -> None: ...

    def quiesce(self, identity: StateIdentity) -> None: ...

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None: ...

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None: ...

    def reset(self, source: StateIdentity, target: StateIdentity) -> None: ...

    def activate(self, identity: StateIdentity) -> None: ...

    def cleanup(self, identity: StateIdentity) -> None: ...

    def describe(self, identity: StateIdentity) -> dict[str, Any]: ...


class CommandHookBackend:
    """Runs app-specific queue/search hooks without invoking a shell."""

    _PHASES = {
        "prepare",
        "quiesce",
        "checkpoint",
        "fork",
        "reset",
        "activate",
        "cleanup",
    }

    def __init__(self, name: str, commands: dict[str, list[list[str]]], timeout: float) -> None:
        self.name = name
        self.commands = commands
        self.timeout = timeout
        unknown = set(commands).difference(self._PHASES)
        if unknown:
            raise ValueError(f"unknown {name} hook phases: {sorted(unknown)}")

    @classmethod
    def from_config(cls, config: DictConfig) -> "CommandHookBackend":
        raw_commands = OmegaConf.to_container(config.get("commands", {}), resolve=True)
        if not isinstance(raw_commands, dict):
            raise ValueError("state hook commands must be a phase -> argv-list mapping")
        commands: dict[str, list[list[str]]] = {}
        for phase, values in raw_commands.items():
            if not isinstance(values, list):
                raise ValueError(f"state hook phase {phase!r} must contain argv lists")
            normalized: list[list[str]] = []
            for argv in values:
                if not isinstance(argv, list) or not argv:
                    raise ValueError(f"state hook {phase!r} contains an invalid argv")
                normalized.append([str(value) for value in argv])
            commands[str(phase)] = normalized
        return cls(
            name=str(config.get("name", "command_hook")),
            commands=commands,
            timeout=float(config.get("timeout_seconds", 120)),
        )

    def _run(
        self,
        phase: str,
        identity: StateIdentity,
        *,
        checkpoint_id: str = "",
        source: StateIdentity | None = None,
    ) -> None:
        environment = os.environ.copy()
        environment.update(identity.command_environment())
        environment["WEB_AGENT_STATE_PHASE"] = phase
        environment["WEB_AGENT_CHECKPOINT_ID"] = checkpoint_id
        if source is not None:
            environment.update(source.command_environment("WEB_AGENT_SOURCE"))
        for argv in self.commands.get(phase, []):
            subprocess.run(
                argv,
                check=True,
                timeout=self.timeout,
                env=environment,
            )

    def prepare(self, identity: StateIdentity) -> None:
        self._run("prepare", identity)

    def quiesce(self, identity: StateIdentity) -> None:
        self._run("quiesce", identity)

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None:
        self._run("checkpoint", identity, checkpoint_id=checkpoint_id)

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None:
        self._run("fork", target, checkpoint_id=checkpoint_id, source=source)

    def reset(self, source: StateIdentity, target: StateIdentity) -> None:
        self._run("reset", target, source=source)

    def activate(self, identity: StateIdentity) -> None:
        self._run("activate", identity)

    def cleanup(self, identity: StateIdentity) -> None:
        self._run("cleanup", identity)

    def describe(self, identity: StateIdentity) -> dict[str, Any]:
        return {self.name: {"hooked_phases": sorted(self.commands)}}


class OverlayFilesystemBackend:
    """OverlayFS lifecycle for dedicated-worker compatibility mode.

    The merged directory must be mounted into a worker or Gitaly process that
    serves exactly one environment/branch. It is never switched per request in
    a shared process.
    """

    def __init__(
        self,
        runtime_root: str | Path,
        components: dict[str, str | Path],
        *,
        mount_enabled: bool,
        worker_mode: str,
    ) -> None:
        if worker_mode != "dedicated":
            raise ValueError(
                "OverlayFS requires non_db_state.worker_mode=dedicated; "
                "a shared worker cannot switch mount namespaces per request"
            )
        self.runtime_root = Path(runtime_root).resolve()
        if self.runtime_root == Path(self.runtime_root.anchor):
            raise ValueError("overlay runtime_root cannot be a filesystem root")
        self.components = {
            _safe_token(name): Path(lower).resolve()
            for name, lower in components.items()
        }
        if not self.components:
            raise ValueError("overlay backend requires at least one component")
        self.mount_enabled = mount_enabled
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: DictConfig, worker_mode: str) -> "OverlayFilesystemBackend":
        raw_components = OmegaConf.to_container(config.get("components", {}), resolve=True)
        if not isinstance(raw_components, dict):
            raise ValueError("overlay components must map component name to lower directory")
        return cls(
            runtime_root=str(config.get("runtime_root", "./runtime/non_db_overlay")),
            components={str(name): str(path) for name, path in raw_components.items()},
            mount_enabled=bool(config.get("mount_enabled", False)),
            worker_mode=worker_mode,
        )

    def _environment_root(self, identity: StateIdentity) -> Path:
        return self.runtime_root / "environments" / _safe_token(identity.environment_id)

    def _branch_root(self, identity: StateIdentity) -> Path:
        return self._environment_root(identity) / "branches" / _safe_token(identity.branch_id)

    def _checkpoint_root(self, identity: StateIdentity, checkpoint_id: str) -> Path:
        return self._environment_root(identity) / "checkpoints" / _safe_token(checkpoint_id)

    def _component_paths(self, root: Path, component: str) -> tuple[Path, Path, Path]:
        component_root = root / component
        return (
            component_root / "upper",
            component_root / "work",
            component_root / "merged",
        )

    def _create_branch(self, identity: StateIdentity) -> None:
        root = self._branch_root(identity)
        for component, lower in self.components.items():
            if not lower.is_dir():
                raise FileNotFoundError(f"overlay lower directory does not exist: {lower}")
            upper, work, merged = self._component_paths(root, component)
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            merged.mkdir(parents=True, exist_ok=True)
            if self.mount_enabled and not os.path.ismount(merged):
                subprocess.run(
                    [
                        "mount",
                        "-t",
                        "overlay",
                        "overlay",
                        "-o",
                        f"lowerdir={lower},upperdir={upper},workdir={work}",
                        str(merged),
                    ],
                    check=True,
                )

    def _unmount_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for merged in sorted(root.glob("*/merged"), reverse=True):
            if os.path.ismount(merged):
                subprocess.run(["umount", str(merged)], check=True)

    def _remove_tree(self, target: Path) -> None:
        resolved = target.resolve()
        if self.runtime_root not in resolved.parents:
            raise ValueError(f"refusing to remove path outside overlay runtime: {resolved}")
        self._unmount_tree(resolved)
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise PermissionError(
                f"failed to remove overlay runtime path: {resolved}"
            )

    def _clone_upper(self, source_root: Path, target_root: Path) -> None:
        self._remove_tree(target_root)
        for component in self.components:
            source_upper, _, _ = self._component_paths(source_root, component)
            target_upper, target_work, target_merged = self._component_paths(
                target_root, component
            )
            target_upper.parent.mkdir(parents=True, exist_ok=True)
            target_work.mkdir(parents=True, exist_ok=True)
            target_merged.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["cp", "-a", "--reflink=auto", f"{source_upper}/.", str(target_upper)],
                check=True,
            )

    def prepare(self, identity: StateIdentity) -> None:
        self._create_branch(identity)

    def quiesce(self, identity: StateIdentity) -> None:
        return None

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None:
        self._clone_upper(
            self._branch_root(identity),
            self._checkpoint_root(identity, checkpoint_id),
        )

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None:
        checkpoint_root = self._checkpoint_root(source, checkpoint_id)
        if not checkpoint_root.exists():
            raise FileNotFoundError(f"unknown filesystem checkpoint: {checkpoint_id}")
        self._clone_upper(checkpoint_root, self._branch_root(target))
        self._create_branch(target)

    def reset(self, source: StateIdentity, target: StateIdentity) -> None:
        environment_root = self._environment_root(source)
        self._remove_tree(environment_root)
        self._create_branch(target)

    def activate(self, identity: StateIdentity) -> None:
        self._create_branch(identity)

    def cleanup(self, identity: StateIdentity) -> None:
        self._remove_tree(self._environment_root(identity))

    def describe(self, identity: StateIdentity) -> dict[str, Any]:
        root = self._branch_root(identity)
        return {
            "filesystem": {
                component: {
                    "lower": str(lower),
                    "merged": str(self._component_paths(root, component)[2]),
                }
                for component, lower in self.components.items()
            }
        }


class GitLabReflinkStateBackend:
    """Version-specific local-file/Gitaly snapshots inside an Omnibus container."""

    def __init__(self, container: str, statectl_path: str, timeout: float) -> None:
        self.container = container
        self.statectl_path = statectl_path
        self.timeout = timeout
        if not self.container:
            raise ValueError("GitLab state backend requires a Docker container name")

    @classmethod
    def from_config(cls, config: DictConfig) -> "GitLabReflinkStateBackend":
        return cls(
            container=str(config.get("docker_container", "")),
            statectl_path=str(
                config.get("statectl_path", "/opt/web-agent/gitlab_statectl.rb")
            ),
            timeout=float(config.get("timeout_seconds", 300)),
        )

    def _run(self, operation: str, *values: str) -> None:
        subprocess.run(
            [
                "docker",
                "exec",
                self.container,
                "/opt/gitlab/embedded/bin/ruby",
                self.statectl_path,
                operation,
                *values,
            ],
            check=True,
            timeout=self.timeout,
        )

    def prepare(self, identity: StateIdentity) -> None:
        self._run(
            "prepare", identity.environment_id, identity.site, identity.branch_id
        )

    def quiesce(self, identity: StateIdentity) -> None:
        return None

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None:
        self._run(
            "checkpoint",
            identity.environment_id,
            identity.site,
            identity.branch_id,
            checkpoint_id,
        )

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None:
        self._run(
            "fork",
            source.environment_id,
            source.site,
            source.branch_id,
            checkpoint_id,
            target.branch_id,
        )

    def reset(self, source: StateIdentity, target: StateIdentity) -> None:
        self._run(
            "reset", source.environment_id, source.site, target.branch_id
        )

    def activate(self, identity: StateIdentity) -> None:
        return None

    def cleanup(self, identity: StateIdentity) -> None:
        self._run("cleanup", identity.environment_id, identity.site)

    def describe(self, identity: StateIdentity) -> dict[str, Any]:
        environment = _safe_token(identity.environment_id)
        branch = _safe_token(identity.branch_id)
        state_root = f"/var/opt/gitlab/web-agent-state/environments/{environment}/branches/{branch}"
        return {
            "filesystem": {
                "uploads": {"root": f"{state_root}/uploads"},
                "artifacts": {"root": f"{state_root}/artifacts"},
                "gitaly": {
                    "relative_prefix": (
                        f".web-agent/environments/{environment}/branches/{branch}/gitaly"
                    )
                },
            }
        }


class MagentoReflinkStateBackend:
    """CoW lifecycle for Magento media, sessions, and writable ``var`` data.

    The host runtime root is mounted into the shared Magento container at
    ``container_root``.  The PHP route adapter derives the same branch path
    from the trusted route record, so no browser-controlled path is accepted.
    """

    _COMPONENTS = ("media", "sessions")

    def __init__(
        self,
        template_root: str | Path,
        runtime_root: str | Path,
        container_root: str | Path,
        sites: Iterable[str] = ("shopping", "shopping_admin"),
        template_roots: dict[str, str | Path] | None = None,
    ) -> None:
        self.template_root = Path(template_root).expanduser().resolve()
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.container_root = Path(container_root)
        self.sites = {str(site) for site in sites}
        self.template_roots = {
            str(site): Path(path).expanduser().resolve()
            for site, path in (template_roots or {}).items()
        }
        if self.runtime_root == Path(self.runtime_root.anchor):
            raise ValueError("Magento runtime_root cannot be a filesystem root")
        if not self.container_root.is_absolute():
            raise ValueError("Magento container_root must be absolute")

    @classmethod
    def from_config(cls, config: DictConfig) -> "MagentoReflinkStateBackend":
        template_roots_config = config.get("template_roots", {})
        raw_template_roots = (
            OmegaConf.to_container(template_roots_config, resolve=True)
            if OmegaConf.is_config(template_roots_config)
            else template_roots_config
        )
        if not isinstance(raw_template_roots, dict):
            raise ValueError("magento_state.template_roots must be a mapping")
        return cls(
            template_root=str(config.get("template_root", "")),
            runtime_root=str(config.get("runtime_root", "")),
            container_root=str(
                config.get(
                    "container_root", "/var/opt/web-agent-magento-state"
                )
            ),
            sites=[str(site) for site in config.get("sites", ["shopping", "shopping_admin"])],
            template_roots={
                str(site): str(path)
                for site, path in raw_template_roots.items()
            },
        )

    def _applies(self, identity: StateIdentity) -> bool:
        return identity.site in self.sites

    def _environment_root(self, identity: StateIdentity) -> Path:
        return self.runtime_root / "environments" / _safe_token(identity.environment_id)

    def _branch_root(self, identity: StateIdentity) -> Path:
        return self._environment_root(identity) / "branches" / _safe_token(identity.branch_id)

    def _checkpoint_root(self, identity: StateIdentity, checkpoint_id: str) -> Path:
        return self._environment_root(identity) / "checkpoints" / _safe_token(checkpoint_id)

    def _safe_remove(self, target: Path) -> None:
        resolved = target.resolve()
        if self.runtime_root not in resolved.parents:
            raise ValueError(
                f"refusing to remove path outside Magento runtime: {resolved}"
            )
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise PermissionError(
                f"failed to remove Magento runtime path: {resolved}"
            )

    def _template_for(self, identity: StateIdentity) -> Path:
        return self.template_roots.get(identity.site, self.template_root)

    def _validate_template(self, identity: StateIdentity) -> Path:
        template_root = self._template_for(identity)
        marker = template_root / ".web-agent-magento-state-ready"
        if not marker.is_file():
            raise FileNotFoundError(
                f"quiesced Magento state template marker not found: {marker}"
            )
        for component in self._COMPONENTS:
            source = template_root / component
            if not source.is_dir():
                raise FileNotFoundError(
                    f"Magento template component does not exist: {source}"
                )
        return template_root

    def _clone_tree(self, source: Path, target: Path) -> None:
        if not source.is_dir():
            raise FileNotFoundError(f"missing Magento state source: {source}")
        self._safe_remove(target)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o2770)
        subprocess.run(
            ["cp", "-a", "--reflink=always", f"{source}/.", str(target)],
            check=True,
            capture_output=True,
            timeout=300,
        )

    def _clone_template(self, target: Path, identity: StateIdentity) -> None:
        template_root = self._validate_template(identity)
        self._safe_remove(target)
        target.mkdir(parents=True, exist_ok=True)
        for component in self._COMPONENTS:
            self._clone_tree(template_root / component, target / component)
        # Magento's request-scoped var directory is intentionally empty. It is
        # derived cache/log/tmp data, not part of a checkpoint's authority.
        (target / "var").mkdir(parents=True, exist_ok=True)
        (target / "var").chmod(0o2770)

    def prepare(self, identity: StateIdentity) -> None:
        if not self._applies(identity):
            return
        target = self._branch_root(identity)
        if not target.is_dir():
            self._clone_template(target, identity)

    def quiesce(self, identity: StateIdentity) -> None:
        return None

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None:
        if not self._applies(identity):
            return
        source = self._branch_root(identity)
        target = self._checkpoint_root(identity, checkpoint_id)
        self._clone_tree(source, target)

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None:
        if not self._applies(target):
            return
        checkpoint = self._checkpoint_root(source, checkpoint_id)
        self._clone_tree(checkpoint, self._branch_root(target))

    def reset(self, source: StateIdentity, target: StateIdentity) -> None:
        if not self._applies(target):
            return
        self._safe_remove(self._environment_root(source))
        self._clone_template(self._branch_root(target), target)

    def activate(self, identity: StateIdentity) -> None:
        if not self._applies(identity):
            return
        if not self._branch_root(identity).is_dir():
            raise FileNotFoundError(
                f"Magento state branch is not prepared: {self._branch_root(identity)}"
            )

    def cleanup(self, identity: StateIdentity) -> None:
        if not self._applies(identity):
            return
        self._safe_remove(self._environment_root(identity))

    def describe(self, identity: StateIdentity) -> dict[str, Any]:
        if not self._applies(identity):
            return {}
        branch = (
            self.container_root
            / "environments"
            / _safe_token(identity.environment_id)
            / "branches"
            / _safe_token(identity.branch_id)
        )
        return {
            "magento": {
                "state_root": str(branch),
                "media_root": str(branch / "media"),
                "session_root": str(branch / "sessions"),
                "var_root": str(branch / "var" / f"g{identity.generation}"),
            }
        }


class StateContextPublisher:
    def __init__(self, runtime_root: str | Path) -> None:
        self.root = Path(runtime_root).resolve() / "contexts"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, identity: StateIdentity) -> Path:
        return self.root / f"{_safe_token(identity.environment_id)}.json"

    def publish(self, identity: StateIdentity, details: dict[str, Any]) -> None:
        payload = {
            "environment_id": identity.environment_id,
            "site": identity.site,
            "branch_id": identity.branch_id,
            "generation": identity.generation,
            "database_name": identity.database_name,
            "state_namespace": identity.namespace,
            "redis_cache_prefix": identity.redis_cache_prefix,
            "redis_state_prefix": identity.redis_state_prefix,
            "opensearch_index": identity.search_index,
            **details,
        }
        destination = self.path_for(identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", text=True
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def remove(self, identity: StateIdentity) -> None:
        self.path_for(identity).unlink(missing_ok=True)


class NonDBStateCoordinator:
    def __init__(
        self,
        backends: Iterable[StateBackend],
        publisher: StateContextPublisher | None,
    ) -> None:
        self.backends = list(backends)
        self.publisher = publisher

    @classmethod
    def from_config(cls, config: DictConfig | None) -> "NonDBStateCoordinator":
        def enabled(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        if not config or not enabled(config.get("enabled", False)):
            return cls([], None)
        worker_mode = str(config.get("worker_mode", "shared"))
        runtime_root = str(config.get("runtime_root", "./runtime/non_db_state"))
        backends: list[StateBackend] = []
        overlay = config.get("overlay", {})
        if enabled(overlay.get("enabled", False)):
            backends.append(OverlayFilesystemBackend.from_config(overlay, worker_mode))
        gitlab_state = config.get("gitlab_state", {})
        if enabled(gitlab_state.get("enabled", False)):
            backends.append(GitLabReflinkStateBackend.from_config(gitlab_state))
        magento_state = config.get("magento_state", {})
        if enabled(magento_state.get("enabled", False)):
            backends.append(MagentoReflinkStateBackend.from_config(magento_state))
        for hook in config.get("command_hooks", []):
            if enabled(hook.get("enabled", True)):
                backends.append(CommandHookBackend.from_config(hook))
        return cls(backends, StateContextPublisher(runtime_root))

    def _details(self, identity: StateIdentity) -> dict[str, Any]:
        details: dict[str, Any] = {}
        for backend in self.backends:
            details.update(backend.describe(identity))
        return details

    def prepare(self, identity: StateIdentity) -> None:
        for backend in self.backends:
            backend.prepare(identity)
        self.activate(identity)

    def quiesce(self, identity: StateIdentity) -> None:
        for backend in self.backends:
            backend.quiesce(identity)

    def checkpoint(self, identity: StateIdentity, checkpoint_id: str) -> None:
        for backend in self.backends:
            backend.checkpoint(identity, checkpoint_id)

    def fork(
        self,
        source: StateIdentity,
        checkpoint_id: str,
        target: StateIdentity,
    ) -> None:
        for backend in self.backends:
            backend.fork(source, checkpoint_id, target)

    def reset(self, source: StateIdentity, target: StateIdentity) -> None:
        for backend in self.backends:
            backend.reset(source, target)

    def activate(self, identity: StateIdentity) -> None:
        for backend in self.backends:
            backend.activate(identity)
        if self.publisher is not None:
            self.publisher.publish(identity, self._details(identity))

    def cleanup(self, identity: StateIdentity) -> None:
        for backend in reversed(self.backends):
            backend.cleanup(identity)
        if self.publisher is not None:
            self.publisher.remove(identity)
