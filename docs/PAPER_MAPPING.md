# Paper-to-code mapping

The CUA-Sandbox paper separates logical mutable state from initialized runtime.
The public code implements that method at inference time as follows.

| Paper mechanism | Public implementation |
| --- | --- |
| Task-aware resource contract | `experiments/state_audits/`, `experiments/*/*_task_contracts.json`, `StateCapabilityGate` |
| State capsule identity and generation | `RouteRecord`, `StateIdentity`, `DBAgentSession` |
| Database branch / copy-on-write backend | `DBIsolationManager`, `MySQLXFSReflinkManager` |
| Non-database state adapters | `NonDBStateCoordinator`, OverlayFS/GitLab/Magento backends |
| Environment-scoped binding table | signed `X-Agent-Route` plus `TrustedDBRouter` |
| Request and background-work lease | `SQLiteRouteRegistry.acquire`, `enqueue_background_job`, `release` |
| Transactional reset/fork/clone | `DBIsolationManager` lifecycle methods and registry freeze/activate operations |
| Freeze, drain, stage, readiness, atomic publish | `DBIsolationManager._freeze_and_drain` and route registry barriers |
| Shared execution runtime | `WebAgentEnv` with configured shared site hosts |
| Original agent interface | `WebAgentEnv.step`, `ToolWebAgent`, regular agent, and evaluator |
| Inference trace | `TaskTracer`, `batch_summary.json`, task `trace.json` and `result.json` |

The release deliberately does not include the training stages described in the
paper. An external OpenAI-compatible or Bedrock model is the inference policy;
the code does not train or publish a policy checkpoint.
