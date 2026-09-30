# Reward Consistency Validation Guide

## Overview

This guide explains how to validate that the DB isolation mode (PostgreSQL CoW + MySQL XFS reflink) produces **exactly the same rewards** as the full-container baseline container baseline.

**Why this matters:**
- inference evaluation depends on accurate reward signals
- Any reward inconsistency corrupts the evaluation process
- We need 100% confidence that multi-tenant execution is equivalent

## Validation Strategy

### Three-Layer Validation

```
Layer 1: Unit Tests (Quick)
  └─ Test individual isolation primitives
  
Layer 2: Functional Tests (Medium)
  └─ Run small task sets, compare rewards
  
Layer 3: Statistical Tests (Comprehensive)
  └─ Run large task sets, verify distribution
```

## Quick Start

### 1. Small Validation (3 tasks, ~5 minutes)

```bash
cd /path/to/CUA-Sandbox

python scripts/validate_reward_consistency.py \
  --test-suite small \
  --site gitlab \
  --output-dir validation_results/small
```

### 2. Medium Validation (10 tasks, ~20 minutes)

```bash
python scripts/validate_reward_consistency.py \
  --test-suite medium \
  --site gitlab \
  --parallel 2 \
  --output-dir validation_results/medium
```

### 3. Full Validation (25 tasks, ~1 hour)

```bash
python scripts/validate_reward_consistency.py \
  --test-suite full \
  --site gitlab \
  --parallel 4 \
  --output-dir validation_results/full
```

### 4. Custom Task Set

```bash
python scripts/validate_reward_consistency.py \
  --tasks 1,5,10,15,20 \
  --site gitlab \
  --output-dir validation_results/custom
```

### 5. Multiple Sites

```bash
# GitLab (PostgreSQL)
python scripts/validate_reward_consistency.py \
  --test-suite small --site gitlab

# Shopping (MySQL)
python scripts/validate_reward_consistency.py \
  --test-suite small --site shopping

# Reddit (PostgreSQL)
python scripts/validate_reward_consistency.py \
  --test-suite small --site reddit
```

## What Gets Validated

### Primary Metrics

1. **Reward Score** (CRITICAL)
   - Must match to 6 decimal places
   - Any difference > 1e-6 is a failure

2. **Success Flag** (CRITICAL)
   - Boolean: task succeeded or failed
   - Must match exactly

3. **Evaluation Result** (IMPORTANT)
   - Detailed eval output (string match, etc.)
   - Should match exactly

### Secondary Metrics

4. **Execution Time** (INFO ONLY)
   - Not used for pass/fail
   - Useful for performance analysis

5. **Final State** (OPTIONAL)
   - May have timing differences
   - Not strictly validated

## Expected Output

### Success Case

```
================================================================================
REWARD CONSISTENCY VALIDATION REPORT
================================================================================

Total Tasks Tested: 3
Reward Matches: 3/3 (100.0%)
Success Matches: 3/3 (100.0%)
Eval Matches: 3/3 (100.0%)

✅ PASSED: All rewards and success flags match exactly!

--------------------------------------------------------------------------------
PERFORMANCE COMPARISON
--------------------------------------------------------------------------------

Average full-container baseline execution time: 15.23s
Average DB execution time: 14.87s
Speedup: 1.02x

--------------------------------------------------------------------------------
DETAILED RESULTS
--------------------------------------------------------------------------------

   task_id    site  reward_match  success_match  reward_diff  container_baseline_reward  db_reward  ...
0        1  gitlab          True           True         0.00          1.00       1.00  ...
1        2  gitlab          True           True         0.00          0.75       0.75  ...
2        3  gitlab          True           True         0.00          0.00       0.00  ...
```

### Failure Case

```
================================================================================
REWARD CONSISTENCY VALIDATION REPORT
================================================================================

Total Tasks Tested: 3
Reward Matches: 2/3 (66.7%)
Success Matches: 2/3 (66.7%)
Eval Matches: 2/3 (66.7%)

❌ FAILED: Discrepancies detected!

--------------------------------------------------------------------------------
DISCREPANCIES DETAILS
--------------------------------------------------------------------------------

Task 2 (gitlab):
  ❌ Reward mismatch:
     full-container baseline:  0.7500
     DB:     0.5000
     Diff:   0.2500
  ❌ Success mismatch:
     full-container baseline:  True
     DB:     False
  Errors:
     DB:     Timeout during evaluation
```

