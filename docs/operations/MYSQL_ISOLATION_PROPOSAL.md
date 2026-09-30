# MySQL Isolation Strategy for CUA-Sandbox

> This proposal has been narrowed into the current implementation: shared
> Magento/PHP, XFS-reflink branch trees, and one lightweight host `mysqld` for
> the active branch. The route adapter and Shopping state audit remain required
> before this path is admitted; see `MYSQL_XFS_CONFIG_EXAMPLE.yaml`.

## Problem
Magento (MySQL) lacks PostgreSQL 18's CoW clone capability (~200ms for 6GB).
Traditional `mysqldump` takes 30-60s, blocking parallel inference evaluation.

## Proposed Solution: Filesystem Snapshot + Schema-per-Agent

### Architecture

```
PostgreSQL sites (GitLab, Reddit):
  Agent 1 → DB agent_1_db (PG18 CoW clone, ~200ms)
  Agent 2 → DB agent_2_db
  
MySQL sites (Shopping, CMS):
  Agent 1 → /mysql_data/agent_1/ (ZFS clone, ~500ms)
  Agent 2 → /mysql_data/agent_2/
  ↓
  Shared MySQL instance, schema-per-agent routing
```

### Implementation Plan

#### Phase 1: Filesystem Snapshot Backend

```python
# rl_web_agent/isolation/mysql_isolation.py

class MySQLFilesystemCloner:
    """Clone MySQL data directories using filesystem CoW."""
    
    def __init__(self, fs_type: str, base_path: str):
        self.fs_type = fs_type  # "zfs" or "btrfs"
        self.base_path = base_path
        self.template_snapshot = f"{base_path}/template@base"
    
    def clone_for_agent(self, agent_id: str) -> str:
        """Clone MySQL datadir using filesystem snapshot."""
        if self.fs_type == "zfs":
            clone_path = f"{self.base_path}/agent_{agent_id}"
            subprocess.run([
                "zfs", "clone",
                self.template_snapshot,
                clone_path
            ], check=True, timeout=5)
            return clone_path
        
        elif self.fs_type == "btrfs":
            source = f"{self.base_path}/template"
            target = f"{self.base_path}/agent_{agent_id}"
            subprocess.run([
                "btrfs", "subvolume", "snapshot",
                source, target
            ], check=True, timeout=5)
            return target
    
    def cleanup(self, agent_id: str):
        """Remove agent's MySQL datadir."""
        if self.fs_type == "zfs":
            clone_path = f"{self.base_path}/agent_{agent_id}"
            subprocess.run(["zfs", "destroy", clone_path], check=True)
        elif self.fs_type == "btrfs":
            target = f"{self.base_path}/agent_{agent_id}"
            subprocess.run(["btrfs", "subvolume", "delete", target], check=True)
```

#### Phase 2: Extend DBIsolationManager

```python
# Modify rl_web_agent/isolation/db_isolation.py

class DBIsolationManager:
    def __init__(self, config: DictConfig, logger):
        # Existing PostgreSQL setup
        self.admin_dsn = ...
        
        # NEW: MySQL support
        self.mysql_enabled = bool(config.get("mysql_isolation", {}).get("enabled", False))
        if self.mysql_enabled:
            mysql_config = config.mysql_isolation
            self.mysql_cloner = MySQLFilesystemCloner(
                fs_type=str(mysql_config.fs_type),
                base_path=str(mysql_config.base_path)
            )
            self.mysql_connection_string = str(mysql_config.connection_string)
    
    def prepare_for_task(self, env_uuid: str, task_config: dict) -> DBAgentSession:
        site = self._extract_site(task_config)
        
        if self._is_mysql_site(site):
            return self._prepare_mysql_agent(env_uuid, task_config, site)
        else:
            return self._prepare_postgresql_agent(env_uuid, task_config, site)
    
    def _prepare_mysql_agent(self, env_uuid: str, task_config: dict, site: str):
        agent_id = self.build_agent_id(env_uuid, task_config)
        
        # Clone filesystem datadir
        datadir = self.mysql_cloner.clone_for_agent(agent_id)
        
        # Start MySQL instance pointing to this datadir (or use schema routing)
        db_name = f"magento_agent_{agent_id}"
        
        # Register route
        route = self.route_registry.set_route(
            agent_id, db_name,
            environment_id=agent_id,
            site=site,
            branch_id="root",
            generation=1,
            lifecycle_state=FROZEN
        )
        
        self.route_registry.activate(agent_id)
        headers = {self.route_token_header: self.route_token_signer.issue(agent_id)}
        
        return DBAgentSession(
            agent_id=agent_id,
            db_name=db_name,
            site=site,
            headers=headers,
            shared_site_hosts=dict(self.shared_site_hosts)
        )
```

#### Phase 3: Configuration

```yaml
# config.yaml addition
environment:
  db_isolation:
    # Existing PostgreSQL config
    admin_dsn: "postgresql://..."
    
    # NEW: MySQL isolation config
    mysql_isolation:
      enabled: true
      fs_type: "zfs"  # or "btrfs"
      base_path: "tank/mysql/webarena"
      connection_string: "mysql://root:pass@localhost"
      sites: ["shopping", "shopping_admin"]  # MySQL sites
```

### Performance Expectations

| Database | Strategy | Clone Time | Storage Overhead |
|----------|----------|------------|------------------|
| PostgreSQL (GitLab, Reddit) | PG18 FILE_COPY | ~200ms | Near-zero (reflink) |
| MySQL (Shopping, CMS) | ZFS snapshot | ~500ms | Near-zero (CoW) |
| Baseline (full-container baseline container) | Full container | ~1.78s | 28 MiB (Btrfs CoW) |

### Alternative: Schema-per-Agent (No Filesystem Dependency)

If filesystem snapshots are not feasible:

```python
class MySQLSchemaCloner:
    """Clone MySQL databases using CREATE DATABASE ... LIKE."""
    
    def clone_db(self, source_db: str, target_db: str):
        # 1. Create empty target database
        self._execute(f"CREATE DATABASE {target_db}")
        
        # 2. Clone schema structure
        tables = self._execute(f"SHOW TABLES FROM {source_db}", fetch=True)
        for table in tables:
            self._execute(
                f"CREATE TABLE {target_db}.{table} "
                f"LIKE {source_db}.{table}"
            )
        
        # 3. Clone data (this is the slow part: ~30-60s for 6GB)
        for table in tables:
            self._execute(
                f"INSERT INTO {target_db}.{table} "
                f"SELECT * FROM {source_db}.{table}"
            )
```

**Performance**: ~30-60 seconds (not ideal, but functional).

### Recommendation

**Use ZFS/Btrfs filesystem snapshots for MVP**, with fallback to schema cloning:

1. Check if ZFS/Btrfs is available at startup
2. If yes: use filesystem snapshots (~500ms)
3. If no: fall back to schema cloning (~30s) with a warning

This maintains architectural consistency while achieving acceptable performance.

## Next Steps

1. [ ] Implement `MySQLFilesystemCloner` class
2. [ ] Extend `DBIsolationManager` with MySQL branch
3. [ ] Add MySQL-specific routing to `route_registry`
4. [ ] Test on Shopping/CMS sites
5. [ ] Benchmark clone performance
6. [ ] Verify reward equivalence vs full-container baseline baseline
