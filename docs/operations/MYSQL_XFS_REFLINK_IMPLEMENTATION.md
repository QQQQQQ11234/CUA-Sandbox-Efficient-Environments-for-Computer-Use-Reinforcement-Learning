# MySQL Isolation using XFS Reflink

> Status note: the original draft of this document described per-agent Docker
> MySQL containers and is superseded by the current implementation. The
> authoritative design is in [MYSQL_XFS_CONFIG_EXAMPLE.yaml](MYSQL_XFS_CONFIG_EXAMPLE.yaml):
> shared Magento/PHP, one lightweight host `mysqld` for the active branch, and
> cold checkpoint/fork trees made with XFS reflink. Do not use the old
> `docker run` snippets below as the runtime path.

## Current Setup

You already have XFS with reflink support:
```
Mount: ${WEBAGENT_ROOT:-/var/lib/web-agent}
Device: /dev/loop8
Filesystem: XFS (reflink=1)
PostgreSQL: ${WEBAGENT_ROOT:-/var/lib/web-agent}/pg18_clone_xfs/data (already using CoW)
```

## Solution: Extend XFS Reflink to MySQL

Instead of ZFS snapshots or LVM, use **XFS reflink** for MySQL data directories.
This is the simplest solution because you already have the infrastructure.

### Architecture

```
PostgreSQL sites (GitLab, Reddit):
  ${WEBAGENT_ROOT:-/var/lib/web-agent}/pg18_clone_xfs/data/
    ├── base_template_db        (template)
    ├── agent_123_db           (PG18 CoW clone, ~200ms)
    └── agent_456_db

MySQL sites (Shopping, CMS):
  ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/
    ├── magento_template/      (template datadir)
    │   ├── ibdata1
    │   ├── magento/           (database schema)
    │   └── mysql/
    ├── agent_123/             (XFS reflink clone, ~500ms)
    │   ├── ibdata1           (CoW linked to template)
    │   ├── magento/
    │   └── mysql/
    └── agent_456/
```

### Implementation

#### 1. Setup MySQL Template on XFS

```bash
# Create MySQL data directory on XFS partition
mkdir -p ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template

# Initialize MySQL datadir
docker run --rm -v ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template:/var/lib/mysql \
  mysql:8.0 --initialize-insecure

# Start MySQL and load Magento schema
docker run -d --name mysql-template \
  -v ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template:/var/lib/mysql \
  -e MYSQL_ROOT_PASSWORD=password \
  mysql:8.0

# Load Magento database
docker exec mysql-template mysql -uroot -ppassword < magento_schema.sql

# Stop and remove container
docker stop mysql-template && docker rm mysql-template
```

#### 2. Extend DBIsolationManager

```python
# rl_web_agent/isolation/mysql_xfs_isolation.py

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

class MySQLXFSReflinkManager:
    """Clone MySQL data directories using XFS reflink."""
    
    def __init__(
        self,
        xfs_base_path: str,
        template_name: str = "magento_template",
        mysql_port_base: int = 13306,
    ):
        self.xfs_base_path = Path(xfs_base_path)
        self.template_path = self.xfs_base_path / template_name
        self.mysql_port_base = mysql_port_base
        
        if not self.template_path.exists():
            raise ValueError(f"MySQL template not found: {self.template_path}")
        
        # Verify we're on XFS with reflink
        self._verify_xfs_reflink()
    
    def _verify_xfs_reflink(self):
        """Verify the path is on XFS filesystem with reflink support."""
        result = subprocess.run(
            ["stat", "-f", "-c", "%T", str(self.xfs_base_path)],
            capture_output=True,
            text=True,
        )
        fs_type = result.stdout.strip()
        if fs_type != "xfs":
            raise RuntimeError(
                f"MySQL base path is not on XFS: {self.xfs_base_path} ({fs_type})"
            )
    
    def clone_for_agent(self, agent_id: str) -> tuple[str, int]:
        """
        Clone MySQL datadir for an agent using XFS reflink.
        
        Returns:
            (datadir_path, mysql_port)
        """
        agent_path = self.xfs_base_path / f"agent_{agent_id}"
        
        if agent_path.exists():
            # Clean up existing
            shutil.rmtree(agent_path)
        
        # Clone using reflink (CoW, almost instant)
        try:
            subprocess.run(
                ["cp", "--reflink=always", "-r", str(self.template_path), str(agent_path)],
                check=True,
                timeout=10,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"XFS reflink clone failed: {e.stderr.decode()}"
            ) from e
        
        # Assign a unique port for this agent's MySQL instance
        port = self._allocate_port(agent_id)
        
        return str(agent_path), port
    
    def _allocate_port(self, agent_id: str) -> int:
        """Allocate a unique MySQL port for this agent."""
        # Simple hash-based allocation
        agent_hash = hash(agent_id) % 10000
        return self.mysql_port_base + agent_hash
    
    def start_mysql_instance(
        self,
        agent_id: str,
        datadir: str,
        port: int,
    ) -> str:
        """
        Start a MySQL container for this agent.
        
        Returns:
            container_name
        """
        container_name = f"mysql-agent-{agent_id}"
        
        # Remove if exists
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
        )
        
        # Start MySQL with agent's datadir
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                "-v", f"{datadir}:/var/lib/mysql",
                "-e", "MYSQL_ROOT_PASSWORD=agent_password",
                "-p", f"{port}:3306",
                "mysql:8.0",
            ],
            check=True,
        )
        
        return container_name
    
    def cleanup(self, agent_id: str):
        """Remove agent's MySQL datadir and container."""
        # Stop container
        container_name = f"mysql-agent-{agent_id}"
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
        )
        
        # Remove datadir
        agent_path = self.xfs_base_path / f"agent_{agent_id}"
        if agent_path.exists():
            shutil.rmtree(agent_path)
```

