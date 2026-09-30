# Reward Consistency Validation - Complete Summary

## What Was Created

I've created a comprehensive reward consistency validation system for your CUA-Sandbox project. Here's what you now have:

### 1. Main Validation Script
**`scripts/validate_reward_consistency.py`**
- Automated reward comparison between full-container baseline and DB modes
- Runs same tasks in both modes and compares results
- Generates detailed reports with statistics
- Exit code 0 = pass, 1 = fail (CI/CD ready)

### 2. Quick Validation Script
**`scripts/quick_validation.sh`**
- One-command validation
- Pre-flight checks (full-container baseline, PostgreSQL, MySQL template)
- User-friendly output

### 3. Comprehensive Guide
**`REWARD_CONSISTENCY_VALIDATION.md`**
- Complete validation methodology
- Debugging workflows
- Common issues and solutions
- Statistical validation methods

## How to Use

### Quick Test (3 tasks, ~5 minutes)

```bash
cd /path/to/CUA-Sandbox

# GitLab (PostgreSQL)
./scripts/quick_validation.sh gitlab small

# Shopping (MySQL) - after template setup
./scripts/quick_validation.sh shopping small
```

### Detailed Validation

```bash
# Small suite (3 tasks)
python scripts/validate_reward_consistency.py \
  --test-suite small \
  --site gitlab \
  --output-dir validation_results/small

# Medium suite (10 tasks)
python scripts/validate_reward_consistency.py \
  --test-suite medium \
  --site gitlab \
  --parallel 2 \
  --output-dir validation_results/medium

# Full suite (25 tasks)
python scripts/validate_reward_consistency.py \
  --test-suite full \
  --site gitlab \
  --parallel 4 \
  --output-dir validation_results/full

# Custom tasks
python scripts/validate_reward_consistency.py \
  --tasks 1,5,10,15,20 \
  --site gitlab \
  --output-dir validation_results/custom
```

## What Gets Validated

### Critical Metrics (Must Match 100%)

1. **Reward Score** - Must match to 6 decimal places
2. **Success Flag** - Boolean pass/fail must match
3. **Evaluation Result** - Detailed eval output must match

### Info Metrics (For Analysis)

4. **Execution Time** - Performance comparison
5. **Error Messages** - Debugging information

## Expected Output

### Success Case ✅

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
```

### Failure Case ❌

```
================================================================================
REWARD CONSISTENCY VALIDATION REPORT
================================================================================

Total Tasks Tested: 3
Reward Matches: 2/3 (66.7%)
Success Matches: 2/3 (66.7%)

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

## Validation Strategy

### Three-Layer Approach

```
Layer 1: Quick Check (3 tasks, 5 min)
  └─ Verify basic functionality
  
Layer 2: Medium Check (10 tasks, 20 min)
  └─ Catch common issues
  
Layer 3: Full Check (25+ tasks, 1 hour)
  └─ Statistical confidence
```

### What This Tests

```
Isolation Correctness:
  ✓ Each agent has independent database
  ✓ No state leakage between agents
  ✓ Route registry works correctly
  ✓ Non-DB state is properly isolated

Database Consistency:
  ✓ PostgreSQL CoW cloning is accurate
  ✓ MySQL XFS reflink cloning is accurate
  ✓ Template databases are up-to-date
  ✓ Migrations are complete

Evaluation Correctness:
  ✓ Same actions produce same outcomes
  ✓ Evaluation logic is deterministic
  ✓ Background jobs are properly drained
  ✓ Final state is correctly assessed
```

## Common Issues and Solutions

### Issue 1: Timing Race Conditions

**Symptom:** Random failures, inconsistent results

**Solution:**
```python
# Ensure background jobs complete before evaluation
db_isolation_manager.wait_for_background_jobs(session)

# Increase drain timeout if needed
environment.db_isolation.evaluation_drain_timeout_seconds: 60
```

### Issue 2: Template Out of Sync

**Symptom:** DB mode fails, full-container baseline succeeds

**Solution:**
```bash
# Rebuild PostgreSQL template
docker exec pg18-gitlab-nondb pg_dump gitlab_production > latest.sql
docker exec pg18-gitlab-nondb psql -c "DROP DATABASE gitlab_base_template"
docker exec pg18-gitlab-nondb psql -c "CREATE DATABASE gitlab_base_template"
docker exec pg18-gitlab-nondb psql gitlab_base_template < latest.sql
```