## Common Discrepancy Causes

### 1. Timing-Related Issues

**Symptom:** Random failures, inconsistent across runs

**Causes:**
- Race conditions in async code
- Insufficient wait times for page loads
- Background jobs not drained before evaluation

**Fix:**
```python
# Ensure background jobs are drained
db_isolation_manager.wait_for_background_jobs(session)

# Increase timeouts if needed
environment.browser.timeouts.page_load_networkidle: 30000
```

### 2. State Leakage Between Agents

**Symptom:** Later tasks fail, early tasks succeed

**Causes:**
- Shared Redis cache not properly namespaced
- Shared file uploads directory
- Database connections not isolated

**Fix:**
```python
# Verify route isolation
route = route_registry.resolve_route(agent_id)
print(route.state_namespace)  # Should be unique per agent
```

### 3. Non-Deterministic Evaluation

**Symptom:** Same task, different rewards

**Causes:**
- Evaluation depends on timestamps
- Evaluation depends on database auto-increment IDs
- Evaluation depends on random elements

**Fix:**
- Make evaluation logic deterministic
- Use stable sorting when order matters
- Mock time-dependent functions during eval

### 4. Database Migration Issues

**Symptom:** DB mode fails, full-container baseline succeeds

**Causes:**
- Template database schema out of sync
- Missing migrations in template
- Incorrect database version

**Fix:**
```bash
# Rebuild template from latest dump
docker exec container_baseline-gitlab pg_dump > latest_dump.sql
# Recreate template with latest dump
```

### 5. MySQL-Specific Issues

**Symptom:** Shopping tasks fail in DB mode

**Causes:**
- MySQL template not properly initialized
- InnoDB buffer pool issues
- Character encoding mismatches

**Fix:**
```bash
# Verify MySQL template
docker run --rm \
  -v ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template:/var/lib/mysql \
  mysql:8.0

# Check for errors
docker logs <container>
```

## Debugging Workflow

### Step 1: Isolate the Failure

```bash
# Run single failing task
python scripts/validate_reward_consistency.py \
  --tasks 5 \
  --site gitlab \
  --output-dir debug/task_5
```

### Step 2: Compare Execution Traces

```bash
# full-container baseline trace
cat debug/task_5/container_baseline_mode/task_5/trace.log

# DB mode trace
cat debug/task_5/db_mode/task_5/trace.log

# Diff them
diff debug/task_5/container_baseline_mode/task_5/trace.log \
     debug/task_5/db_mode/task_5/trace.log
```

### Step 3: Check Database State

```python
# In DB mode, before evaluation
import psycopg
conn = psycopg.connect("postgresql://...")
cursor = conn.cursor()

# Check table counts
cursor.execute("SELECT COUNT(*) FROM issues;")
print(f"Issues: {cursor.fetchone()[0]}")

cursor.execute("SELECT COUNT(*) FROM merge_requests;")
print(f"MRs: {cursor.fetchone()[0]}")
```

### Step 4: Verify Isolation

```python
# Check that agent's actions only affect their DB
route = route_registry.resolve_route(agent_id)
print(f"Agent DB: {route.db_name}")
print(f"State namespace: {route.state_namespace}")

# Verify no cross-contamination
other_agent_db = route_registry.resolve("other_agent_id")
print(f"Other agent DB: {other_agent_db}")  # Should be different
```

## Advanced Validation

### Statistical Validation

For large-scale validation, check reward distribution:

```python
import pandas as pd
import scipy.stats as stats

df = pd.read_csv("validation_results/full/reward_consistency_data.csv")

# Extract rewards
container_baseline_rewards = df["container_baseline_reward"].values
db_rewards = df["db_reward"].values

# Statistical tests
print(f"Mean difference: {(container_baseline_rewards - db_rewards).mean()}")
print(f"Max difference: {abs(container_baseline_rewards - db_rewards).max()}")

# Paired t-test (should show no significant difference)
t_stat, p_value = stats.ttest_rel(container_baseline_rewards, db_rewards)
print(f"T-test p-value: {p_value}")  # Should be > 0.05

# Correlation (should be 1.0)
correlation = df["container_baseline_reward"].corr(df["db_reward"])
print(f"Correlation: {correlation}")  # Should be 1.0
```

### Stress Testing

