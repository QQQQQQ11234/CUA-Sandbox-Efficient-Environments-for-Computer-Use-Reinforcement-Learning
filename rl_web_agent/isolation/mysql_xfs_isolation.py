"""Container-free MySQL datadir branching on a reflink-capable filesystem.

The Magento/PHP application is shared. Only the mutable MySQL datadir and a
small host ``mysqld`` process belong to an environment branch. Database
endpoints are published through the trusted route registry; they are never
used as browser HTTP targets.

This backend deliberately does not use ``docker run``. A deployment must also
install a Magento route adapter for DB/cache/media/search state before Shopping
tasks are admitted by the state capability gate.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import shlex
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from omegaconf import DictConfig, OmegaConf


def _safe_token(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in value.strip()
    ).strip("_.")
    return normalized or "default"


@dataclass(frozen=True)
class MySQLBranchResource:
    environment_id: str
    branch_id: str
    datadir: str
    host: str
    port: int
    database_name: str
    process_id: int | None = None


class MySQLXFSReflinkManager:
    """Manage one lightweight host MySQL process per active logical branch.

    XFS reflink removes the full-copy cost that MySQL lacks at the database
    level. Only one branch for an environment is active at a time; inactive
    branches/checkpoints are cold CoW trees and consume no process memory.
    """

    def __init__(self, config: DictConfig, logger: logging.Logger):
        self.logger = logger
        self.xfs_base_path = Path(
            str(
                config.get(
                    "xfs_base_path",
                    "/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs",
                )
            )
        ).expanduser().resolve()
        self.template_name = _safe_token(
            str(config.get("template_name", "magento_template"))
        )
        self.template_path = self.xfs_base_path / self.template_name
        self.template_datadir = (
            self.template_path / "mysql"
            if (self.template_path / "mysql").is_dir()
            else self.template_path
        )
        self.runtime_root = Path(
            str(config.get("runtime_root", self.xfs_base_path / "runtime"))
        ).expanduser().resolve()
        self.bind_host = str(config.get("bind_host", "127.0.0.1"))
        self.admin_host = str(config.get("admin_host", "127.0.0.1"))
        self.advertised_host = str(config.get("advertised_host", self.bind_host))
        self.mysql_port_base = int(config.get("mysql_port_base", 13306))
        self.mysql_max_instances = int(config.get("mysql_max_instances", 1000))
        self.mysql_database = str(config.get("database_name", "magentodb"))
        self.mysql_user = str(config.get("mysql_user", "root"))
        self.mysql_password = str(config.get("mysql_password", ""))
        self.startup_timeout = float(config.get("startup_timeout_seconds", 60))
        self.shutdown_timeout = float(config.get("shutdown_timeout_seconds", 30))
        self.launch_enabled = self._enabled(config.get("launch_enabled", True))
        self.launch_mode = str(config.get("launch_mode", "host"))
        if self.launch_mode not in {"host", "docker_exec"}:
            raise ValueError("mysql_xfs.launch_mode must be host or docker_exec")
        self.runtime_container = str(
            config.get("runtime_container", "shopping-mysql-runtime")
        ).strip()
        self.runtime_container_user = str(
            config.get("runtime_container_user", "")
        ).strip() or f"mysql:{os.getgid()}"
        self.mysqld_binary = str(config.get("mysqld_binary", "mysqld"))
        self.mysqladmin_binary = str(config.get("mysqladmin_binary", "mysqladmin"))
        configured_args = config.get("mysqld_extra_args", [])
        raw_args = (
            OmegaConf.to_container(configured_args, resolve=True)
            if OmegaConf.is_config(configured_args)
            else configured_args
        )
        if not isinstance(raw_args, list):
            raise ValueError("mysql_xfs.mysqld_extra_args must be an argv list")
        self.mysqld_extra_args = [str(value) for value in raw_args]
        self._processes: dict[tuple[str, str], subprocess.Popen[bytes]] = {}
        self._log_handles: dict[tuple[str, str], Any] = {}

        self._validate_paths()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._initialize_port_registry()

    @staticmethod
    def _enabled(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _validate_paths(self) -> None:
        if not self.xfs_base_path.is_dir():
            raise ValueError(
                f"MySQL XFS base path does not exist: {self.xfs_base_path}"
            )
        if not self.template_path.is_dir():
            raise ValueError(
                f"quiesced MySQL template not found: {self.template_path}"
            )
        marker = self.template_path / ".web-agent-mysql-template-ready"
        if not marker.is_file():
            raise ValueError(
                f"quiesced MySQL template marker not found: {marker}; "
                "run scripts/setup_mysql_template.sh"
            )
        if not self.template_datadir.is_dir():
            raise ValueError(
                f"MySQL template datadir not found: {self.template_datadir}"
            )
        if self.runtime_root == Path(self.runtime_root.anchor):
            raise ValueError("MySQL runtime root cannot be a filesystem root")
        if (
            self.runtime_root == self.template_path
            or self.template_path in self.runtime_root.parents
        ):
            raise ValueError("MySQL runtime root must not contain or replace the template")

        result = subprocess.run(
            ["stat", "-f", "-c", "%T", str(self.xfs_base_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "xfs":
            raise ValueError(
                f"mysql_xfs requires XFS; found {result.stdout.strip() or 'unknown'} "
                f"at {self.xfs_base_path}"
            )

        probe_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        probe_source = self.xfs_base_path / f".web-agent-reflink-probe-{probe_id}"
        probe_target = (
            self.xfs_base_path / f".web-agent-reflink-probe-copy-{probe_id}"
        )
        try:
            probe_source.write_bytes(b"web-agent-reflink-probe")
            subprocess.run(
                ["cp", "--reflink=always", str(probe_source), str(probe_target)],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"XFS reflink is unavailable at {self.xfs_base_path}"
            ) from exc
        finally:
            probe_source.unlink(missing_ok=True)
            probe_target.unlink(missing_ok=True)

        if self.launch_enabled and self.launch_mode == "host":
            for label, binary in (
                ("mysqld", self.mysqld_binary),
                ("mysqladmin", self.mysqladmin_binary),
            ):
                resolved = shutil.which(binary)
                if resolved is None and not (
                    Path(binary).is_file() and os.access(binary, os.X_OK)
                ):
                    raise ValueError(
                        f"mysql_xfs requires executable {label}: {binary!r}; "
                        "install a host MySQL runtime compatible with the template"
                    )
        elif self.launch_enabled:
            if not self.runtime_container:
                raise ValueError(
                    "mysql_xfs.runtime_container is required for docker_exec mode"
                )
            inspection = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    self.runtime_container,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if inspection.returncode != 0 or inspection.stdout.strip() != "true":
                raise ValueError(
                    f"shared MySQL runtime container is not running: "
                    f"{self.runtime_container}; run scripts/setup_mysql_runtime_container.sh"
                )
            template_image_file = self.template_path / "runtime-image-id"
            if not template_image_file.is_file():
                raise ValueError(
                    f"MySQL template runtime image metadata is missing: "
                    f"{template_image_file}"
                )
            runtime_image = subprocess.run(
                ["docker", "inspect", "-f", "{{.Image}}", self.runtime_container],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            expected_image = template_image_file.read_text().strip()
            if (
                runtime_image.returncode != 0
                or runtime_image.stdout.strip() != expected_image
            ):
                raise ValueError(
                    "MySQL template/runtime image mismatch; rebuild the template "
                    "and shared binary runtime from the same MariaDB image"
                )
            for label, binary in (
                ("mysqld", self.mysqld_binary),
                ("mysqladmin", self.mysqladmin_binary),
            ):
                probe = subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.runtime_container,
                        "sh",
                        "-lc",
                        f"command -v -- {shlex.quote(binary)}",
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if probe.returncode != 0:
                    raise ValueError(
                        f"{label} {binary!r} is missing in shared runtime "
                        f"container {self.runtime_container}"
                    )

    def _runtime_command(self, argv: list[str]) -> list[str]:
        if self.launch_mode == "docker_exec":
            command = ["docker", "exec", "-i"]
            if self.runtime_container_user:
                command.extend(["--user", self.runtime_container_user])
            return [*command, self.runtime_container, *argv]
        return argv

    @property
    def _port_registry_path(self) -> Path:
        return self.runtime_root / "mysql_ports.sqlite3"

    def _initialize_port_registry(self) -> None:
        with sqlite3.connect(self._port_registry_path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS allocations (
                    resource_key TEXT PRIMARY KEY,
                    port INTEGER NOT NULL UNIQUE
                )
                """
            )

    def _environment_root(self, environment_id: str) -> Path:
        return self.runtime_root / "environments" / _safe_token(environment_id)

    def _branch_root(self, environment_id: str, branch_id: str) -> Path:
        return (
            self._environment_root(environment_id)
            / "branches"
            / _safe_token(branch_id)
        )

    def _checkpoint_root(self, environment_id: str, checkpoint_id: str) -> Path:
        return (
            self._environment_root(environment_id)
            / "checkpoints"
            / _safe_token(checkpoint_id)
        )

    @staticmethod
    def _datadir(root: Path) -> Path:
        return root / "mysql"

    def _safe_remove(self, target: Path) -> None:
        resolved = target.resolve()
        if self.runtime_root not in resolved.parents:
            raise ValueError(
                f"refusing to remove path outside MySQL runtime: {resolved}"
            )
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise PermissionError(f"failed to remove MySQL runtime path: {resolved}")

    def _clone_tree(self, source: Path, target: Path) -> None:
        if not source.is_dir():
            raise FileNotFoundError(f"missing MySQL state source: {source}")
        self._safe_remove(target)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o2770)
        subprocess.run(
            ["cp", "-a", "--reflink=always", f"{source}/.", str(target)],
            check=True,
            capture_output=True,
            timeout=120,
        )

    def _resource_key(self, environment_id: str, branch_id: str) -> str:
        return f"{_safe_token(environment_id)}:{_safe_token(branch_id)}"

    def _port_is_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((self.bind_host, port))
            except OSError:
                return False
        return True

    def _allocate_port(self, environment_id: str, branch_id: str) -> int:
        resource_key = self._resource_key(environment_id, branch_id)
        digest = hashlib.sha256(resource_key.encode()).digest()
        first_offset = int.from_bytes(digest[:8], "big") % self.mysql_max_instances
        with sqlite3.connect(self._port_registry_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT port FROM allocations WHERE resource_key = ?", (resource_key,)
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            for attempt in range(self.mysql_max_instances):
                port = (
                    self.mysql_port_base
                    + (first_offset + attempt) % self.mysql_max_instances
                )
                allocated = connection.execute(
                    "SELECT 1 FROM allocations WHERE port = ?", (port,)
                ).fetchone()
                if allocated is not None or not self._port_is_available(port):
                    continue
                connection.execute(
                    "INSERT INTO allocations(resource_key, port) VALUES (?, ?)",
                    (resource_key, port),
                )
                return port
        raise RuntimeError("no free MySQL branch ports are available")

    def _release_port(self, environment_id: str, branch_id: str) -> None:
        resource_key = self._resource_key(environment_id, branch_id)
        with sqlite3.connect(self._port_registry_path, timeout=30) as connection:
            connection.execute(
                "DELETE FROM allocations WHERE resource_key = ?", (resource_key,)
            )

    def _config_path(self, resource: MySQLBranchResource) -> Path:
        return Path(resource.datadir).parent / "my.cnf"

    @staticmethod
    def _socket_path(resource: MySQLBranchResource) -> Path:
        # AF_UNIX paths are limited to 107 bytes on Linux.  Environment and
        # branch paths are intentionally descriptive, so keep the socket in a
        # short runtime path and key it by the already unique allocated port.
        return Path(f"/tmp/web-agent-mysql-{resource.port}.sock")

    def _remove_runtime_socket(self, resource: MySQLBranchResource) -> None:
        subprocess.run(
            self._runtime_command(["rm", "-f", str(self._socket_path(resource))]),
            capture_output=True,
            timeout=5,
            check=False,
        )

    def _signal_server(
        self, resource: MySQLBranchResource, signal_name: str
    ) -> None:
        pid_file = Path(resource.datadir).parent / "mysqld.pid"
        try:
            server_pid = int(pid_file.read_text().strip())
        except (FileNotFoundError, PermissionError, ValueError):
            return
        if server_pid <= 1:
            raise RuntimeError(f"refusing to signal invalid mysqld pid {server_pid}")
        if self.launch_mode == "docker_exec":
            subprocess.run(
                self._runtime_command(
                    ["kill", f"-{signal_name}", str(server_pid)]
                ),
                capture_output=True,
                timeout=5,
                check=False,
            )
            return
        os.kill(server_pid, getattr(signal, f"SIG{signal_name}"))

    def _write_config(self, resource: MySQLBranchResource) -> Path:
        root = Path(resource.datadir).parent
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o2770)
        # Concurrent branch-local MariaDB servers must not share /var/tmp.
        # They generate overlapping #sql-temptable names during Magento's
        # catalog search reindex and can otherwise delete each other's files.
        tmpdir = root / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmpdir.chmod(0o2770)
        config = self._config_path(resource)
        config.write_text(
            "\n".join(
                [
                    "[mysqld]",
                    f"datadir={resource.datadir}",
                    f"bind-address={self.bind_host}",
                    f"port={resource.port}",
                    f"socket={self._socket_path(resource)}",
                    f"pid-file={root / 'mysqld.pid'}",
                    f"log-error={root / 'mysqld.log'}",
                    f"tmpdir={tmpdir}",
                    "skip-log-bin",
                    "skip-name-resolve",
                    "performance-schema=OFF",
                    "innodb-buffer-pool-size=128M",
                    "max-connections=32",
                    "table-open-cache=256",
                    "table-definition-cache=256",
                    "",
                ]
            )
        )
        return config

    def _wait_ready(self, resource: MySQLBranchResource) -> None:
        deadline = time.monotonic() + self.startup_timeout
        command = self._runtime_command([
            self.mysqladmin_binary,
            "--protocol=tcp",
            f"--host={self.admin_host}",
            f"--port={resource.port}",
            f"--user={self.mysql_user}",
        ])
        if self.mysql_password:
            command.append(f"--password={self.mysql_password}")
        command.append("ping")
        while time.monotonic() < deadline:
            process = self._processes.get(
                (resource.environment_id, resource.branch_id)
            )
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    "mysqld exited during startup for "
                    f"{resource.environment_id}/{resource.branch_id}"
                )
            result = subprocess.run(
                command, capture_output=True, timeout=5, check=False
            )
            if result.returncode == 0:
                return
            time.sleep(0.2)
        raise TimeoutError(
            f"mysqld did not become ready on {self.admin_host}:{resource.port}"
        )

    def _start(self, resource: MySQLBranchResource) -> MySQLBranchResource:
        if not self.launch_enabled:
            return resource
        key = (resource.environment_id, resource.branch_id)
        existing = self._processes.get(key)
        if existing is not None and existing.poll() is None:
            return replace(resource, process_id=existing.pid)
        config = self._write_config(resource)
        self._remove_runtime_socket(resource)
        log_handle = (Path(resource.datadir).parent / "launcher.log").open("ab")
        command = self._runtime_command([
            self.mysqld_binary,
            f"--defaults-file={config}",
            *self.mysqld_extra_args,
        ])
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._processes[key] = process
            self._log_handles[key] = log_handle
            started = replace(resource, process_id=process.pid)
            self._wait_ready(started)
            return started
        except Exception:
            process = self._processes.get(key)
            if process is not None and process.poll() is None:
                self._signal_server(resource, "TERM")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_server(resource, "KILL")
                    process.wait(timeout=5)
            self._remove_runtime_socket(resource)
            log_handle.close()
            self._processes.pop(key, None)
            self._log_handles.pop(key, None)
            raise

    def _stop(self, resource: MySQLBranchResource) -> MySQLBranchResource:
        key = (resource.environment_id, resource.branch_id)
        process = self._processes.get(key)
        if process is None:
            return replace(resource, process_id=None)
        if process.poll() is None:
            command = self._runtime_command([
                self.mysqladmin_binary,
                "--protocol=tcp",
                f"--host={self.admin_host}",
                f"--port={resource.port}",
                f"--user={self.mysql_user}",
            ])
            if self.mysql_password:
                command.append(f"--password={self.mysql_password}")
            command.append("shutdown")
            shutdown = subprocess.run(
                command, capture_output=True, timeout=10, check=False
            )
            try:
                process.wait(
                    timeout=self.shutdown_timeout
                    if shutdown.returncode == 0
                    else 0.5
                )
            except subprocess.TimeoutExpired:
                # Killing the local `docker exec` client does not terminate
                # mysqld inside the shared binary namespace. Signal the pid
                # written by mysqld itself instead.
                self._signal_server(resource, "TERM")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_server(resource, "KILL")
                    process.wait(timeout=5)
        self._processes.pop(key, None)
        handle = self._log_handles.pop(key, None)
        if handle is not None:
            handle.close()
        self._remove_runtime_socket(resource)
        return replace(resource, process_id=None)

    def prepare(
        self, environment_id: str, branch_id: str = "root"
    ) -> MySQLBranchResource:
        target = self._datadir(self._branch_root(environment_id, branch_id))
        if not target.is_dir():
            self._clone_tree(self.template_datadir, target)
        resource = MySQLBranchResource(
            environment_id=_safe_token(environment_id),
            branch_id=_safe_token(branch_id),
            datadir=str(target),
            host=self.advertised_host,
            port=self._allocate_port(environment_id, branch_id),
            database_name=self.mysql_database,
        )
        try:
            return self._start(resource)
        except Exception:
            self._release_port(environment_id, branch_id)
            self._safe_remove(self._branch_root(environment_id, branch_id))
            raise

    def checkpoint(
        self, resource: MySQLBranchResource, checkpoint_id: str
    ) -> MySQLBranchResource:
        stopped = self._stop(resource)
        target = self._datadir(
            self._checkpoint_root(resource.environment_id, checkpoint_id)
        )
        try:
            self._clone_tree(Path(resource.datadir), target)
        finally:
            resource = self._start(stopped)
        return resource

    def fork(
        self,
        resource: MySQLBranchResource,
        checkpoint_id: str,
        target_branch: str,
    ) -> MySQLBranchResource:
        self._stop(resource)
        source = self._datadir(
            self._checkpoint_root(resource.environment_id, checkpoint_id)
        )
        target = self._datadir(
            self._branch_root(resource.environment_id, target_branch)
        )
        self._clone_tree(source, target)
        self._release_port(resource.environment_id, resource.branch_id)
        child = MySQLBranchResource(
            environment_id=resource.environment_id,
            branch_id=_safe_token(target_branch),
            datadir=str(target),
            host=self.advertised_host,
            port=self._allocate_port(resource.environment_id, target_branch),
            database_name=resource.database_name,
        )
        try:
            return self._start(child)
        except Exception:
            # The route remains frozen. Keep the old branch cold rather than
            # serving a potentially inconsistent parent after a failed fork.
            self.logger.exception("failed to activate MySQL child branch")
            raise

    def restore(
        self,
        resource: MySQLBranchResource,
        checkpoint_id: str,
    ) -> MySQLBranchResource:
        """Replace the active branch with a cold checkpoint and restart it."""
        stopped = self._stop(resource)
        source = self._datadir(
            self._checkpoint_root(resource.environment_id, checkpoint_id)
        )
        target_root = self._branch_root(
            resource.environment_id, resource.branch_id
        )
        target = self._datadir(target_root)
        self._safe_remove(target_root)
        self._clone_tree(source, target)
        try:
            return self._start(stopped)
        except Exception:
            self.logger.exception("failed to reactivate restored MySQL branch")
            raise

    def reset(self, resource: MySQLBranchResource) -> MySQLBranchResource:
        self._stop(resource)
        environment_root = self._environment_root(resource.environment_id)
        self._safe_remove(environment_root)
        self._release_all_ports(resource.environment_id)
        return self.prepare(resource.environment_id, "root")

    def _release_all_ports(self, environment_id: str) -> None:
        prefix = f"{_safe_token(environment_id)}:%"
        with sqlite3.connect(self._port_registry_path, timeout=30) as connection:
            connection.execute(
                "DELETE FROM allocations WHERE resource_key LIKE ?", (prefix,)
            )

    def cleanup(self, resource: MySQLBranchResource) -> None:
        environment = resource.environment_id
        for (candidate_environment, branch_id), _process in list(
            self._processes.items()
        ):
            if candidate_environment != environment:
                continue
            branch_resource = MySQLBranchResource(
                environment_id=environment,
                branch_id=branch_id,
                datadir=str(
                    self._datadir(self._branch_root(environment, branch_id))
                ),
                host=self.advertised_host,
                port=self._allocate_port(environment, branch_id),
                database_name=self.mysql_database,
            )
            self._stop(branch_resource)
        self._release_all_ports(environment)
        self._safe_remove(self._environment_root(environment))

    def describe(
        self, resource: MySQLBranchResource
    ) -> dict[str, str | int | None]:
        return {
            "engine": "mysql",
            "host": resource.host,
            "port": resource.port,
            "database": resource.database_name,
            "datadir": resource.datadir,
            "process_id": resource.process_id,
        }
