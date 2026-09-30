#!/usr/bin/env python3
"""GitLab clone using a real volume-level overlayfs CoW branch.

The timed sandbox operation creates empty upper/work directories, mounts the
PostgreSQL and GitLab non-DB lower volumes as overlayfs branches, and writes a
branch manifest.  It never walks or copies the lower trees.  PostgreSQL child
startup and an independent write are validation outside the timed interval.
Docker is measured as stopped child creation from a prepared GitLab image.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path("/var/lib/web-agent/pg18_clone_xfs")
BENCH = ROOT / "gitlab_volume_cow_benchmark"
HELPER = "gitlab-volume-cow-helper"
IMAGE = "gitlab-populated-final-port8023:latest"


def run(argv: list[str], *, check: bool = True, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=timeout)


def helper_script(script: str, *, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return run(["docker", "exec", HELPER, "bash", "-lc", script], timeout=timeout)


def dist(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": ordered[(len(ordered) - 1) // 2] if ordered else None,
        "mean": sum(ordered) / len(ordered) if ordered else None,
        "min": min(ordered) if ordered else None,
        "max": max(ordered) if ordered else None,
    }


def sandbox_clone(run_id: str, iteration: int) -> dict[str, Any]:
    upper = f"/cow-host/gitlab_volume_cow_upper/branch-{run_id}-{iteration}"
    work = f"/cow-host/gitlab_volume_cow_work/branch-{run_id}-{iteration}"
    merged = f"/cow-host/gitlab_volume_cow_merged/branch-{run_id}-{iteration}"
    script = f"""
set -euo pipefail
u={upper}; w={work}; m={merged}
rm -rf "$u" "$w" "$m"
mkdir -p "$u" "$w" "$m"
chown 999:999 "$u" "$w" "$m"
t0=$(date +%s%N)
mount -t overlay overlay -o lowerdir=/cow-host/pg18_clone_xfs,upperdir="$u",workdir="$w" "$m"
chmod 755 "$m"; chmod 700 "$m/data/pgdata"
printf '%s\n' '{{"mode":"volume-overlay-cow","iteration":{iteration}}}' > "$m/branch-manifest.json"
t1=$(date +%s%N)
echo "COW_ELAPSED_NS=$((t1-t0))"
"""
    operation = helper_script(script)
    match = re.search(r"COW_ELAPSED_NS=(\d+)", operation.stdout)
    if not match:
        raise RuntimeError("volume CoW helper did not report operation duration")
    elapsed = int(match.group(1)) / 1_000_000_000
    # Validation is outside clone timing.  PostgreSQL recovery can take tens
    # of seconds on this large cluster; that is startup, not branch creation.
    port = 55500 + iteration
    validate = f"""
set -euo pipefail
b={merged}
runuser -u postgres -- /usr/lib/postgresql/18/bin/postgres -D "$b/data/pgdata" -k /tmp -p {port} >"$b/pg.log" 2>&1 &
for i in $(seq 1 90); do pg_isready -h /tmp -p {port} -U postgres >/dev/null 2>&1 && break; sleep 1; done
pg_isready -h /tmp -p {port} -U postgres
psql -h /tmp -p {port} -U postgres -Atc 'select 1;' | grep -qx 1
psql -h /tmp -p {port} -U postgres -v ON_ERROR_STOP=1 -c "create table if not exists public.cow_clone_probe(value text); insert into public.cow_clone_probe values ('child-{iteration}');" >/dev/null
runuser -u postgres -- /usr/lib/postgresql/18/bin/pg_ctl -D "$b/data/pgdata" -m fast -w stop >/dev/null
umount "$b"
rm -rf "{upper}" "{work}" "{merged}"
"""
    try:
        helper_script(validate, timeout=180)
    except Exception:
        # Preserve the branch for diagnosis if validation fails; the caller
        # records the error instead of treating a timed clone as successful.
        raise
    return {"seconds": elapsed, "state_valid": True, "independent_write": True, "mode": "volume-overlay-cow"}


def docker_clone(image: str, token: str) -> dict[str, Any]:
    child = f"gitlab-volume-docker-child-{token}"
    payload = json.dumps({"Image": image, "Cmd": ["sleep", "infinity"]})
    started = time.perf_counter()
    response = run([
        "curl", "--silent", "--show-error", "--unix-socket", "/var/run/docker.sock",
        "-H", "Content-Type: application/json", "-X", "POST",
        "-d", payload, f"http://localhost/containers/create?name={child}",
    ], timeout=120)
    elapsed = time.perf_counter() - started
    body = json.loads(response.stdout)
    if "Id" not in body:
        raise RuntimeError(f"Docker API create failed: {body}")
    try:
        running = run(["docker", "inspect", "-f", "{{.State.Running}}", child]).stdout.strip()
        return {"seconds": elapsed, "state_valid": running == "false", "mode": "docker-image-cow"}
    finally:
        run(["docker", "rm", "-f", child], check=False, timeout=120)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    image = f"gitlab-volume-cow-image-{run_id}:latest"
    source = f"gitlab-volume-cow-source-{run_id}"
    ours: list[dict[str, Any]] = []
    docker: list[dict[str, Any]] = []
    run(["docker", "rm", "-f", HELPER], check=False, timeout=120)
    run(["docker", "run", "--detach", "--name", HELPER, "--privileged",
         "-v", "/var/lib/web-agent:/cow-host", "postgres:18", "sleep", "infinity"], timeout=120)
    try:
        run(["docker", "create", "--name", source, IMAGE], timeout=120)
        run(["docker", "commit", source, image], timeout=900)
        for iteration in range(args.iterations):
            try:
                ours.append(sandbox_clone(run_id, iteration))
            except Exception as error:
                ours.append({"error": repr(error)})
            try:
                docker.append(docker_clone(image, uuid.uuid4().hex[:8]))
            except Exception as error:
                docker.append({"error": repr(error)})
    finally:
        run(["docker", "rm", "-f", source], check=False, timeout=120)
        run(["docker", "image", "rm", "--force", image], check=False, timeout=300)
        run(["docker", "rm", "-f", HELPER], check=False, timeout=120)
    good_ours = [x for x in ours if "error" not in x]
    good_docker = [x for x in docker if "error" not in x]
    ours_dist = dist([x["seconds"] for x in good_ours])
    docker_dist = dist([x["seconds"] for x in good_docker])
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "semantics": "volume-level overlayfs CoW branch for PostgreSQL and GitLab non-DB tree; child startup and independent write outside timed clone",
        "host": {"hostname": platform.node(), "platform": platform.platform()},
        "ours": {"samples": ours, "summary": ours_dist},
        "docker": {"samples": docker, "summary": docker_dist},
        "speedup_docker_over_ours": (
            docker_dist["p50"] / ours_dist["p50"]
            if ours_dist["p50"] and docker_dist["p50"] else None
        ),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
