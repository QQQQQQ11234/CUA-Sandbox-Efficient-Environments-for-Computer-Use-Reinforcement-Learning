# MySQL XFS Reflink Integration - Checklist

## Pre-Integration Verification ✅

- [x] XFS partition with reflink support exists at `${WEBAGENT_ROOT:-/var/lib/web-agent}`
- [x] XFS reflink is enabled (`reflink=1`)
- [x] PostgreSQL 18 is already using CoW cloning
- [x] Project structure reviewed and understood

## Implementation Completed ✅

### Code Files
- [x] `rl_web_agent/isolation/mysql_xfs_isolation.py` - MySQL manager implementation
- [x] `rl_web_agent/isolation/db_isolation.py` - Extended for MySQL support
- [x] `rl_web_agent/isolation/__init__.py` - Updated exports

### Documentation
- [x] `MYSQL_QUICKSTART.md` - Quick start guide
- [x] `MYSQL_INTEGRATION_SUMMARY.md` - Complete integration overview
- [x] `MYSQL_XFS_CONFIG_EXAMPLE.yaml` - Configuration reference
- [x] `MYSQL_XFS_REFLINK_IMPLEMENTATION.md` - Technical implementation guide
- [x] `MYSQL_ISOLATION_PROPOSAL.md` - Research and comparison

### Scripts
- [x] `scripts/setup_mysql_template.sh` - Automated template setup
- [x] `scripts/test_mysql_xfs_isolation.py` - Validation test suite
- [x] `scripts/check_cow_capability.py` - CoW capability checker

## Setup Tasks (To Do)

### Step 1: Prepare MySQL Template
- [ ] Run setup script: `./scripts/setup_mysql_template.sh`
- [ ] Or manually:
  - [ ] Create template directory
  - [ ] Initialize MySQL datadir
  - [ ] Load Magento schema/data
  - [ ] Verify template integrity

### Step 2: Validation
- [ ] Run test suite: `python scripts/test_mysql_xfs_isolation.py`
- [ ] Verify all tests pass:
  - [ ] XFS reflink check
  - [ ] Clone speed test (< 2s)
  - [ ] MySQL container test
  - [ ] Python integration test

### Step 3: Configuration
- [ ] Update config with MySQL settings
- [ ] Set `mysql_xfs.enabled: true`
- [ ] Configure paths and credentials
- [ ] Verify configuration syntax

### Step 4: Integration Testing
- [ ] Test with single shopping task
- [ ] Check logs for clone timing
- [ ] Verify MySQL container starts
- [ ] Confirm agent can interact with site

### Step 5: Performance Validation
- [ ] Benchmark clone speed (target: ~500ms)
- [ ] Test with multiple concurrent agents
- [ ] Monitor disk usage (should stay low with CoW)
- [ ] Compare with full-container baseline baseline

### Step 6: Production Readiness
- [ ] Run full inference evaluation workflow
- [ ] Verify reward equivalence
- [ ] Test checkpoint/fork functionality
- [ ] Document any edge cases

## Quick Reference Commands

### Setup
```bash
cd /path/to/CUA-Sandbox
./scripts/setup_mysql_template.sh
```

### Test
```bash
python scripts/test_mysql_xfs_isolation.py
```

### Run
```bash
python -m rl_web_agent.entrypoints.batch_agent \
  --task_ids 1 --sites shopping \
  environment.isolation.mode=db \
  environment.db_isolation.mysql_xfs.enabled=true
```

### Monitor
```bash
# Watch MySQL containers
watch 'docker ps | grep mysql-agent'

# Check disk usage
watch 'df -h ${WEBAGENT_ROOT:-/var/lib/web-agent}'

# View logs
tail -f results/*/logs/*.log | grep MySQL
```

### Debug
```bash
# Check XFS reflink
xfs_info ${WEBAGENT_ROOT:-/var/lib/web-agent} | grep reflink

# Test manual clone
time cp --reflink=always -r \
  ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template \
  ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/test_clone

# Check container logs
docker logs mysql-agent-task_1_xxx
```

## Success Criteria

### Must Have ✓
- [ ] MySQL template created and validated
- [ ] XFS reflink clone works (< 2s for multi-GB)
- [ ] MySQL containers start successfully
- [ ] Agent can complete shopping tasks
- [ ] No data corruption or isolation leaks

### Should Have ✓
- [ ] Clone time < 1s for typical datadirs
- [ ] Support 10+ concurrent agents
- [ ] Memory usage comparable to PostgreSQL approach
- [ ] Logs show clear timing information

### Nice to Have ✓
- [ ] Checkpoint/fork support for MySQL
- [ ] Automatic cleanup on failure
- [ ] Performance metrics dashboard
- [ ] Reward equivalence validation

## Known Limitations

1. **Container Overhead**: MySQL container startup takes ~5-10s (vs full-container baseline ~1.78s)
   - Trade-off: Better isolation and shared infrastructure

2. **Port Allocation**: Hash-based, supports max 1000 concurrent agents
   - Mitigation: Configurable port base and range

3. **Single Database per Container**: Each agent gets full MySQL instance
   - Alternative: Shared MySQL with schema-per-agent (fallback mode)

## Troubleshooting Guide

| Issue | Check | Solution |
|-------|-------|----------|
| Clone fails | XFS reflink | `xfs_info ${WEBAGENT_ROOT:-/var/lib/web-agent}` |
| Container won't start | Template validity | `docker run --rm -v template:/var/lib/mysql mysql:8.0` |
| Port conflicts | Running containers | Adjust `mysql_port_base` |
| Slow clones | Reflink working | Time manual `cp --reflink` |
| Permission errors | Datadir ownership | `chown -R mysql:mysql datadir` |

## Next Steps After Setup

1. **Benchmark**: Run performance comparison with full-container baseline
2. **Scale Test**: Test with 50+ concurrent agents
3. **Validate**: Verify reward equivalence on full task set
4. **Document**: Update main README with MySQL support info
5. **Optimize**: Profile and optimize if needed

## Contact/Support

For issues or questions:
- Review: `MYSQL_INTEGRATION_SUMMARY.md`
- Test: `python scripts/test_mysql_xfs_isolation.py`
- Debug: Check container logs and XFS status

## Version Info

- Implementation Date: 2026-08-11
- CUA-Sandbox Version: Current main branch
- MySQL Version: 8.0
- XFS Location: `${WEBAGENT_ROOT:-/var/lib/web-agent}`
- Status: ✅ Code complete, pending template setup

---

**Ready to Start?**

Run: `./scripts/setup_mysql_template.sh`