### Issue 3: MySQL Template Issues

**Symptom:** Shopping tasks fail

**Solution:**
```bash
# Verify MySQL template
docker run --rm \
  -v ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template:/var/lib/mysql \
  mysql:8.0

# Check for errors
docker logs <container_id>

# Recreate if needed
./scripts/setup_mysql_template.sh
```

### Issue 4: State Leakage

**Symptom:** Later tasks fail, early tasks pass

**Solution:**
```python
# Verify route isolation
route = route_registry.resolve_route(agent_id)
print(f"DB: {route.db_name}")
print(f"Namespace: {route.state_namespace}")

# Check Redis namespacing
redis-cli keys "*webagent*"
# Should show separate namespaces per agent
```

## Debugging Workflow

### Step 1: Run Single Task

```bash
python scripts/validate_reward_consistency.py \
  --tasks 5 \
  --site gitlab \
  --output-dir debug/task_5
```

### Step 2: Compare Traces

```bash
# View full-container baseline trace
cat debug/task_5/container_baseline_mode/task_5/trace.log

# View DB trace
cat debug/task_5/db_mode/task_5/trace.log

# Diff them
diff debug/task_5/container_baseline_mode/task_5/trace.log \
     debug/task_5/db_mode/task_5/trace.log
```

### Step 3: Check Database State

```bash
# Connect to agent's database
docker exec pg18-gitlab-nondb psql -d agent_task_5_xxx_db

# Check table counts
SELECT COUNT(*) FROM issues;
SELECT COUNT(*) FROM merge_requests;
```

### Step 4: Verify Isolation

```python
# Check route registry
from rl_web_agent.isolation import SQLiteRouteRegistry

registry = SQLiteRouteRegistry("./runtime/db_routes.sqlite3")
routes = registry.list_route_records()

for agent_id, route in routes.items():
    print(f"{agent_id}: {route.db_name} (gen={route.generation})")
```

## Success Criteria

### Minimum (MVP)
- ✅ 95% reward match on 10-task sample
- ✅ No systematic bias
- ✅ Critical tasks pass

### Production
- ✅ **100% reward match on full task set**
- ✅ **100% success match**
- ✅ **Statistical tests show no difference**
- ✅ **Validated across all sites**

## Running Validation Now

### Prerequisites Check

```bash
# 1. full-container baseline server running?
curl http://localhost:8001/health

# 2. PostgreSQL ready?
docker exec pg18-gitlab-nondb psql -U postgres -c "SELECT 1"

# 3. GitLab ready?
curl http://localhost:8023/

# 4. MySQL template exists? (for shopping)
ls -la ${WEBAGENT_ROOT:-/var/lib/web-agent}/mysql_clone_xfs/magento_template/
```

### Run Quick Validation

```bash
cd /path/to/CUA-Sandbox

# Test GitLab (PostgreSQL)
./scripts/quick_validation.sh gitlab small

# Expected: 3/3 tasks pass, ~5 minutes
```

### Interpret Results

**If PASSED:**
```
✅ Rewards are consistent!
✅ DB isolation is working correctly
✅ Safe to use for inference evaluation
```

**If FAILED:**
```
❌ Check the detailed report in validation_results/
❌ Review REWARD_CONSISTENCY_VALIDATION.md
❌ Follow debugging workflow
❌ Fix issues and re-run
```

## Integration with CI/CD

```yaml
# Add to .github/workflows/test.yml
- name: Validate Reward Consistency
  run: |
    python scripts/validate_reward_consistency.py \
      --test-suite small \
      --site gitlab
```

## Output Files

After running validation, you'll get:

```
validation_results/
├── reward_consistency_report.txt     # Human-readable report
├── reward_consistency_data.csv       # Data for analysis
├── reward_consistency_full.json      # Complete raw data
├── container_baseline_mode/                       # full-container baseline execution logs
│   └── task_*/
└── db_mode/                          # DB mode execution logs
    └── task_*/
```

## Summary

You now have:

1. ✅ **Automated validation script** - Runs tasks in both modes
2. ✅ **Quick validation wrapper** - One command to test
3. ✅ **Comprehensive guide** - Debugging and troubleshooting
4. ✅ **CI/CD integration** - Ready for continuous validation

**Next Step: Run the quick validation to verify everything works!**

```bash
./scripts/quick_validation.sh gitlab small
```

This will tell you if your DB isolation implementation produces the same rewards as full-container baseline baseline.
