#!/usr/bin/env bash
# Remove generated local state without touching source, manifests, or tests.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

rm -rf \
  .pytest_cache \
  .ruff_cache \
  .mypy_cache \
  outputs \
  validation_results \
  runtime

find . -type d -name __pycache__ -prune -exec rm -rf {} +
find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

printf '%s\n' "Removed generated caches, outputs, and runtime state from $project_root"
printf '%s\n' 'Benchmark results under results/ are preserved; remove them explicitly when desired.'
