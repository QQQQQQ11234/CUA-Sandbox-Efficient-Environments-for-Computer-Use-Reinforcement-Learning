# Local Validation Status

Date: 2026-07-14

## Docker

- Docker daemon access works for user `yanx` through group `docker`.
- Docker Engine server version: 24.0.2.
- No WebArena GitLab image was initially installed.

## WebArena GitLab image

- Official source:
  `http://metis.lti.cs.cmu.edu/webarena-images/gitlab-populated-final-port8023.tar`
- Remote size: 77,755,595,776 bytes (72.4 GiB).
- Partial download: `/tmp/gitlab-populated-final-port8023.tar`.
- aria2 state: `/tmp/gitlab-populated-final-port8023.tar.aria2`.
- Approximately 97% was downloaded before the CMU server started resetting all
  Range requests from this host.

Resume without restarting the download:

```bash
aria2c -c -x1 -s1 -k4M --file-allocation=none \
  -d /tmp -o gitlab-populated-final-port8023.tar \
  http://metis.lti.cs.cmu.edu/webarena-images/gitlab-populated-final-port8023.tar
```

After completion:

```bash
docker load --input /tmp/gitlab-populated-final-port8023.tar
rm -f /tmp/gitlab-populated-final-port8023.tar{,.aria2}
```

The readable image config identifies the image as
`gitlab-populated-final-port8023`, created on 2023-07-02 from an Omnibus GitLab
base built on 2023-01-12. Exact GitLab/Rails/ActiveRecord versions still require
loading and inspecting the complete image.

## PostgreSQL 18

- Container: `pg18_clone_lab`.
- PostgreSQL: 18.4.
- Port: 55432 on the host.
- `file_copy_method=clone` is enabled.
- Data directory is on `/mnt/data`, which is ext4 without reflink support.
- Existing template: `base_template_db`, approximately 43 MB.

Five `CREATE DATABASE ... STRATEGY FILE_COPY` runs on ext4 took:

```text
4.45s, 4.42s, 4.42s, 4.73s, 5.10s
```

These numbers are correctness-only baseline results, not CoW performance
results. Repeat the same benchmark with `PGDATA` on XFS `reflink=1`, Btrfs, or
ZFS before making performance claims.