Run the same task multiple times to detect non-determinism:

```bash
# Run task 5 ten times in DB mode
for i in {1..10}; do
  python -m rl_web_agent.entrypoints.batch_agent \
    --task_ids 5 \
    --sites gitlab \
    --output_dir validation_results/stability/run_$i \
    environment.isolation.mode=db
done

# Compare all rewards
grep "reward" validation_results/stability/*/task_5_result.json
# All should be identical
```

## Validation Checklist

### Pre-Validation Setup

- [ ] full-container baseline server is running and healthy
- [ ] DB isolation is properly configured
- [ ] PostgreSQL 18 template exists and is up-to-date
- [ ] MySQL template exists (if testing shopping)
- [ ] XFS reflink is enabled and working
- [ ] No other agents are running (clean slate)

### During Validation

- [ ] Tasks run successfully in both modes
- [ ] No errors in logs
- [ ] Execution times are reasonable
- [ ] Resource usage is normal

### Post-Validation Analysis

- [ ] Reward match rate is 100%
- [ ] Success match rate is 100%
- [ ] Eval match rate is 100%
- [ ] No systematic bias in discrepancies
- [ ] Performance is acceptable

## Continuous Validation

### Integration into CI/CD

```yaml
# .github/workflows/reward_consistency.yml
name: Reward Consistency Check

on:
  pull_request:
    paths:
      - 'rl_web_agent/isolation/**'
      - 'rl_web_agent/env.py'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      
      - name: Setup environment
        run: |
          # Setup full-container baseline, PostgreSQL, etc.
      
      - name: Run validation
        run: |
          python scripts/validate_reward_consistency.py \
            --test-suite small \
            --site gitlab
      
      - name: Upload results
        uses: actions/upload-artifact@v2
        with:
          name: validation-results
          path: validation_results/
```

### Regular Validation Schedule

```bash
# Weekly full validation
0 2 * * 0 cd /path/to/CUA-Sandbox && \
  python scripts/validate_reward_consistency.py \
    --test-suite full \
    --site gitlab \
    --parallel 4 \
    --output-dir /var/log/cua-sandbox/validation/$(date +\%Y\%m\%d)
```

## Expected Results by Site

### GitLab (PostgreSQL)

- **Clone speed**: ~200ms
- **Expected pass rate**: 100%
- **Known issues**: None (baseline)

### Reddit (PostgreSQL)

- **Clone speed**: ~200ms
- **Expected pass rate**: 100%
- **Known issues**: None

### Shopping (MySQL)

- **Clone speed**: ~500ms
- **Expected pass rate**: 100% (after template setup)
- **Known issues**: 
  - Requires MySQL template
  - First run may be slower (container pull)

### Shopping Admin (MySQL)

- **Clone speed**: ~500ms
- **Expected pass rate**: 100%
- **Known issues**: Same as Shopping

## Troubleshooting Guide

| Issue | Symptom | Solution |
|-------|---------|----------|
| **Timeout** | Tasks timeout in DB mode | Increase `route_drain_timeout_seconds` |
| **Connection refused** | Can't connect to DB | Check PostgreSQL container is running |
| **Permission denied** | Docker errors | Check user is in `docker` group |
| **Port conflict** | MySQL won't start | Adjust `mysql_port_base` |
| **Out of memory** | System crashes | Reduce `--parallel` count |
| **Disk full** | Clone fails | Clean up old datadirs |

## Success Criteria

### Minimum Requirements (MVP)

- ✅ 95% reward match rate on 10-task sample
- ✅ No systematic bias
- ✅ All critical tasks pass

### Production Requirements

- ✅ 100% reward match rate on full task set
- ✅ 100% success match rate
- ✅ Statistical tests show no difference
- ✅ Validated across all sites (GitLab, Reddit, Shopping)

## Next Steps After Validation

1. **Document Results**: Add validation report to project docs
2. **Benchmark Performance**: Compare speed and resource usage
3. **Scale Testing**: Test with 100+ concurrent agents
4. **Production Deployment**: Roll out DB isolation mode
5. **Continuous Monitoring**: Set up ongoing validation

## Contact

For validation failures or questions:
- Check logs in `validation_results/`
- Review `MYSQL_INTEGRATION_SUMMARY.md`
- Run debug workflow above

---

**Remember:** Reward consistency is critical. Don't skip validation!
