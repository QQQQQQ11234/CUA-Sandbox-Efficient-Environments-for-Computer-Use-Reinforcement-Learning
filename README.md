# CUA-Sandbox

## Efficient Environments for Computer-Use Agent Reinforcement Learning

Xin Yan, Zhengbo Jiao, Jiaqi Liu, Zhenglin Wan, SiYuan Ma, Xuliang Yu, Tianyi Jiang, Chubin Zhang, Pengfei Zhou, Wangbo Zhao, Xingrui Yu, Bo An, Yang You, Ivor Tsang

> **TL;DR.** CUA-Sandbox separates an initialized web application runtime from the mutable state of each computer-use agent. Every rollout receives a private state capsule, a signed route capability, and a generation lease, while the application processes remain shared. This makes parallel inference environments cheaper without changing the browser action interface or evaluator.

[Paper](https://arxiv.org/abs/2609.32750) · [Citation](#citation) · [Paper-to-code mapping](docs/PAPER_MAPPING.md)

## News

- **2026-09-30:** Inference and evaluation code released as `cua-sandbox`.
- **2026:** CUA-Sandbox paper released as [arXiv:2609.32750](https://arxiv.org/abs/2609.32750).

## Abstract

Computer-use agents need a clean environment for every task, but starting a complete copy of every web application is expensive and limits parallel evaluation. CUA-Sandbox keeps the initialized application runtime shared and isolates only the mutable state that a rollout can reach. A state contract identifies those resources; a state capsule gives the rollout private database and non-database branches; a signed route capability selects the capsule; and a generation lease keeps requests, callbacks, background work, and evaluator reads on one consistent version.

Reset, checkpoint, clone, fork, and cleanup use a transactional lifecycle:

```text
freeze route -> drain admitted work -> stage successor capsule
-> bind contracted resources -> run readiness checks
-> atomically publish generation -> resume route
```

If staging or readiness fails, the previous generation remains published. This prevents mixed-generation observations while shared application processes continue serving other environments.

## CUA-Sandbox Runtime

The public repository implements the inference-time environment and evaluation runtime described in the paper:

- **State contracts:** task-specific declarations of database, filesystem, uploads, queues, caches, and other mutable resources.
- **State capsules:** PostgreSQL branches, MySQL/XFS reflinks, and application-specific non-database adapters.
- **Trusted routing:** an HMAC-signed `X-Agent-Route` capability maps an environment and generation to its private resources. The browser never chooses a database name.
- **Lifecycle barriers:** freeze, drain, stage, readiness, publish, reset, clone, fork, and cleanup are coordinated by the route registry.
- **Inference traces:** task trajectories, state audits, route events, and batch summaries are written to JSON files under ignored output directories.

The paper evaluates matched Docker and CUA-Sandbox backends on WebArena-Lite, VisualWebArena, and OSWorld. This release contains the WebArena-compatible environment and its shared-runtime adapters; the complete VisualWebArena and OSWorld adapters are not included.

## Initial Code Release

The paper describes a complete training setting. This repository is the **inference and evaluation release**: it runs an external model against the environment and exposes the state-isolation method.

| Paper or system component | Current release | Implementation |
| --- | --- | --- |
| Browser environment and task evaluator | ✅ | `rl_web_agent/` |
| Regular and function-calling inference agents | ✅ | `rl_web_agent/agent.py`, `rl_web_agent/tool_agent.py` |
| State capsules and route registry | ✅ | `rl_web_agent/isolation/` |
| Reset, checkpoint, clone, fork, and cleanup lifecycle | ✅ | `DBIsolationManager`, route registry |
| Task contracts and state audits | ✅ | `experiments/state_audits/`, task contract files |
| JSON traces and batch summaries | ✅ | `TaskTracer`, batch entrypoints |
| SFT, RL, or other trainer code | Not released | Outside this repository |
| Official training trajectories, datasets, and checkpoints | Not released | Outside this repository |

No training dependency is required to install or run the inference release. An OpenAI-compatible endpoint, Bedrock endpoint, or optional local model server supplies the policy decisions.

## Installation

Python 3.10 or newer is required. CUDA is optional when using an external model endpoint.

```bash
git clone <your-public-repository-url> cua-sandbox
cd cua-sandbox

# Recommended
uv sync
uv run playwright install chromium

# Or use an existing virtual environment
python -m pip install -e .
python -m playwright install chromium
```

Create local configuration from the template:

```bash
cp examples/.env.example .env
export ROUTE_TOKEN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

The checked-in configuration contains portable defaults only. Hosts, credentials, model endpoints, route secrets, database DSNs, and storage paths should be supplied through environment variables or Hydra overrides.

## Quick Start

The basic inference path needs WebArena-compatible services, a task directory containing `<task_id>.json` files, and an OpenAI-compatible model endpoint.

Run CUA-Sandbox inference with the regular agent:

```bash
python -m rl_web_agent.entrypoints.batch_agent \
  --tasks_dir /path/to/tasks \
  --task_ids 1,2,3 \
  --output_dir results/smoke \
  --max_concurrent 3 \
  environment.isolation.mode=db \
  environment.db_isolation.route_token_secret="$ROUTE_TOKEN_SECRET"
```

Use the function-calling agent on the same shared-runtime backend:

```bash
python -m rl_web_agent.entrypoints.batch_agent \
  --tasks_dir /path/to/tasks \
  --task_ids 1,2,3 \
  --output_dir results/db-smoke \
  environment.isolation.mode=db \
  environment.db_isolation.route_token_secret="$ROUTE_TOKEN_SECRET" \
  --agent_type tool
```

The external-model function-calling agent is selected with `--agent_type tool`. The minimal launcher is [`examples/inference.sh`](examples/inference.sh). It records task results and traces under `results/`, which is ignored by Git.

Start the route registry sidecar for the GitLab adapter:

```bash
WEB_AGENT_ROUTE_REGISTRY_SECRET="$ROUTE_TOKEN_SECRET" \
python -m rl_web_agent.entrypoints.route_registry_server \
  --registry-path ./runtime/db_routes.sqlite3 \
  --host 127.0.0.1 --port 8765
```

The shared-runtime backend requires an immutable application template, a service that consumes the trusted route, a route registry secret, and a reviewed state contract. See [`docs/deployment.md`](docs/deployment.md) before enabling `environment.isolation.mode=db`.

## Code Architecture

```text
External model / policy
          |
          v
  WebAgentEnv + agent loop
          |
          v
  route capability -> route registry -> generation lease
          |                                  |
          v                                  v
  state capsule manager             request/background/evaluator scope
          |
          +-- PostgreSQL branch
          +-- MySQL/XFS reflink
          +-- non-database state adapters
          +-- readiness, audit, trace, and cleanup
```

| Path | Responsibility |
| --- | --- |
| `rl_web_agent/` | Browser environment, agents, evaluators, configuration, and isolation runtime |
| `rl_web_agent/isolation/` | Capsules, route registry, leases, lifecycle barriers, and state audits |
| `experiments/` | Task contracts, benchmark runners, and reproducible measurements |
| `gitlab_shared/` | Shared GitLab image and Rails route adapter |
| `shopping_shared/` | Shared Shopping/Magento image assets |
| `thirdparty/webarena/` | WebArena benchmark integration and task configuration |
| `tests/` | Inference interface, lifecycle, state-audit, and environment tests |
| `docs/` | Architecture, deployment, configuration, operations, and paper mapping |

Further documentation:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/configuration.md`](docs/configuration.md)
- [`docs/deployment.md`](docs/deployment.md)
- [`docs/PAPER_MAPPING.md`](docs/PAPER_MAPPING.md)
- [`docs/release-scope.md`](docs/release-scope.md)

## Reproducibility and Testing

Run the dependency-light checks:

```bash
python -m compileall -q rl_web_agent scripts experiments
python -m pytest -q tests/test_agent_interface.py \
  tests/test_route_lifecycle.py \
  tests/test_state_audit.py \
  tests/test_non_db_state.py
```

The lifecycle and backend integration checks require the corresponding PostgreSQL, MySQL, GitLab, Shopping, browser, and model services. They are kept separate from the unit suite so a clean checkout can validate the inference code without private training infrastructure.

To create a fresh public source tree without local state or historical Git files:

```bash
python scripts/export_inference.py --output ../cua-sandbox
```

The exporter includes inference sources, tests, documentation, and WebArena assets. It excludes training code, runtime state, results, virtual environments, local credentials, and Git history. Publish from the exported `cua-sandbox` directory.

## Configuration and Safety

Never commit `.env`, route secrets, service passwords, browser state, model outputs, or runtime databases. Route capabilities must be injected by a trusted ingress and client-supplied `X-Agent-Route` headers must be stripped before forwarding requests.

The state-capability gate fails closed when a contracted resource has no binding. Keep a task on the isolated backend until uploads, queues, search indexes, sessions, and other mutable resources are covered by a state contract.

## Citation

If you use CUA-Sandbox, please cite:

```bibtex
@misc{yan2026cuasandboxefficientenvironmentscomputeruse,
  title         = {CUA-Sandbox: Efficient Environments for Computer-Use Agent Reinforcement Learning},
  author        = {Xin Yan and Zhengbo Jiao and Jiaqi Liu and Zhenglin Wan and SiYuan Ma and Xuliang Yu and Tianyi Jiang and Chubin Zhang and Pengfei Zhou and Wangbo Zhao and Xingrui Yu and Bo An and Yang You and Ivor Tsang},
  year          = {2026},
  eprint        = {2609.32750},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2609.32750}
}
```

The WebArena benchmark is separately described in [WebArena](https://github.com/web-arena-x/webarena). See [`CITATION.cff`](CITATION.cff) for machine-readable citation metadata.

## License

The code is released under the MIT License; see [`LICENSE`](LICENSE).
