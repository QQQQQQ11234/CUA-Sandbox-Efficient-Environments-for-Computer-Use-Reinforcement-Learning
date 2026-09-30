#!/usr/bin/env python3
"""Export an inference source tree, without local state or Git history.

Run from the working checkout; the destination must not already exist.
Only explicitly listed source roots and file types are copied. Symlinks are
rejected so private content cannot be pulled into the release indirectly.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT_FILES = (
    'README.md', 'LICENSE', 'CITATION.cff', 'CONTRIBUTING.md', 'Makefile',
    'pyproject.toml', 'uv.lock', 'config.yaml',
    '.gitignore', '.dockerignore', '.pre-commit-config.yaml',
)
SOURCE_ROOTS = (
    'rl_web_agent', 'experiments', 'scripts', 'tests', 'docs', 'examples',
    'gitlab_shared', 'shopping_shared', 'thirdparty/webarena', '.github',
)
SUFFIXES = {
    '.py', '.rb', '.php', '.js', '.sh', '.md', '.txt', '.json', '.yaml', '.yml',
    '.toml', '.cfg', '.cff', '.html', '.css', '.svg', '.png', '.jpg', '.jpeg',
}
NAMES = {'Dockerfile', 'registry.Dockerfile', 'LICENSE', 'Notice.txt', 'Makefile', 'py.typed', '.env.example'}
EXCLUDED_PARTS = {
    '__pycache__', '.git', '.auth', 'runtime', 'results', 'outputs',
    '.venv', '.pytest_cache', 'build', 'dist', 'verl', 'checkpoints',
}
EXCLUDED_FILES = {'browser_tool.py', 'dummy_browser_tool.py', 'webarena.sh', 'webarena_tool_config.yaml', 'comparison_replay.py', 'test_reward_consistency.py', 'validate_reward_consistency.py'}


def export(destination: Path) -> int:
    root = Path(__file__).resolve().parent.parent
    destination = destination.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError('Export to a new directory outside the working checkout')
    if destination.exists():
        raise FileExistsError(f'Refusing to overwrite {destination}')
    files = [root / name for name in ROOT_FILES]
    for name in SOURCE_ROOTS:
        for path in (root / name).rglob('*'):
            relative = path.relative_to(root)
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            if path.name in EXCLUDED_FILES or any(part.endswith('.egg-info') for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'Release source must not contain symlinks: {relative}')
            if path.is_file() and (path.suffix in SUFFIXES or path.name in NAMES):
                files.append(path)
    # Validate all inputs before creating the release directory.
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Invalid release source: {path}')
    destination.mkdir(parents=True)
    for source in sorted(set(files)):
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = sorted(str(path.relative_to(root)) for path in set(files))
    (destination / 'RELEASE_FILES.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return len(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    count = export(args.output)
    print(f'Exported {count} inference source files to {args.output}')


if __name__ == '__main__':
    main()
