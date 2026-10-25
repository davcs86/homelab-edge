# Database host (`tag:db`)

One Debian 13 LXC (`db`) runs PostgreSQL 18 with TimescaleDB (Apache build), PgBouncer and pgBackRest for every workload.
`playbooks/db.yml` (role `db_host`) configures it; the container itself comes from `tools/pve-setup.py lxc-create`.

| Port | Service | Reached by |
|---|---|---|
| 5432 | PostgreSQL, TLS only | config, ledger, identity, notify, ui and the migrator (they need `LISTEN/NOTIFY` and advisory locks) |
| 6432 | PgBouncer, transaction mode, TLS | trading, portfolio, marketdata, indicators, ingest, analysis (`DB_PGBOUNCER=true`) |
| 8432 | pgBackRest TLS server | the edge, which pulls backups (`tag:edge`) |
| 22 | sshd | `tag:ci` (this playbook), admins |

Per workload (`db_workloads` in `group_vars/all/main.yml`): database `xstockstrat-<env>`, owner role `xstockstrat_<env>`, a
`CONNECT` grant for that role and `admin` only, a `hostssl` line for that workload's tailnet IP, a PgBouncer pool named after the
database. `admin` is read-only (`pg_monitor`, `pg_read_all_data`). Passwords live in `secrets/edge.sops.yaml`.

## First build

1. **Secrets.** Run **Actions → init-secrets** with `fill_missing` on the branch. It adds the three `db_role_*_password` values, the
   database TLS CA and server cert (`db_tls_*`, its own CA, not the pgBackRest one), and, signed by the pgBackRest CA, the
   database host's pgBackRest server cert and the edge's client cert (`pgbackrest_db_server_*`, `pgbackrest_edge_client_*`).
   Nothing existing changes. A set that is only partly present is refused.
2. **Edge.** Run **deploy** (tailnet). The edge now holds its client cert and the `[main]` stanza section; the backup timers stay
   off (`pgbackrest_backups_enabled: false`).
3. **Container.** On the PVE host: `python3 pve-setup.py lxc-create`. Answers: Debian 13 template, hostname `db`, root disk
   `8G`, 2 cores, memory `3072`, swap `0`, **no** Docker nesting, **yes** to the data volume (`64G`, mounted at
   `/var/lib/postgresql`, tuned: `recordsize=16K`, `compression=lz4`, `atime=off`), **yes** to `/dev/net/tun`. The data volume is a
   separate filesystem, so the heartbeat's 85% check measures the data and not the root disk.
4. **Host key.** The playbook pins it under the alias `db` in `inventories/prod/known_hosts`. On the PVE host:
   `pct exec <id> -- cat /etc/ssh/ssh_host_ed25519_key.pub`, compare its fingerprint with
   `pct exec <id> -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, then add the line `db <that key>` to `known_hosts` in a PR.
5. **Bootstrap** (once, from an admin machine that has the SOPS key and the SSH key you gave `lxc-create` for root; over the LAN):
   mint a one-hour key with `--tags tag:db` (`tailnet-devices.md`), then
   ```sh
   export DB_TAILSCALE_AUTHKEY=<the key>
   ansible-playbook playbooks/db.yml -e db_stage=bootstrap -e ansible_user=root -e ansible_host=<LAN IP of the container>
   ```
   It creates the `deploy` user, hardens sshd, installs the pinned Tailscale and joins as `db` with `tag:db`. Check
   **node-admin → list-devices**: `db` carries `tag:db`.
6. **Converge.** **node-admin → mint-ci-key** (24 h), then **Actions → db-host**. It installs the pinned packages, writes the
   configuration, creates roles, databases and the TimescaleDB extension, sets passwords, builds the PgBouncer user list from
   PostgreSQL's SCRAM verifiers, and ends with checks: all four listeners answer, `ssl=on`, `max_locks_per_transaction=1024`,
   UTF8, no `pg_hba` errors, the extension version equals `db_timescaledb_version`, and a pooled query succeeds as each workload role.
   A workload host that is not on the tailnet yet gets no `hostssl` line and a message; run again after it joins.
7. **Backups.** On the edge, once: `sudo -u pgbackrest pgbackrest --stanza=main stanza-create`. Then in one PR set
   `db_archive_enabled: true` and `pgbackrest_backups_enabled: true`, run **deploy** and **db-host**. The last task of
   **db-host** runs `pgbackrest --stanza=main check`. Take the first backup by hand
   (`sudo -u pgbackrest pgbackrest --stanza=main --type=full backup`) and read the repository size against the 15 GB budget.
   Archiving stays off until the stanza exists, because a failing `archive_command` fills the data disk.
8. **Heartbeat.** From the PVE host: `python3 pve-setup.py db-heartbeat`. The address to give is the container's **tailnet IP**
   (`tailscale ip -4` inside it): PostgreSQL and PgBouncer listen on loopback and that address, pgBackRest only on that address, so only
   it works for all three checks. The data directory is `/var/lib/postgresql/18/main`.
9. **Gate before cutover** (build guide, phase 3): restore a staging dump into a scratch database, compare row counts, one full restore
   and one point-in-time recovery from the edge repository, both timed.

## Why it can be re-run

Every task is idempotent. A role password is set only when logging in with the SOPS value fails, so a rotated secret converges and a
steady state changes nothing. `pg_hba.conf` is rewritten whole from the template; a workload is added by adding it to `db_workloads`
(with a `password_key` that you also add to `GENERATED` in `scripts/init_secrets.py`, then `fill_missing`); see `onboard-service.md`.

## Constraints worth knowing

- **Compression is not enabled.** The package is TimescaleDB's Apache build, the same edition as DigitalOcean's. Compression needs the
  community build and waits for the DigitalOcean cutover; xstockstrat's `003_canonicalize_ohlcv_timeframe` also refuses to run
  against compressed `marketdata.ohlcv` chunks.
- **The extension version must equal the source's for a restore.** DigitalOcean production runs 2.30.2, the pinned version. Read
  `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` again before the cutover and after any bump of either side.
- **Locales are placeholders.** `db_cluster_locale` and `db_workload_locale` are `C.UTF-8` until you compare them with DigitalOcean's
  `datcollate`. The workload databases are created with `db_workload_locale`, so drop and recreate an empty one to change it.
- **Role settings** (`statement_timeout`, `idle_in_transaction_session_timeout`) are per workload `settings:`, empty until you give the values.
- **`tag:ci` reaches this host's sshd and the `deploy` user has passwordless sudo.** Anything that runs with a `tag:ci` key (including
  xstockstrat's deploy pipeline) can become root here. The owner accepted this; `tag:deploy` would narrow it with a one-line grant change plus the workflow's key.
- `tailscale up` runs with `--accept-dns=true` (the host resolves `edge.ts…` and the workload names through MagicDNS). If apt cannot resolve
  names after the join, look at headscale's DNS settings first.
- The backup schedules (`pgbackrest_full_schedule`, `pgbackrest_diff_schedule`) are not stated by the owner; check them.

## Break glass

`pct enter <id>` on the PVE host reaches the container without the tailnet. `pg_hba.conf` accepts only the tailnet IPs listed and
loopback, so a lost workload address is fixed by the next **db-host** run. To restore PostgreSQL from the edge repository, run `pgbackrest --stanza=main restore` on the database host after
stopping `postgresql@18-main`; `recovery.md` covers the edge, not this host.
