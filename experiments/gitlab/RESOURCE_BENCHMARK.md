# GitLab DB-Isolation Resource Benchmark

## Paper metric

CUA-Sandbox Table 2 benchmarks one writable **WebArena Shopping** server instance on
an AWS EC2 `r6id.metal` host:

| Metric | CUA-Sandbox (full-container baseline) | Naive Docker |
| --- | ---: | ---: |
| Launch | 1.781 s | 8.963 s |
| Storage | 28.01 MiB | 6.78 GiB |
| Memory | 1.74 GiB | 1.63 GiB |

Storage is the persistent disk added by an instance. Memory is the running
server instance's memory. The paper does not specify the sampling command,
repeat count, or whether filesystem cache is included.

## Container-free metric

The local benchmark targets **GitLab**, not Shopping. GitLab, PostgreSQL and the
route registry are shared, so resource usage has two parts:

1. a fixed shared-service baseline;
2. the marginal cost of each active DB-isolated episode.

`benchmark_db_isolation_resources.py` creates four simultaneous episodes. Each
episode clones `gitlab_base_template` with PostgreSQL `FILE_COPY`, prepares the
GitLab non-DB namespace, publishes a route and makes a routed HTTP request. It
measures:

- physical storage from the used-block delta on the shared XFS filesystem;
- memory from the GitLab, PostgreSQL and route-registry cgroups, including
  anonymous memory and filesystem cache;
- setup latency around the complete DB/non-DB preparation operation.

`du` is intentionally not used for the primary storage result: it counts shared
reflink extents once per file and reports each logical database as about
10.25 GiB even though very few new physical blocks are allocated.

## Result (2026-08-10)

| Metric | GitLab DB isolation |
| --- | ---: |
| Median setup latency | 3.451 s |
| Median physical storage / episode | 38.14 MiB |
| Aggregate physical storage slope / episode | 37.87 MiB |
| Median active memory increment / episode | 106.17 MiB |
| Aggregate active memory slope / episode | 111.95 MiB |
| Logical PostgreSQL database / episode | 10.25 GiB |
| Fixed shared-service cgroup baseline | 21.49 GiB |

All four routed HTTP probes returned 200. After cleanup, no benchmark databases
or routes remained and the physical storage delta was 20 KiB. The shared
service cgroups retained about 214 MiB above the immediate pre-run baseline;
Ruby/PostgreSQL allocators and caches may retain a high-water mark even after
route pools and databases are released. Repeated long-run sampling is required
before classifying retained cgroup memory as a leak.

These numbers are not a direct speed or memory ranking against Table 2 because
the applications, host hardware and isolation architecture differ. The valid
comparison is structural: full-container baseline pays the memory of every full server instance,
while DB isolation pays a large fixed GitLab service cost and a much smaller
marginal cost per episode.

## Reproduce

Run only while no evaluation routes are active:

```bash
.venv-gitlab-eval/bin/python \
  experiments/gitlab/benchmark_db_isolation_resources.py \
  --samples 4 \
  --output results/gitlab_db_isolation_resources.json
```

Raw measurements from this run are in
`results/gitlab_db_isolation_resources_20260810.json`.
