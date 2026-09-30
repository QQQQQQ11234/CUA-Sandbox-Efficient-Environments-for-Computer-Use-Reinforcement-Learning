from __future__ import annotations

import re
import hashlib
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from threading import RLock
from typing import Any

from omegaconf import DictConfig, OmegaConf

from .mysql_xfs_isolation import MySQLBranchResource, MySQLXFSReflinkManager
from .non_db_state import NonDBStateCoordinator, StateIdentity
from .route_registry import ACTIVE, FROZEN, SQLiteRouteRegistry
from .route_token import RouteTokenSigner
from .state_audit import StateCapabilityGate

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(name: str) -> str:
    if not _VALID_IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid postgres identifier: {name!r}")
    return f'"{name}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_token(raw: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip())
    token = re.sub(r"_+", "_", token).strip("_")
    if not token:
        token = "agent"
    if token[0].isdigit():
        token = f"a_{token}"
    return token


def _normalize_fragment(raw: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip())
    fragment = re.sub(r"_+", "_", fragment).strip("_")
    return fragment or "value"


@dataclass(frozen=True)
class DBAgentSession:
    agent_id: str
    db_name: str
    site: str
    headers: dict[str, str]
    shared_site_hosts: dict[str, str]
    # MySQL-specific fields (None for PostgreSQL sites)
    mysql_datadir: str | None = None
    mysql_port: int | None = None
    # Kept for result-schema compatibility. The container-free backend always
    # leaves this field unset.
    mysql_container: str | None = None


