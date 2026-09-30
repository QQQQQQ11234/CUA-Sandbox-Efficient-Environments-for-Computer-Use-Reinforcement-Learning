# Contributing

Keep changes scoped to the runtime, state adapters, deployment assets, or
benchmark tooling they affect. New state sharing must be accompanied by a
machine-readable task contract and a test that proves isolation across two
logical environments.

Before opening a pull request, run:

```bash
make compile
make test
make lint
```

Backend changes should also include the relevant smoke test and a note about
the application image version. Do not commit credentials, browser profiles,
runtime databases, benchmark outputs, or model checkpoints. Use
`scripts/clean_workspace.sh` to remove generated local state.

When changing a lifecycle operation, preserve the freeze/drain/stage/readiness/
atomic-publish ordering. A direct route update that can expose a mixed
component generation is a correctness regression even if a single-agent smoke
test passes.