#### 3. Integrate with DBIsolationManager

```python
# Modify rl_web_agent/isolation/db_isolation.py

class DBIsolationManager:
    def __init__(self, config: DictConfig, logger):
        # Existing PostgreSQL setup
        self.admin_dsn = ...
        
        # NEW: MySQL XFS reflink support
        self.mysql_enabled = bool(config.get("mysql_xfs", {}).get("enabled", False))
        if self.mysql_enabled:
            from .mysql_xfs_isolation import MySQLXFSReflinkManager
            mysql_config = config.mysql_xfs
            self.mysql_manager = MySQLXFSReflinkManager(
                xfs_base_path=str(mysql_config.base_path),
                template_name=str(mysql_config.template_name),
            )
        else:
            self.mysql_manager = None
    
    def prepare_for_task(self, env_uuid: str, task_config: dict) -> DBAgentSession:
        site = self._extract_site(task_config)
        
        if self._is_mysql_site(site) and self.mysql_manager:
            return self._prepare_mysql_agent(env_uuid, task_config, site)
        else:
            return self._prepare_postgresql_agent(env_uuid, task_config, site)
    
    def _is_mysql_site(self, site: str) -> bool:
        """Check if site uses MySQL."""
        return site in ["shopping", "shopping_admin"]
    
    def _prepare_mysql_agent(
        self,
        env_uuid: str,
        task_config: dict,
        site: str,
    ) -> DBAgentSession:
        """Prepare MySQL environment using XFS reflink."""
        agent_id = self.build_agent_id(env_uuid, task_config)
        
        # Clone MySQL datadir using XFS reflink (~500ms)
        datadir, port = self.mysql_manager.clone_for_agent(agent_id)
        
        # Start MySQL container
        container_name = self.mysql_manager.start_mysql_instance(
            agent_id, datadir, port
        )
        
        # Register route (same as PostgreSQL)
        route = self.route_registry.set_route(
            agent_id,
            f"mysql_{agent_id}",
            environment_id=agent_id,
            site=site,
            branch_id="root",
            generation=1,
            lifecycle_state=FROZEN,
        )
        
        self.route_registry.activate(agent_id)
        
        headers = {self.route_token_header: self.route_token_signer.issue(agent_id)}
        
        # Point to MySQL container
        shared_site_hosts = dict(self.shared_site_hosts)
        shared_site_hosts[site] = f"127.0.0.1:{port}"
        
        return DBAgentSession(
            agent_id=agent_id,
            db_name=f"mysql_{agent_id}",
            site=site,
            headers=headers,
            shared_site_hosts=shared_site_hosts,
        )
```

#### 4. Configuration

```yaml
# config.yaml
environment:
  isolation:
    mode: db
  
  db_isolation:
    # Existing PostgreSQL config
    admin_dsn: postgresql://...
    clone_strategy: FILE_COPY
    
    # NEW: MySQL XFS reflink config
    mysql_xfs:
      enabled: true
      base_path: ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs
      template_name: magento_template
      mysql_port_base: 13306
```

### Performance Expectations

| Site | Database | Clone Method | Time | Storage |
|------|----------|--------------|------|---------|
| GitLab, Reddit | PostgreSQL 18 | PG18 FILE_COPY (XFS reflink) | ~200ms | Near-zero (CoW) |
| Shopping, CMS | MySQL 8.0 | XFS reflink datadir clone | ~500ms | Near-zero (CoW) |
| Baseline (full-container baseline) | Mixed | Full container | ~1.78s | 28 MiB |

### Testing

```bash
# 1. Verify XFS reflink works
cd ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs
time cp --reflink=always -r magento_template test_clone
# Should complete in < 1 second

# 2. Check storage usage
du -sh magento_template test_clone
df -h ${WEBAGENT_ROOT:-/var/lib/web-agent}
# test_clone should show similar size but df shows minimal space used

# 3. Clean up
rm -rf test_clone
```

### Advantages

1. ✅ **Unified Architecture**: Both PostgreSQL and MySQL use CoW cloning
2. ✅ **Fast**: ~500ms for MySQL (vs 200ms for PG, 1.78s for containers)
3. ✅ **Storage Efficient**: True CoW, minimal overhead
4. ✅ **No New Dependencies**: Uses existing XFS partition
5. ✅ **Simple**: No ZFS or LVM complexity

### Comparison with Other Solutions

| Solution | Clone Time | Storage | Complexity | Requires |
|----------|-----------|---------|------------|----------|
| **XFS Reflink** ⭐⭐⭐⭐⭐ | ~500ms | CoW (minimal) | Low | Your existing XFS |
| ZFS Snapshots | ~100ms | CoW (minimal) | Medium | ZFS setup |
| LVM Snapshots | ~200ms | CoW (minimal) | Medium | LVM setup |
| Schema Clone | 30-60s | High | Low | Nothing |
| MariaDB CLONE | ~5s | High | Medium | MariaDB |

### Next Steps

1. [ ] Create MySQL template on XFS partition
2. [ ] Implement `MySQLXFSReflinkManager` class
3. [ ] Extend `DBIsolationManager` with MySQL branch
4. [ ] Test reflink clone speed
5. [ ] Verify Magento works with cloned datadirs
6. [ ] Benchmark full inference evaluation workflow
