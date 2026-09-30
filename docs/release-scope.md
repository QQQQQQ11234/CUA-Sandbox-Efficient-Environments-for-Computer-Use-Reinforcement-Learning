# Release scope

This repository is the public inference and evaluation release for the
CUA-Sandbox WebArena runtime. It follows the same boundary used by inference
harness releases: a configured external model supplies the decisions, while
this repository supplies the executable environment, tools, state lifecycle,
verification, and persistent traces.

## Included

- regular and function-calling WebArena agents;
- Playwright browser execution and task evaluators;
- the shared-runtime state-capsule backend;
- signed route capabilities, generation leases, reset/restore/checkpoint,
  clone/fork, and non-database state adapters;
- reviewed task manifests and state audits;
- JSON traces, batch summaries, replay helpers, and lifecycle tests.

## Excluded

- SFT, RL, or any other trainer;
- VERL/Ray/SGLang rollout-training integration;
- official training trajectories, datasets, or model checkpoints;
- claims that an external model is reproduced by this repository.

The former local training-only assets are kept outside the public tree in the
working directory when needed for internal experiments. They are not required
for inference installation or evaluation.

Export with `python scripts/export_inference.py --output ../cua-sandbox`.
Start a new public Git repository in that output directory; do not push the
working checkout history, which still contains the original training sources.