class DBIsolationManager:
    def __init__(self, config: DictConfig, logger) -> None:
        self.logger = logger
        mysql_config = config.get("mysql_xfs", {})
        mysql_requested = self._config_enabled(mysql_config.get("enabled", False))
        self.admin_dsn = str(config.get("admin_dsn", "")).strip()
        self.admin_docker_container = str(
            config.get("admin_docker_container", "")
        ).strip()
        self.admin_docker_psql_command = str(
            config.get("admin_docker_psql_command", "gitlab-psql")
        ).strip()
        if not self.admin_dsn and not self.admin_docker_container and not mysql_requested:
            raise ValueError(
                "db isolation requires `environment.db_isolation.admin_dsn` or "
                "`environment.db_isolation.admin_docker_container`"
            )

        self.base_template_db = str(config.get("base_template_db", "base_template_db"))
        self.clone_strategy = str(config.get("clone_strategy", "FILE_COPY")).upper()
        if self.clone_strategy not in {"FILE_COPY", "WAL_LOG", "TEMPLATE"}:
            raise ValueError(f"unsupported clone strategy: {self.clone_strategy}")

        self.agent_db_prefix = str(config.get("agent_db_prefix", "agent_"))
        self.agent_db_suffix = str(config.get("agent_db_suffix", "_db"))
        self.route_token_header = str(config.get("route_token_header", "X-Agent-Route"))
        route_token_secret = str(config.get("route_token_secret", ""))
        if not route_token_secret:
            raise ValueError("db isolation requires `environment.db_isolation.route_token_secret`")
        route_token_ttl_seconds = int(config.get("route_token_ttl_seconds", 86400))
        self.route_token_signer = RouteTokenSigner(
            route_token_secret,
            ttl_seconds=route_token_ttl_seconds,
        )
        self.drop_on_close = bool(config.get("drop_on_close", True))
        self.reset_on_setup = bool(config.get("reset_on_setup", True))
        self.route_drain_timeout_seconds = float(
            config.get("route_drain_timeout_seconds", 30.0)
        )
        self.evaluation_drain_timeout_seconds = float(
            config.get("evaluation_drain_timeout_seconds", 30.0)
        )
        self.rails_pool_release_enabled = bool(
            config.get("rails_pool_release_enabled", True)
        )
        self.rails_pool_release_timeout_seconds = float(
            config.get("rails_pool_release_timeout_seconds", 2.0)
        )
        self.rails_pool_release_path = str(
            config.get("rails_pool_release_path", "/__web_agent_release_pool")
        )
        self.checkpoint_tag = str(config.get("checkpoint_tag", "_ckpt_"))
        self.branch_tag = str(config.get("branch_tag", "_branch_"))
        registry_path = str(config.get("route_registry_path", "./runtime/db_routes.sqlite3"))
        self.route_registry = SQLiteRouteRegistry(registry_path)
        self._lock = RLock()
        self._current_db_by_agent: dict[str, str] = {}
        self._owned_dbs_by_agent: dict[str, set[str]] = {}

        state_audit_config = config.get("state_audit", {})
        self.state_capability_gate: StateCapabilityGate | None = None
        if bool(state_audit_config.get("enabled", False)):
            self.state_capability_gate = StateCapabilityGate.from_paths(
                state_audit_config.get("audit_paths", []),
                state_audit_config.get("task_manifest_paths", []),
            )
        self.non_db_state = NonDBStateCoordinator.from_config(
            config.get("non_db_state", {})
        )

        shared_site_hosts = OmegaConf.to_container(config.get("shared_site_hosts", {}), resolve=True)
        if not isinstance(shared_site_hosts, dict):
            raise ValueError("`shared_site_hosts` must be a mapping site_name -> host_or_ip")
        self.shared_site_hosts = {str(k): str(v) for k, v in shared_site_hosts.items()}

        # MySQL XFS reflink support
        self.mysql_enabled = mysql_requested
        self.mysql_config = mysql_config
        self.mysql_sites = set(mysql_config.get("sites", ["shopping", "shopping_admin"]))
        template_config = mysql_config.get("templates", {})
        raw_templates = (
            OmegaConf.to_container(template_config, resolve=True)
            if OmegaConf.is_config(template_config)
            else template_config
        )
        if not isinstance(raw_templates, dict):
            raise ValueError("mysql_xfs.templates must map site to template name")
        self.mysql_templates = {
            str(site): str(template) for site, template in raw_templates.items()
        }
        # Initialize the comparatively strict host/XFS backend only when a
        # Shopping task is admitted. PostgreSQL-only runs must not require a
        # local MySQL installation, while Shopping still fails closed.
        self.mysql_manager: MySQLXFSReflinkManager | None = None
        self._mysql_managers: dict[str, MySQLXFSReflinkManager] = {}
        if self.mysql_enabled:
            self.logger.info(
                "MySQL XFS reflink configured for sites: %s", self.mysql_sites
            )

        # Track MySQL resources for cleanup
        self._mysql_resources_by_agent: dict[str, MySQLBranchResource] = {}
        self._mysql_manager_by_agent: dict[str, MySQLXFSReflinkManager] = {}

    @staticmethod
    def _config_enabled(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def build_agent_id(self, env_uuid: str, task_config: dict[str, Any] | None) -> str:
        task_id = "na"
        if task_config and task_config.get("task_id") is not None:
            task_id = str(task_config["task_id"])
        raw = f"task_{task_id}_{env_uuid}"
        return _normalize_token(raw)

    def build_agent_db_name(self, agent_id: str) -> str:
        token = _normalize_token(agent_id)
        db_name = f"{self.agent_db_prefix}{token}{self.agent_db_suffix}"
        # Validate final database name before SQL execution.
        _quote_identifier(db_name)
        return db_name

    def prepare_for_task(self, env_uuid: str, task_config: dict[str, Any] | None) -> DBAgentSession:
        if self.state_capability_gate is not None:
            self.state_capability_gate.assert_supported(task_config or {})
        agent_id = self.build_agent_id(env_uuid=env_uuid, task_config=task_config)
        sites = list(task_config.get("sites", [])) if task_config else []
        site = str(sites[0]) if len(sites) == 1 else ",".join(map(str, sites))

        # Check if this is a MySQL site. Site type is independent from backend
        # enablement; silently sending Shopping to PostgreSQL would violate
        # state and reward isolation.
        if self._is_mysql_site(site):
            if not self.mysql_enabled:
                raise RuntimeError(
                    f"MySQL XFS isolation is disabled for Shopping site {site!r}"
                )
            self._ensure_mysql_manager(site)
            return self._prepare_mysql_agent(agent_id, site, task_config)
        else:
            return self._prepare_postgresql_agent(agent_id, site, task_config)

    def _is_mysql_site(self, site: str) -> bool:
        """Check if the site uses MySQL instead of PostgreSQL."""
        return site in self.mysql_sites

    def _ensure_mysql_manager(self, site: str) -> MySQLXFSReflinkManager:
        # prepare_for_task is intentionally concurrency-safe.  Without this
        # lock, the first parallel batch can construct many managers for the
        # same site and race their filesystem/port capability probes.
        with self._lock:
            manager = self._mysql_managers.get(site)
            if manager is None:
                config = OmegaConf.create(
                    OmegaConf.to_container(self.mysql_config, resolve=True)
                )
                if site in self.mysql_templates:
                    config.template_name = self.mysql_templates[site]
                manager = MySQLXFSReflinkManager(config, self.logger)
                self._mysql_managers[site] = manager
                # Compatibility for callers that inspect the active manager.
                self.mysql_manager = manager
            return manager

    def _prepare_mysql_agent(
        self,
        agent_id: str,
        site: str,
        task_config: dict[str, Any] | None,
    ) -> DBAgentSession:
        """Prepare MySQL environment using XFS reflink cloning."""
        self.logger.info(f"Preparing MySQL environment for agent {agent_id} on site {site}")

        # Clone a cold datadir and start a small host mysqld. The Magento HTTP
        # application remains shared and obtains this endpoint from the trusted
        # route registry.
        mysql_manager = self._ensure_mysql_manager(site)
        resource = mysql_manager.prepare(agent_id, "root")

        # Track MySQL resources for cleanup
        with self._lock:
            self._mysql_resources_by_agent[agent_id] = resource
            self._mysql_manager_by_agent[agent_id] = mysql_manager

        # Register route (reuse PostgreSQL route registry).  Route publication
        # is inside the failure boundary so a busy/invalid registry cannot
        # leak a running mysqld or a reflink branch.
        db_name = resource.database_name
        route_registered = False
        try:
            route = self.route_registry.set_route(
                agent_id,
                db_name,
                environment_id=agent_id,
                site=site,
                branch_id="root",
                generation=1,
                lifecycle_state=FROZEN,
                db_engine="mysql",
                db_host=resource.host,
                db_port=resource.port,
            )
            route_registered = True
            # Setup non-DB state if needed
            self.non_db_state.prepare(StateIdentity.from_route(route))
            self.route_registry.activate(agent_id)
        except Exception:
            self.logger.exception("Failed to prepare MySQL agent %s", agent_id)
            if route_registered:
                try:
                    failed_route = self.route_registry.resolve_route(agent_id)
                    self.non_db_state.cleanup(
                        StateIdentity.from_route(failed_route)
                    )
                except Exception:
                    self.logger.exception(
                        "Failed to roll back Magento non-DB state for %s",
                        agent_id,
                    )
            # Cleanup on failure
            mysql_manager.cleanup(resource)
            with self._lock:
                self._mysql_resources_by_agent.pop(agent_id, None)
                self._mysql_manager_by_agent.pop(agent_id, None)
            if route_registered:
                self.route_registry.remove(agent_id)
            raise

        # Generate auth headers
        headers = {self.route_token_header: self.route_token_signer.issue(agent_id)}

        return DBAgentSession(
            agent_id=agent_id,
            db_name=db_name,
            site=site,
            headers=headers,
            shared_site_hosts=dict(self.shared_site_hosts),
            mysql_datadir=resource.datadir,
            mysql_port=resource.port,
        )

    def _prepare_postgresql_agent(
        self,
        agent_id: str,
        site: str,
        task_config: dict[str, Any] | None,
    ) -> DBAgentSession:
        """Prepare PostgreSQL environment (original logic)."""
        db_name = self.build_agent_db_name(agent_id)

        if self.reset_on_setup:
            self.reset_agent_db(db_name)
        else:
            self.create_agent_db_if_missing(db_name)

        with self._lock:
            self._current_db_by_agent[agent_id] = db_name
            self._owned_dbs_by_agent[agent_id] = {db_name}
        route = self.route_registry.set_route(
            agent_id,
            db_name,
            environment_id=agent_id,
            site=site,
            branch_id="root",
            generation=1,
            lifecycle_state=FROZEN,
        )
        try:
            self.non_db_state.prepare(StateIdentity.from_route(route))
            self.route_registry.activate(agent_id)
        except Exception:
            self.logger.exception(
                "Non-DB state preparation failed for agent %s", agent_id
            )
            self.route_registry.remove(agent_id)
            with self._lock:
                self._current_db_by_agent.pop(agent_id, None)
                self._owned_dbs_by_agent.pop(agent_id, None)
            if self.drop_on_close:
                self.drop_db(db_name)
            raise

        headers = {self.route_token_header: self.route_token_signer.issue(agent_id)}
        return DBAgentSession(
            agent_id=agent_id,
            db_name=db_name,
            site=site,
            headers=headers,
            shared_site_hosts=dict(self.shared_site_hosts),
        )

    def cleanup(self, session: DBAgentSession | None) -> None:
        if not session:
            return

        # Handle MySQL cleanup
        if session.mysql_datadir is not None:
            self._cleanup_mysql_agent(session)
            return

        # Handle PostgreSQL cleanup (original logic)
        try:
            route = self._freeze_and_drain(session.agent_id)
        except KeyError:
            route = None
        if route is not None:
            identity = StateIdentity.from_route(route)
            self.non_db_state.quiesce(identity)
            self.non_db_state.cleanup(identity)
        with self._lock:
            owned_dbs = set(self._owned_dbs_by_agent.pop(session.agent_id, {session.db_name}))
            self._current_db_by_agent.pop(session.agent_id, None)
        self.route_registry.remove(session.agent_id)
        if not self.drop_on_close:
            return
        for db_name in sorted(owned_dbs, reverse=True):
            self.drop_db(db_name)

    def _cleanup_mysql_agent(self, session: DBAgentSession) -> None:
        """Cleanup MySQL agent resources."""
        self.logger.info(f"Cleaning up MySQL agent {session.agent_id}")

        route = None
        cleanup_error: Exception | None = None
        try:
            route = self._freeze_and_drain(session.agent_id)
        except KeyError:
            pass
        except Exception as error:
            cleanup_error = error
            self.logger.exception(
                "Magento route drain failed for %s", session.agent_id
            )
        if route is not None:
            try:
                identity = StateIdentity.from_route(route)
                self.non_db_state.quiesce(identity)
                self.non_db_state.cleanup(identity)
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
                self.logger.exception(
                    "Magento non-DB cleanup failed for %s", session.agent_id
                )
        try:
            self.route_registry.remove(session.agent_id)
        except Exception as error:
            if cleanup_error is None:
                cleanup_error = error
            self.logger.exception(
                "Magento route removal failed for %s", session.agent_id
            )
        finally:
            # Process/datadir cleanup is mandatory even if route drain or a
            # derived Redis/OpenSearch cleanup hook fails.
            with self._lock:
                mysql_resource = self._mysql_resources_by_agent.pop(
                    session.agent_id, None
                )
                mysql_manager = self._mysql_manager_by_agent.pop(
                    session.agent_id, None
                )
            if mysql_resource and mysql_manager:
                mysql_manager.cleanup(mysql_resource)
        if cleanup_error is not None:
            raise cleanup_error

    def current_db(self, agent_id: str) -> str:
        with self._lock:
            db_name = self._current_db_by_agent.get(agent_id)
        if db_name is not None:
            return db_name
        return self.route_registry.resolve(agent_id)

    def wait_for_background_jobs(self, session: DBAgentSession) -> None:
        self.route_registry.wait_for_background_jobs(
            session.agent_id,
            timeout_seconds=self.evaluation_drain_timeout_seconds,
        )

    def _mysql_resource(self, agent_id: str) -> MySQLBranchResource:
        """Return the active MySQL branch resource for an agent.

        The route registry is the source of truth for the endpoint exposed to
        the application, while this in-process map owns the host process and
        its lifecycle.  Keeping the lookup in one place prevents checkpoint,
        fork, and reset from accidentally operating on a stale branch.
        """
        with self._lock:
            resource = self._mysql_resources_by_agent.get(agent_id)
        if resource is None:
            raise KeyError(f"unknown MySQL resource for agent {agent_id!r}")
        return resource

    def _mysql_backend(self, agent_id: str) -> MySQLXFSReflinkManager:
        with self._lock:
            manager = self._mysql_manager_by_agent.get(agent_id)
        if manager is None:
            raise KeyError(f"unknown MySQL backend for agent {agent_id!r}")
        return manager

    def _checkpoint_mysql(self, session: DBAgentSession, step: int) -> str:
        route = self._freeze_and_drain(session.agent_id)
        identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(identity)
        resource = self._mysql_resource(session.agent_id)
        mysql_manager = self._mysql_backend(session.agent_id)
        checkpoint_name = self._checkpoint_db_name(session.agent_id, step)
        try:
            # The active mysqld is stopped while the datadir is reflink-cloned;
            # it is restarted before the route is reactivated.
            updated = mysql_manager.checkpoint(resource, checkpoint_name)
            with self._lock:
                self._mysql_resources_by_agent[session.agent_id] = updated
            self.non_db_state.checkpoint(identity, checkpoint_name)
            next_identity = StateIdentity(
                environment_id=identity.environment_id,
                site=identity.site,
                branch_id=identity.branch_id,
                generation=identity.generation + 1,
                database_name=updated.database_name,
            )
            self.non_db_state.activate(next_identity)
            self.route_registry.activate(
                session.agent_id,
                generation=next_identity.generation,
                db_engine="mysql",
                db_host=updated.host,
                db_port=updated.port,
            )
            return checkpoint_name
        except Exception:
            self.logger.exception(
                "MySQL checkpoint failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def _fork_mysql(
        self, session: DBAgentSession, checkpoint_name: str, branch_name: str
    ) -> str:
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        resource = self._mysql_resource(session.agent_id)
        mysql_manager = self._mysql_backend(session.agent_id)
        branch_id = _normalize_fragment(branch_name)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id=branch_id,
            generation=source_identity.generation + 1,
            database_name=resource.database_name,
        )
        try:
            child = mysql_manager.fork(resource, checkpoint_name, branch_id)
            with self._lock:
                self._mysql_resources_by_agent[session.agent_id] = child
            # Publish the target endpoint while still FROZEN so trusted
            # maintenance hooks (Magento reindex/cache warmup) can resolve the
            # child without admitting browser traffic.
            self.route_registry.set_route(
                session.agent_id,
                child.database_name,
                environment_id=target_identity.environment_id,
                site=target_identity.site,
                branch_id=target_identity.branch_id,
                generation=target_identity.generation,
                lifecycle_state=FROZEN,
                db_engine="mysql",
                db_host=child.host,
                db_port=child.port,
            )
            self.non_db_state.fork(
                source_identity,
                checkpoint_name,
                target_identity,
            )
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                branch_id=branch_id,
                generation=target_identity.generation,
                db_engine="mysql",
                db_host=child.host,
                db_port=child.port,
            )
            return child.database_name
        except Exception:
            self.logger.exception(
                "MySQL fork failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def _reset_mysql(self, session: DBAgentSession) -> str:
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        resource = self._mysql_resource(session.agent_id)
        mysql_manager = self._mysql_backend(session.agent_id)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id="root",
            generation=source_identity.generation + 1,
            database_name=resource.database_name,
        )
        try:
            reset_resource = mysql_manager.reset(resource)
            with self._lock:
                self._mysql_resources_by_agent[session.agent_id] = reset_resource
            self.route_registry.set_route(
                session.agent_id,
                reset_resource.database_name,
                environment_id=target_identity.environment_id,
                site=target_identity.site,
                branch_id=target_identity.branch_id,
                generation=target_identity.generation,
                lifecycle_state=FROZEN,
                db_engine="mysql",
                db_host=reset_resource.host,
                db_port=reset_resource.port,
            )
            self.non_db_state.reset(source_identity, target_identity)
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                db_name=reset_resource.database_name,
                branch_id="root",
                generation=target_identity.generation,
                db_engine="mysql",
                db_host=reset_resource.host,
                db_port=reset_resource.port,
            )
            return reset_resource.database_name
        except Exception:
            self.logger.exception(
                "MySQL reset failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def _restore_mysql(
        self, session: DBAgentSession, checkpoint_name: str
    ) -> str:
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        resource = self._mysql_resource(session.agent_id)
        mysql_manager = self._mysql_backend(session.agent_id)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id=source_identity.branch_id,
            generation=source_identity.generation + 1,
            database_name=resource.database_name,
        )
        try:
            restored = mysql_manager.restore(resource, checkpoint_name)
            with self._lock:
                self._mysql_resources_by_agent[session.agent_id] = restored
            self.non_db_state.fork(
                source_identity,
                checkpoint_name,
                target_identity,
            )
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                generation=target_identity.generation,
                db_engine="mysql",
                db_host=restored.host,
                db_port=restored.port,
            )
            return restored.database_name
        except Exception:
            self.logger.exception(
                "MySQL restore failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def checkpoint(self, session: DBAgentSession, step: int) -> str:
        if session.mysql_datadir is not None:
            return self._checkpoint_mysql(session, step)
        route = self._freeze_and_drain(session.agent_id)
        identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(identity)
        source_db = route.db_name
        checkpoint_name = self._checkpoint_db_name(session.agent_id, step)
        try:
            self.drop_db(checkpoint_name)
            self.clone_db(source_db, checkpoint_name)
            self._register_owned_db(session.agent_id, checkpoint_name)
            self.non_db_state.checkpoint(identity, checkpoint_name)
            next_identity = StateIdentity(
                environment_id=identity.environment_id,
                site=identity.site,
                branch_id=identity.branch_id,
                generation=identity.generation + 1,
                database_name=identity.database_name,
            )
            self.non_db_state.activate(next_identity)
            self.route_registry.activate(
                session.agent_id,
                generation=next_identity.generation,
            )
            return checkpoint_name
        except Exception:
            self.logger.exception(
                "State checkpoint failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def fork(self, session: DBAgentSession, checkpoint_name: str, branch_name: str) -> str:
        if session.mysql_datadir is not None:
            return self._fork_mysql(session, checkpoint_name, branch_name)
        self._assert_owned_db(session.agent_id, checkpoint_name)
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        branch_db = self._branch_db_name(session.agent_id, branch_name)
        branch_id = _normalize_fragment(branch_name)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id=branch_id,
            generation=source_identity.generation + 1,
            database_name=branch_db,
        )
        try:
            self.drop_db(branch_db)
            self.clone_db(checkpoint_name, branch_db)
            self._register_owned_db(session.agent_id, branch_db)
            self.non_db_state.fork(
                source_identity,
                checkpoint_name,
                target_identity,
            )
            with self._lock:
                self._current_db_by_agent[session.agent_id] = branch_db
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                db_name=branch_db,
                branch_id=branch_id,
                generation=target_identity.generation,
            )
            return branch_db
        except Exception:
            self.logger.exception(
                "State fork failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def reset(self, session: DBAgentSession) -> str:
        if session.mysql_datadir is not None:
            return self._reset_mysql(session)
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id="root",
            generation=source_identity.generation + 1,
            database_name=session.db_name,
        )
        try:
            self.reset_agent_db(session.db_name)
            self.non_db_state.reset(source_identity, target_identity)
            with self._lock:
                self._current_db_by_agent[session.agent_id] = session.db_name
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                db_name=session.db_name,
                branch_id="root",
                generation=target_identity.generation,
            )
            return session.db_name
        except Exception:
            # A failed destructive reset remains frozen. Serving the partially
            # rebuilt environment would violate isolation and reward fidelity.
            self.logger.exception(
                "Database reset failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def restore(self, session: DBAgentSession, checkpoint_name: str) -> str:
        """Restore the currently routed branch from an owned checkpoint."""
        if session.mysql_datadir is not None:
            return self._restore_mysql(session, checkpoint_name)
        self._assert_owned_db(session.agent_id, checkpoint_name)
        route = self._freeze_and_drain(session.agent_id)
        source_identity = StateIdentity.from_route(route)
        self.non_db_state.quiesce(source_identity)
        target_db = source_identity.database_name
        target_identity = StateIdentity(
            environment_id=source_identity.environment_id,
            site=source_identity.site,
            branch_id=source_identity.branch_id,
            generation=source_identity.generation + 1,
            database_name=target_db,
        )
        try:
            self.drop_db(target_db)
            self.clone_db(checkpoint_name, target_db)
            self.non_db_state.fork(
                source_identity,
                checkpoint_name,
                target_identity,
            )
            with self._lock:
                self._current_db_by_agent[session.agent_id] = target_db
            self.non_db_state.activate(target_identity)
            self.route_registry.activate(
                session.agent_id,
                db_name=target_db,
                generation=target_identity.generation,
            )
            return target_db
        except Exception:
            self.logger.exception(
                "Database restore failed; route remains frozen for agent %s",
                session.agent_id,
            )
            raise

    def _freeze_and_drain(self, agent_id: str):
        self.route_registry.freeze(agent_id)
        route = self.route_registry.wait_for_drained(
            agent_id,
            timeout_seconds=self.route_drain_timeout_seconds,
        )
        self._release_rails_pool(route)
        return route

    def _release_rails_pool(self, route) -> None:
        if not self.rails_pool_release_enabled or route.site != "gitlab":
            return
        host = self.shared_site_hosts.get(route.site)
        if not host:
            self.logger.warning(
                "Cannot release Rails pool for %s: no shared GitLab host configured",
                route.agent_id,
            )
            return
        base_url = host if "://" in host else f"http://{host}"
        url = f"{base_url.rstrip('/')}/{self.rails_pool_release_path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=b"",
            method="POST",
            headers={
                self.route_token_header: self.route_token_signer.issue(route.agent_id),
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.rails_pool_release_timeout_seconds
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Rails pool release returned HTTP {response.status}"
                    )
        except (OSError, urllib.error.HTTPError, RuntimeError) as exc:
            # PostgreSQL FORCE still terminates live connections. Other Puma
            # workers reap the removed pool key from the route registry.
            self.logger.warning(
                "Immediate Rails pool release failed for %s: %s",
                route.agent_id,
                exc,
            )

    def _checkpoint_db_name(self, agent_id: str, step: int) -> str:
        return self._derived_db_name(agent_id, self.checkpoint_tag, str(step))

    def _branch_db_name(self, agent_id: str, branch_name: str) -> str:
        return self._derived_db_name(agent_id, self.branch_tag, branch_name)

    def _derived_db_name(self, agent_id: str, tag: str, value: str) -> str:
        token = _normalize_token(agent_id)
        suffix = _normalize_fragment(value)
        db_name = f"{self.agent_db_prefix}{token}{tag}{suffix}"
        # PostgreSQL identifiers are limited to NAMEDATALEN-1 (63) bytes.
        # Keep derived names deterministic so restore/fork can refer to the
        # exact same checkpoint even when an environment id is long.
        if len(db_name) > 63:
            digest = hashlib.sha1(db_name.encode("utf-8")).hexdigest()[:10]
            db_name = db_name[:52] + "_" + digest
        _quote_identifier(db_name)
        return db_name

    def _register_owned_db(self, agent_id: str, db_name: str) -> None:
        with self._lock:
            self._owned_dbs_by_agent.setdefault(agent_id, set()).add(db_name)

    def _assert_owned_db(self, agent_id: str, db_name: str) -> None:
        with self._lock:
            if db_name not in self._owned_dbs_by_agent.get(agent_id, set()):
                raise ValueError(f"database {db_name!r} is not owned by agent {agent_id!r}")

    def reset_agent_db(self, db_name: str) -> None:
        self.drop_db(db_name)
        self.clone_db(self.base_template_db, db_name)

    def create_agent_db_if_missing(self, db_name: str) -> None:
        if self.db_exists(db_name):
            return
        self.clone_db(self.base_template_db, db_name)

    def db_exists(self, db_name: str) -> bool:
        rows = self._execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_name,),
            fetch=True,
        )
        return bool(rows)

    def clone_db(self, source_db: str, target_db: str) -> None:
        source_ident = _quote_identifier(source_db)
        target_ident = _quote_identifier(target_db)
        self.terminate_connections(source_db)
        if self.clone_strategy == "TEMPLATE":
            query = f"CREATE DATABASE {target_ident} TEMPLATE {source_ident}"
        else:
            query = (
                f"CREATE DATABASE {target_ident} TEMPLATE {source_ident} "
                f"STRATEGY {self.clone_strategy}"
            )
        self._execute(query)
        self.logger.info(f"Cloned database {target_db} from {source_db}")

    def terminate_connections(self, db_name: str) -> None:
        self._execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s
              AND pid <> pg_backend_pid()
            """,
            (db_name,),
        )

    def drop_db(self, db_name: str) -> None:
        ident = _quote_identifier(db_name)
        self.terminate_connections(db_name)
        self._execute(f"DROP DATABASE IF EXISTS {ident} WITH (FORCE)")
        self.logger.info(f"Dropped database if exists: {db_name}")

    def _execute(self, query: str, params: tuple[Any, ...] = (), fetch: bool = False) -> list[tuple[Any, ...]] | None:
        if self.admin_docker_container:
            return self._execute_in_docker(query, params, fetch)

        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "db isolation requires psycopg. Install with: pip install 'psycopg[binary]'"
            ) from exc

        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetch and cur.description is not None:
                    return list(cur.fetchall())
        return None

    def _execute_in_docker(
        self,
        query: str,
        params: tuple[Any, ...],
        fetch: bool,
    ) -> list[tuple[Any, ...]] | None:
        rendered_query = query
        for value in params:
            rendered_query = rendered_query.replace("%s", _quote_literal(str(value)), 1)
        if "%s" in rendered_query:
            raise ValueError("not enough SQL parameters")

        command = [
            "docker",
            "exec",
            self.admin_docker_container,
            self.admin_docker_psql_command,
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-c",
            rendered_query,
        ]
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        if not fetch:
            return None
        return [tuple(line.split("|")) for line in result.stdout.splitlines() if line]
